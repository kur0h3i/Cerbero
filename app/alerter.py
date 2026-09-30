"""Envío de alertas por Telegram con cooldown.

Las cabezas no hablan con Telegram ni con el almacén directamente: llaman a
``Alerter.fire`` mientras una condición se cumple y a ``Alerter.resolve``
cuando deja de cumplirse. El ``Alerter`` decide qué se envía:

- Una alerta con la misma clave no se reenvía hasta pasados
  ``ALERT_COOLDOWN_MIN`` minutos, aunque entre medias se resuelva y vuelva a
  dispararse (así una condición que va y viene no llena el chat). Si el
  problema persiste, pasado el cooldown se envía un recordatorio.
- Si la alerta sube de nivel (de warning a critical), se envía ya.
- Al resolverse se envía 🟢 [RESUELTO] solo si la alerta llegó a notificarse.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

import httpx

from .config import Settings
from .store import SEVERITY, Alert, AlertStore, Head, Level

log = logging.getLogger(__name__)

LEVEL_LABEL: dict[Level, str] = {
    "info": "🟢 [INFO]",
    "warning": "🟡 [AVISO]",
    "critical": "🔴 [CRÍTICO]",
}
RESOLVED_LABEL = "🟢 [RESUELTO]"
HEAD_LABEL: dict[Head, str] = {
    "recursos": "Recursos",
    "contenedores": "Contenedores",
    "accesos": "Accesos",
}

TIME_FMT = "%Y-%m-%d %H:%M:%S"


def format_message(
    label: str,
    head: Head | None,
    message: str,
    server: str,
    when: datetime,
    since: datetime | None = None,
) -> str:
    """Mensaje de Telegram (HTML): cabecera en negrita, mensaje y servidor · fecha."""
    title = f"{label} Cabeza: {HEAD_LABEL[head]}" if head else label
    lines = [f"<b>{html.escape(title)}</b>", f"Mensaje: {html.escape(message)}"]
    if since is not None:
        lines.append(f"Activa desde: {since.strftime(TIME_FMT)}")
    lines.append(f"Servidor: {html.escape(server)} · {when.strftime(TIME_FMT)}")
    return "\n".join(lines)


class Notifier(Protocol):
    async def send(self, text: str) -> bool: ...


class TelegramNotifier:
    """Envía mensajes a un chat con la Bot API.

    Nunca lanza: si Telegram falla se registra y se devuelve ``False`` (la
    alerta queda sin notificar y se reintenta la próxima vez que se dispare).
    """

    API_URL = "https://api.telegram.org"
    TIMEOUT_S = 10.0
    # Tope propio para no inundar el chat (ni chocar con los límites de Telegram)
    # si llega, p. ej., una fuerza bruta distribuida desde cientos de IPs.
    MAX_PER_MINUTE = 20

    def __init__(
        self,
        token: str | None,
        chat_id: str | None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token or ""
        self._chat_id = chat_id or ""
        self._client = client or httpx.AsyncClient(timeout=self.TIMEOUT_S)
        self._sent: deque[float] = deque()

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def _rate_limited(self) -> bool:
        now = time.monotonic()
        while self._sent and now - self._sent[0] > 60:
            self._sent.popleft()
        return len(self._sent) >= self.MAX_PER_MINUTE

    async def send(self, text: str) -> bool:
        if not self.enabled:
            log.info("Telegram no configurado; alerta solo en el registro:\n%s", text)
            return False
        if self._rate_limited():
            log.warning(
                "Límite de %d mensajes/min alcanzado; no se envía:\n%s", self.MAX_PER_MINUTE, text
            )
            return False
        self._sent.append(time.monotonic())
        url = f"{self.API_URL}/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        for attempt in range(2):
            try:
                resp = await self._client.post(url, json=payload)
            except httpx.HTTPError as exc:
                # El mensaje de algunas excepciones incluye la URL, que lleva el token.
                log.warning(
                    "Telegram no responde: %s", self._redact(str(exc)) or type(exc).__name__
                )
                return False
            if resp.status_code == 200:
                return True
            retry_after = _retry_after(resp)
            if resp.status_code == 429 and attempt == 0 and retry_after is not None:
                await asyncio.sleep(min(retry_after, 10))
                continue
            log.warning(
                "Telegram rechazó el mensaje (%s): %s",
                resp.status_code,
                self._redact(resp.text[:200]),
            )
            return False
        return False

    async def aclose(self) -> None:
        await self._client.aclose()


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.json()["parameters"]["retry_after"])
    except (ValueError, KeyError, TypeError):
        return None


class Alerter:
    def __init__(
        self,
        settings: Settings,
        store: AlertStore,
        notifier: Notifier,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._notifier = notifier
        self._clock = clock or settings.now
        self._cooldown = timedelta(minutes=settings.alert_cooldown_min)
        # Último envío por clave. Sobrevive a la resolución de la alerta: es lo
        # que evita el spam cuando una condición va y viene.
        self._last_sent: dict[str, datetime] = {}

    @property
    def store(self) -> AlertStore:
        return self._store

    async def fire(
        self,
        key: str,
        head: Head,
        level: Level,
        message: str,
        ttl: timedelta | None = None,
    ) -> Alert:
        """Abre (o mantiene) la alerta ``key`` y la notifica si toca.

        ``ttl`` es para eventos sin fin natural (un login desde fuera): la
        alerta se cierra sola, sin mensaje de resolución, pasado ese tiempo.
        """
        now = self._clock()
        previous = self._store.get(key)
        escalated = previous is not None and SEVERITY[level] > SEVERITY[previous.level]
        expires_at = now + ttl if ttl is not None else None
        alert, is_new = self._store.add(key, head, level, message, now, expires_at)
        if is_new:
            log.info("Alerta %s [%s] %s", key, level, message)

        last = self._last_sent.get(key)
        if last is not None and not escalated and now - last < self._cooldown:
            return alert
        reminder = alert.notified and not escalated
        text = format_message(
            LEVEL_LABEL[level],
            head,
            message,
            self._settings.server_name,
            now,
            since=alert.timestamp if reminder else None,
        )
        if await self._notifier.send(text):
            alert.notified = True
            self._last_sent[key] = now
        return alert

    async def resolve(self, key: str, message: str) -> Alert | None:
        """Cierra la alerta ``key`` si está activa y avisa si se había notificado."""
        now = self._clock()
        alert = self._store.resolve(key, now)
        if alert is None:
            return None
        log.info("Resuelta %s: %s", key, message)
        if alert.notified:
            text = format_message(
                RESOLVED_LABEL, alert.head, message, self._settings.server_name, now
            )
            await self._notifier.send(text)
        return alert

    def is_active(self, key: str) -> bool:
        return self._store.get(key) is not None

    def active_keys(self, prefix: str = "") -> list[str]:
        return self._store.active_keys(prefix)

    def expire(self) -> None:
        """Cierra en silencio los eventos caducados (los disparados con ``ttl``)."""
        now = self._clock()
        for key in self._store.expired_keys(now):
            self._store.resolve(key, now)

    async def announce(self, message: str) -> None:
        """Mensaje informativo que no es una alerta (p. ej., el arranque)."""
        text = format_message(
            LEVEL_LABEL["info"], None, message, self._settings.server_name, self._clock()
        )
        await self._notifier.send(text)
