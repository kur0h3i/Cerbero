"""Almacén de alertas en memoria.

Cada alerta tiene una *clave* que identifica la condición que la dispara
(``recursos:ram``, ``contenedores:caido:nginx``...). Mientras la condición dura,
la alerta está activa y volver a dispararla solo la actualiza; al resolverse
pasa al historial.

Las activas se guardan aparte y sin límite (hay tantas como condiciones en
curso) para que una ráfaga de eventos nunca eche del almacén una alerta que
sigue abierta. Del historial de resueltas se conservan las últimas 50.

Sin base de datos: si Cerbero se reinicia, el historial se pierde.
"""

from __future__ import annotations

import secrets
from collections import deque
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Level = Literal["info", "warning", "critical"]
Head = Literal["recursos", "contenedores", "accesos"]

HEADS: tuple[Head, ...] = ("recursos", "contenedores", "accesos")
SEVERITY: dict[Level, int] = {"info": 0, "warning": 1, "critical": 2}
HISTORY_SIZE = 50


class Alert(BaseModel):
    id: str
    key: str
    level: Level
    head: Head
    message: str
    # Cuándo se disparó por primera vez.
    timestamp: datetime
    active: bool = True
    resolved_at: datetime | None = None
    # Internos (no salen en la API):
    # si llegó a enviarse por Telegram (solo entonces se avisa de la resolución)...
    notified: bool = Field(default=False, exclude=True)
    # ...y, para los eventos sin fin natural, cuándo caducan.
    expires_at: datetime | None = Field(default=None, exclude=True)


class AlertStore:
    def __init__(self, history_size: int = HISTORY_SIZE) -> None:
        self._active: dict[str, Alert] = {}
        self._history: deque[Alert] = deque(maxlen=history_size)

    def get(self, key: str) -> Alert | None:
        """La alerta activa con esa clave, si la hay."""
        return self._active.get(key)

    def add(
        self,
        key: str,
        head: Head,
        level: Level,
        message: str,
        now: datetime,
        expires_at: datetime | None = None,
    ) -> tuple[Alert, bool]:
        """Abre una alerta o actualiza la activa con la misma clave.

        Devuelve la alerta y si es nueva.
        """
        alert = self._active.get(key)
        if alert is not None:
            alert.level = level
            alert.message = message
            alert.expires_at = expires_at
            return alert, False
        alert = Alert(
            id=secrets.token_hex(6),
            key=key,
            level=level,
            head=head,
            message=message,
            timestamp=now,
            expires_at=expires_at,
        )
        self._active[key] = alert
        return alert, True

    def resolve(self, key: str, now: datetime) -> Alert | None:
        """Cierra la alerta activa con esa clave y la pasa al historial."""
        alert = self._active.pop(key, None)
        if alert is None:
            return None
        alert.active = False
        alert.resolved_at = now
        alert.expires_at = None
        self._history.append(alert)
        return alert

    def active_keys(self, prefix: str = "") -> list[str]:
        return [k for k in self._active if k.startswith(prefix)]

    def expired_keys(self, now: datetime) -> list[str]:
        return [
            k for k, a in self._active.items() if a.expires_at is not None and a.expires_at <= now
        ]

    def list(self) -> list[Alert]:
        """Activas primero y después las resueltas, de la más reciente a la más antigua."""
        active = sorted(self._active.values(), key=lambda a: a.timestamp, reverse=True)
        return [*active, *reversed(self._history)]
