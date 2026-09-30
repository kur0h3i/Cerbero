"""Cabeza 3 — Accesos: intentos de acceso SSH en ``auth.log``.

Se sigue el fichero como ``tail -f`` (polling con ``seek``: nunca se relee
entero) en un hilo aparte, que encola las alertas en el bucle de asyncio con
``run_coroutine_threadsafe``. No depende de fail2ban.

- Fuerza bruta: más de ``BRUTE_FORCE_THRESHOLD`` fallos de login desde una
  misma IP en ``BRUTE_FORCE_WINDOW_MIN`` minutos. 🔴 si la IP es externa, 🟡
  si es de un rango de confianza. Se resuelve cuando pasa una ventana entera
  sin fallos desde esa IP.
- Login externo: un login aceptado desde fuera de los rangos de confianza 🔴.
  Es un evento sin "fin": queda activo un tiempo y se cierra solo.
- Intentos externos con usuario válido 🟡: una única alerta agregada (una
  botnet con cientos de IPs no manda cientos de mensajes).

Qué cuenta como fallo: cada línea ``Failed <método>`` (menos ``none``, que es
el sondeo inicial del cliente) y, si una conexión no tuvo ninguna, su cierre
en preautenticación con usuario (``Connection closed by authenticating user``),
que es lo único que deja un intento fallido con clave pública.

Formatos: Debian 13 trae OpenSSH 10, que registra como ``sshd-session`` en vez
de ``sshd``, y rsyslog usa fechas RFC 3339; se aceptan los dos formatos.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from ipaddress import IPv6Address, ip_address
from typing import Literal

from ..alerter import Alerter
from ..config import IPAddress, Settings
from ..store import Level

log = logging.getLogger(__name__)

HEAD = "accesos"

# --- Parser ----------------------------------------------------------------------

# Cabecera de syslog (fecha RFC 3339 o clásica) + host + proceso de sshd. Se
# ancla al principio de la línea para que otro programa no pueda colar texto
# que parezca de sshd en mitad de su propio mensaje.
_SSHD_LINE = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}T\S+|[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2})"
    r"\s\S+\ssshd(?:-session|-auth)?\[\d+\]:\s(?P<msg>.*)$"
)
# El usuario lo elige quien se conecta: puede contener " from 192.168.1.5 port 1".
# Por eso es codicioso (``.*``): se queda con la ÚLTIMA pareja "from IP port N",
# que es la que escribe sshd, y un usuario manipulado no puede falsear la IP.
_IP = r"(?P<ip>[0-9A-Fa-f:.]+)"
_TAIL = r"(?: ssh2(?:: .*)?)?$"
_ACCEPTED = re.compile(
    rf"^Accepted (?P<method>\S+) for (?P<user>.*) from {_IP} port (?P<port>\d+){_TAIL}"
)
_FAILED = re.compile(
    rf"^Failed (?P<method>\S+) for (?P<invalid>invalid user )?(?P<user>.*) from {_IP} "
    rf"port (?P<port>\d+){_TAIL}"
)
_CLOSED = re.compile(
    r"^(?:Connection closed by|Disconnected from|Disconnecting) "
    rf"(?P<kind>authenticating|invalid) user (?P<user>.*) {_IP} port (?P<port>\d+)"
    r"(?:: .*)? \[preauth\]$"
)

EventKind = Literal["accepted", "failed", "closed"]


@dataclass(frozen=True)
class SshEvent:
    kind: EventKind
    ip: IPAddress
    port: int
    user: str
    valid_user: bool
    method: str = ""


def _ip(text: str) -> IPAddress | None:
    try:
        ip = ip_address(text)
    except ValueError:
        return None
    if isinstance(ip, IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def parse_line(line: str) -> SshEvent | None:
    """Extrae el evento SSH de una línea de ``auth.log`` (o ``None`` si no interesa)."""
    head = _SSHD_LINE.match(line.rstrip("\r\n"))
    if head is None:
        return None
    msg = head["msg"]
    if m := _FAILED.match(msg):
        kind: EventKind = "failed"
        valid = m["invalid"] is None
    elif m := _ACCEPTED.match(msg):
        kind, valid = "accepted", True
    elif m := _CLOSED.match(msg):
        kind, valid = "closed", m["kind"] == "authenticating"
    else:
        return None
    ip = _ip(m["ip"])
    if ip is None:
        return None
    method = m.groupdict().get("method") or ""
    return SshEvent(kind, ip, int(m["port"]), m["user"], valid, method)


# --- Análisis --------------------------------------------------------------------


@dataclass(frozen=True)
class Fire:
    key: str
    level: Level
    message: str
    ttl: timedelta | None = None


@dataclass(frozen=True)
class Resolve:
    key: str
    message: str


Action = Fire | Resolve

# Una alerta en curso se actualiza como mucho cada tanto (el cooldown ya evita
# el spam; esto evita encolar una corrutina por cada línea de un ataque).
REFIRE_EVERY_S = 30.0
# Topes para acotar la memoria ante un escaneo masivo (Cerbero debe quedarse en
# < 80 MB justo cuando más falta hace): IPs seguidas a la vez (se descarta la que
# lleva más tiempo sin fallar), momentos guardados por IP (la cuenta satura, pero
# sigue por encima del umbral) y conexiones abiertas con fallos ya contados.
MAX_TRACKED_IPS = 2000
MAX_TIMES_PER_IP = 200
MAX_OPEN_CONNS = 2000
MAX_EXTERNAL_ATTEMPTS = 1000
USERS_SHOWN = 4
EXTERNAL_KEY = f"{HEAD}:intento_externo"


@dataclass(slots=True)
class _IpFailures:
    times: deque[float]
    users: list[str] = field(default_factory=list)
    total: int = 0


class AccessAnalyzer:
    """Convierte eventos SSH en alertas. Sin E/S: el tiempo llega como parámetro."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._window = settings.brute_force_window_min * 60
        self._failures: dict[IPAddress, _IpFailures] = {}
        # Conexiones (ip, puerto) que ya sumaron un fallo con una línea "Failed".
        self._counted_conns: dict[tuple[IPAddress, int], float] = {}
        self._external: deque[tuple[float, IPAddress, str]] = deque(maxlen=MAX_EXTERNAL_ATTEMPTS)
        self._raised: set[str] = set()
        self._last_fire: dict[str, float] = {}
        # Última actualización que el límite de REFIRE_EVERY_S retuvo; ``tick`` la
        # entrega para que el mensaje guardado (el que ve Dis) no se quede atrás.
        self._pending: dict[str, Fire] = {}
        ttl_min = max(settings.alert_cooldown_min, 5)
        self._login_ttl = timedelta(minutes=ttl_min)

    def feed(self, event: SshEvent, now: float) -> list[Action]:
        if event.kind == "accepted":
            return self._accepted(event)
        conn = (event.ip, event.port)
        if event.kind == "failed":
            if event.method == "none":
                return []
            self._counted_conns[conn] = now
            if len(self._counted_conns) > MAX_OPEN_CONNS:
                self._counted_conns.pop(next(iter(self._counted_conns)))
        elif self._counted_conns.pop(conn, None) is not None:
            return []  # el cierre de una conexión cuyos fallos ya contamos
        return self._failure(event, now)

    def _accepted(self, event: SshEvent) -> list[Action]:
        if self._settings.is_trusted(event.ip):
            return []
        return [
            Fire(
                f"{HEAD}:login_externo:{event.user}@{event.ip}",
                "critical",
                f"Login SSH aceptado desde IP externa {event.ip} "
                f"(usuario {event.user}, {event.method})",
                ttl=self._login_ttl,
            )
        ]

    def _failure(self, event: SshEvent, now: float) -> list[Action]:
        actions: list[Action] = []
        trusted = self._settings.is_trusted(event.ip)
        # Se reinserta al final: el orden del dict es de menos a más reciente.
        entry = self._failures.pop(event.ip, None)
        if entry is None:
            if len(self._failures) >= MAX_TRACKED_IPS:
                self._failures.pop(next(iter(self._failures)))
            maxlen = max(MAX_TIMES_PER_IP, self._settings.brute_force_threshold + 1)
            entry = _IpFailures(deque(maxlen=maxlen))
        self._failures[event.ip] = entry
        entry.times.append(now)
        entry.total += 1
        if event.user not in entry.users:
            entry.users = [*entry.users, event.user][-USERS_SHOWN:]
        self._prune(entry.times, now)

        count = len(entry.times)
        saturated = "+" if count == entry.times.maxlen else ""
        if count > self._settings.brute_force_threshold:
            key = f"{HEAD}:fuerza_bruta:{event.ip}"
            origin = "de confianza" if trusted else "externa"
            users = ", ".join(u or "(vacío)" for u in entry.users)
            actions += self._throttled(
                key,
                "warning" if trusted else "critical",
                f"Fuerza bruta SSH desde {event.ip} ({origin}): {count}{saturated} fallos en "
                f"{self._settings.brute_force_window_min:g} min (usuarios: {users})",
                now,
            )

        if event.valid_user and not trusted:
            self._external.append((now, event.ip, event.user))
            self._prune_external(now)
            ips = {ip for _, ip, _ in self._external}
            full = "+" if len(self._external) == self._external.maxlen else ""
            actions += self._throttled(
                EXTERNAL_KEY,
                "warning",
                f"Intentos SSH con usuario válido desde IPs externas: {len(self._external)}{full} "
                f"en {self._settings.brute_force_window_min:g} min desde {len(ips)} IP(s) "
                f"(último: {event.user} desde {event.ip})",
                now,
            )
        return actions

    def _throttled(self, key: str, level: Level, message: str, now: float) -> list[Action]:
        new = key not in self._raised
        fire = Fire(key, level, message)
        if not new and now - self._last_fire.get(key, 0.0) < REFIRE_EVERY_S:
            self._pending[key] = fire
            return []
        self._raised.add(key)
        self._last_fire[key] = now
        self._pending.pop(key, None)
        return [fire]

    def _prune(self, times: deque[float], now: float) -> None:
        while times and now - times[0] > self._window:
            times.popleft()

    def _prune_external(self, now: float) -> None:
        while self._external and now - self._external[0][0] > self._window:
            self._external.popleft()

    def tick(self, now: float) -> list[Action]:
        """Mantenimiento periódico: resuelve lo que ha cesado y libera memoria."""
        resolved: list[Action] = []
        for ip in list(self._failures):
            entry = self._failures[ip]
            self._prune(entry.times, now)
            if not entry.times:
                del self._failures[ip]
                message = f"Cesa la fuerza bruta SSH desde {ip} ({entry.total} fallos en total)"
                resolved += self._resolve(f"{HEAD}:fuerza_bruta:{ip}", message)
        self._prune_external(now)
        if not self._external:
            resolved += self._resolve(EXTERNAL_KEY, "Cesan los intentos SSH desde IPs externas")
        for conn, seen in list(self._counted_conns.items()):
            if now - seen > self._window:
                del self._counted_conns[conn]

        # Lo que retuvo el límite de actualizaciones, salvo lo que se acaba de resolver.
        updates: list[Action] = list(self._pending.values())
        for key in self._pending:
            self._last_fire[key] = now
        self._pending.clear()
        return [*updates, *resolved]

    def _resolve(self, key: str, message: str) -> list[Action]:
        if key not in self._raised:
            return []
        self._raised.discard(key)
        self._last_fire.pop(key, None)
        self._pending.pop(key, None)
        return [Resolve(key, message)]

    @property
    def tracked_ips(self) -> int:
        return len(self._failures)


# --- Tail del fichero ------------------------------------------------------------


class LogTailer:
    """Lee las líneas nuevas de un fichero de log, como ``tail -F``.

    - Al arrancar empieza por el final (no se reprocesa el histórico). Si el
      fichero aún no existe, cuando aparezca se lee desde el principio.
    - Rotación (logrotate crea un fichero nuevo): se termina de leer el viejo
      y se abre el nuevo desde el principio.
    - Truncado (``copytruncate``): se vuelve al principio.
    - Una línea a medio escribir se guarda hasta que llega su salto de línea.
    """

    CHUNK = 64 * 1024
    MAX_PARTIAL = 64 * 1024

    def __init__(self, path: str) -> None:
        self.path = path
        self._fh = None
        self._ino: tuple[int, int] | None = None
        self._partial = b""
        self._first_open = True
        self.error: str | None = None

    @property
    def is_open(self) -> bool:
        return self._fh is not None

    def _open(self) -> bool:
        try:
            fh = open(self.path, "rb")  # noqa: SIM115 - se mantiene abierto entre lecturas
        except OSError as exc:
            self.error = exc.strerror or type(exc).__name__
            self._first_open = False  # cuando aparezca, se lee entero
            return False
        st = os.fstat(fh.fileno())
        self._fh, self._ino, self._partial, self.error = fh, (st.st_dev, st.st_ino), b"", None
        if self._first_open:
            fh.seek(0, os.SEEK_END)
            self._first_open = False
        return True

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def _drain(self) -> list[str]:
        data = self._fh.read(self.CHUNK)
        if not data:
            return []
        data = self._partial + data
        *complete, self._partial = data.split(b"\n")
        if len(self._partial) > self.MAX_PARTIAL:
            self._partial = b""  # una "línea" sin fin no es un log de sshd
        return [raw.decode("utf-8", errors="replace") for raw in complete]

    def read(self) -> list[str]:
        """Devuelve las líneas completas nuevas (lista vacía si no hay)."""
        if self._fh is None and not self._open():
            return []
        lines = self._drain()
        if lines:
            return lines
        try:
            st = os.stat(self.path)
        except OSError:
            return []  # rotado y aún sin fichero nuevo: seguimos con el viejo
        if (st.st_dev, st.st_ino) != self._ino:
            self.close()
            self._first_open = False
            return self._drain() if self._open() else []
        if st.st_size < self._fh.tell():
            self._fh.seek(0)
            self._partial = b""
            return self._drain()
        return []


# --- Cabeza ----------------------------------------------------------------------


class AccesosHead:
    name = HEAD

    POLL_S = 1.0  # espera cuando no hay líneas nuevas
    TICK_S = 10.0  # cada cuánto se resuelven los ataques que han cesado
    LOG_KEY = f"{HEAD}:log"

    def __init__(
        self,
        settings: Settings,
        alerter: Alerter,
        tailer: LogTailer | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._alerter = alerter
        self._tailer = tailer or LogTailer(settings.host_path(settings.auth_log))
        self._analyzer = AccessAnalyzer(settings)
        self._clock = clock
        self._thread: threading.Thread | None = None
        self.lines_seen = 0

    def healthy(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._tailer.is_open

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        stop = threading.Event()
        self._thread = threading.Thread(
            target=self._worker, args=(loop, stop), name="cerbero-accesos", daemon=True
        )
        self._thread.start()
        try:
            await asyncio.Event().wait()  # hasta que cancelen la tarea
        finally:
            stop.set()
            await asyncio.to_thread(self._thread.join, 5)
            self._tailer.close()

    def _worker(self, loop: asyncio.AbstractEventLoop, stop: threading.Event) -> None:
        log.info("Vigilando %s", self._tailer.path)
        last_tick = self._clock()
        log_problem: str | None = None
        while not stop.is_set():
            try:
                lines = self._tailer.read()
                now = self._clock()
                for line in lines:
                    self.lines_seen += 1
                    if event := parse_line(line):
                        self._dispatch(loop, self._analyzer.feed(event, now))
                if now - last_tick >= self.TICK_S:
                    last_tick = now
                    self._dispatch(loop, self._analyzer.tick(now))
                log_problem = self._check_log(loop, log_problem)
            except Exception:
                log.exception("Error procesando %s", self._tailer.path)
                lines = []
            if not lines:
                stop.wait(self.POLL_S)

    def _check_log(self, loop: asyncio.AbstractEventLoop, previous: str | None) -> str | None:
        """Avisa (una vez) si el log no se puede leer, y cuando vuelve a leerse."""
        problem = self._tailer.error if not self._tailer.is_open else None
        if problem == previous:
            return problem
        if problem:
            hint = " (¿está instalado rsyslog?)" if "No such file" in problem else ""
            self._dispatch(
                loop,
                [
                    Fire(
                        self.LOG_KEY,
                        "warning",
                        f"No se puede leer {self._settings.auth_log}: {problem}{hint}. "
                        "La vigilancia de accesos SSH está ciega",
                    )
                ],
            )
        else:
            self._dispatch(loop, [Resolve(self.LOG_KEY, f"{self._settings.auth_log} legible")])
        return problem

    def _dispatch(self, loop: asyncio.AbstractEventLoop, actions: list[Action]) -> None:
        for action in actions:
            if isinstance(action, Fire):
                coro = self._alerter.fire(
                    action.key, HEAD, action.level, action.message, ttl=action.ttl
                )
            else:
                coro = self._alerter.resolve(action.key, action.message)
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            future.add_done_callback(_log_failure)


def _log_failure(future) -> None:
    if not future.cancelled() and (exc := future.exception()) is not None:
        log.error("No se pudo registrar una alerta de accesos: %r", exc)
