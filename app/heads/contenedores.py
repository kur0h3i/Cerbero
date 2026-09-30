"""Cabeza 2 — Contenedores: estado de Docker con el SDK oficial.

Solo lectura: Cerbero lista e inspecciona contenedores, nunca los toca.

- Caída: un contenedor que Cerbero ha visto en marcha pasa a ``exited`` o
  ``dead``. Los que ya estaban parados al arrancar Cerbero no avisan. Una
  parada a mano (hubo un evento ``stop`` de Docker: ``docker stop``,
  ``compose stop``/``down``...) o una salida limpia (código 0 o 143) es 🟡;
  cualquier otra, o si el kernel lo mató por falta de memoria, es 🔴. El
  código no basta: un proceso que ignora SIGTERM sale con 137 también en un
  ``docker stop``.
- Bucle de reinicios: más de ``RESTART_LOOP_THRESHOLD`` reinicios (el
  ``RestartCount`` que lleva Docker) en ``RESTART_LOOP_WINDOW_MIN`` minutos.
- Healthcheck: un contenedor en marcha marcado ``unhealthy``.
- Docker inaccesible: si el socket no responde dos lecturas seguidas.

``CONTAINERS_IGNORE`` excluye contenedores por nombre (p. ej. tareas puntuales).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ..alerter import Alerter
from ..config import Settings
from ..store import Level

log = logging.getLogger(__name__)

HEAD = "contenedores"
DOWN_STATES = {"exited", "dead"}
# Salidas limpias: 0 (terminó bien) y 143 (128 + SIGTERM, lo que manda ``docker stop``).
CLEAN_EXIT_CODES = {0, 143}
# Lecturas fallidas seguidas antes de avisar de que Docker no responde.
DOCKER_FAILURES_TO_ALERT = 2
HEALTH_OUTPUT_MAX = 160


@dataclass
class ContainerInfo:
    name: str
    id: str
    state: str
    exit_code: int = 0
    oom_killed: bool = False
    restart_count: int = 0
    health: str | None = None
    health_output: str = ""
    failing_streak: int = 0


def parse_inspect(data: dict) -> ContainerInfo:
    """Convierte la respuesta de ``docker inspect`` en un ``ContainerInfo``."""
    state = data.get("State") or {}
    health = state.get("Health") or {}
    entries = health.get("Log") or []
    output = (entries[-1].get("Output") or "") if entries else ""
    return ContainerInfo(
        name=(data.get("Name") or "").lstrip("/"),
        id=data.get("Id") or "",
        state=state.get("Status") or "unknown",
        exit_code=int(state.get("ExitCode") or 0),
        oom_killed=bool(state.get("OOMKilled")),
        restart_count=int(data.get("RestartCount") or 0),
        health=health.get("Status"),
        health_output=" ".join(output.split())[:HEALTH_OUTPUT_MAX],
        failing_streak=int(health.get("FailingStreak") or 0),
    )


class ContainerSource(Protocol):
    def list(self) -> list[ContainerInfo]: ...

    def manual_stops(self, since: float, until: float) -> set[str]: ...


class DockerSource:
    """Lee los contenedores del socket de Docker (bloqueante: usar desde un hilo).

    El cliente se crea al primer uso y se descarta si falla, para reconectar
    solo cuando Docker vuelva.
    """

    TIMEOUT_S = 10

    def __init__(self) -> None:
        self._client = None

    def list(self) -> list[ContainerInfo]:
        import docker  # import perezoso: solo cuesta memoria si se usa

        try:
            if self._client is None:
                self._client = docker.from_env(timeout=self.TIMEOUT_S)
            api = self._client.api
            infos = []
            for summary in api.containers(all=True):
                try:
                    infos.append(parse_inspect(api.inspect_container(summary["Id"])))
                except docker.errors.NotFound:
                    continue  # borrado entre el listado y la inspección
            return infos
        except Exception:
            self.close()
            raise

    def manual_stops(self, since: float, until: float) -> set[str]:
        """Contenedores con un evento ``stop`` (parada pedida a Docker) en el intervalo."""
        if self._client is None:
            return set()
        try:
            events = self._client.api.events(
                since=int(since) - 1,
                until=int(until) + 1,
                filters={"type": "container", "event": "stop"},
                decode=True,
            )
            return {e.get("Actor", {}).get("Attributes", {}).get("name", "") for e in events}
        except Exception as exc:
            log.warning("No se pudieron leer los eventos de Docker: %s", exc)
            return set()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


class ContenedoresHead:
    name = HEAD

    def __init__(
        self,
        settings: Settings,
        alerter: Alerter,
        source: ContainerSource | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._alerter = alerter
        self._source = source or DockerSource()
        self._clock = clock
        self._ignore = set(settings.containers_ignore)
        self._window_s = settings.restart_loop_window_min * 60
        # Estado de la lectura anterior; ``None`` hasta la primera (la de referencia).
        self._known: dict[str, ContainerInfo] | None = None
        # Momentos de los reinicios vistos, por contenedor.
        self._restarts: dict[str, deque[float]] = {}
        self._failures = 0
        self._last_poll: float | None = None  # hora de pared, para pedir eventos
        self._last_ok: float | None = None
        self.snapshot: list[ContainerInfo] | None = None

    def healthy(self) -> bool:
        if self._last_ok is None:
            return False
        return time.monotonic() - self._last_ok < 3 * self._settings.poll_interval_s

    async def run(self) -> None:
        while True:
            try:
                await self.check()
            except Exception:
                log.exception("Error revisando los contenedores")
            await asyncio.sleep(self._settings.poll_interval_s)

    async def check(self) -> None:
        key = f"{HEAD}:docker"
        try:
            infos = await asyncio.to_thread(self._source.list)
            now = time.time()
            stops: set[str] = set()
            if self._last_poll is not None:
                stops = await asyncio.to_thread(self._source.manual_stops, self._last_poll, now)
            self._last_poll = now
        except Exception as exc:
            self._failures += 1
            reason = " ".join(str(exc).split())[:200] or type(exc).__name__
            log.warning("No se puede consultar Docker: %s", reason)
            if self._failures >= DOCKER_FAILURES_TO_ALERT:
                await self._alerter.fire(
                    key, HEAD, "critical", f"No se puede consultar Docker: {reason}"
                )
            return
        self._failures = 0
        if self._alerter.is_active(key):
            await self._alerter.resolve(key, "Docker responde de nuevo")
        self.snapshot = sorted(infos, key=lambda i: i.name)
        await self.evaluate(infos, stops)
        self._last_ok = time.monotonic()

    async def evaluate(
        self, infos: list[ContainerInfo], manual_stops: set[str] | frozenset[str] = frozenset()
    ) -> None:
        current = {i.name: i for i in infos if i.name not in self._ignore}
        baseline = self._known is None
        known = self._known or {}
        now = self._clock()
        for name, info in current.items():
            previous = None if baseline else known.get(name)
            if not baseline:
                await self._check_down(info, previous, info.name in manual_stops)
            self._check_restarts_count(info, previous, now)
            await self._check_restart_loop(name)
            await self._check_health(info)
        for name in known.keys() - current.keys():
            await self._forget(name)
        self._known = current

    async def _check_down(
        self, info: ContainerInfo, previous: ContainerInfo | None, manual: bool
    ) -> None:
        key = f"{HEAD}:caido:{info.name}"
        if info.state in DOWN_STATES:
            # Solo cuenta si lo vimos en marcha: los parados desde antes no avisan.
            fell = previous is not None and previous.state not in DOWN_STATES
            alert = self._alerter.store.get(key)
            if fell or (alert is not None and alert.level == "critical"):
                # Mientras siga caído, las críticas se recuerdan (tras el cooldown);
                # una parada limpia se avisa una vez.
                level, message = _down_alert(info, manual) if fell else (alert.level, alert.message)
                await self._alerter.fire(key, HEAD, level, message)
        elif info.state == "running" and self._alerter.is_active(key):
            await self._alerter.resolve(key, f"Contenedor {info.name} en marcha de nuevo")

    def _check_restarts_count(
        self, info: ContainerInfo, previous: ContainerInfo | None, now: float
    ) -> None:
        times = self._restarts.setdefault(info.name, deque())
        # Si el id cambia, el contenedor se ha recreado y la cuenta empieza de cero.
        if previous is not None and previous.id == info.id:
            times.extend([now] * max(0, info.restart_count - previous.restart_count))
        while times and now - times[0] > self._window_s:
            times.popleft()

    async def _check_restart_loop(self, name: str) -> None:
        s = self._settings
        key = f"{HEAD}:reinicios:{name}"
        count = len(self._restarts.get(name, ()))
        if count > s.restart_loop_threshold:
            await self._alerter.fire(
                key,
                HEAD,
                "critical",
                f"Contenedor {name} en bucle de reinicios: {count} en "
                f"{s.restart_loop_window_min:g} min (umbral {s.restart_loop_threshold})",
            )
        elif count == 0 and self._alerter.is_active(key):
            await self._alerter.resolve(
                key,
                f"Contenedor {name} estable: sin reinicios en {s.restart_loop_window_min:g} min",
            )

    async def _check_health(self, info: ContainerInfo) -> None:
        key = f"{HEAD}:unhealthy:{info.name}"
        if info.state != "running":
            return  # si está caído, ya avisa la alerta de caída
        if info.health == "unhealthy":
            detail = f": {info.health_output}" if info.health_output else ""
            await self._alerter.fire(
                key,
                HEAD,
                "warning",
                f"Contenedor {info.name} unhealthy "
                f"({info.failing_streak} comprobaciones fallidas seguidas){detail}",
            )
        elif info.health != "starting" and self._alerter.is_active(key):
            await self._alerter.resolve(key, f"Contenedor {info.name} healthy de nuevo")

    async def _forget(self, name: str) -> None:
        """El contenedor ya no existe: cierra sus alertas."""
        self._restarts.pop(name, None)
        for kind in ("caido", "reinicios", "unhealthy"):
            key = f"{HEAD}:{kind}:{name}"
            if self._alerter.is_active(key):
                await self._alerter.resolve(key, f"Contenedor {name} eliminado")


def _down_alert(info: ContainerInfo, manual: bool) -> tuple[Level, str]:
    if info.oom_killed:
        return (
            "critical",
            f"Contenedor {info.name} caído: sin memoria (OOM, código {info.exit_code})",
        )
    if manual:
        return (
            "warning",
            f"Contenedor {info.name} parado a mano (docker stop, código {info.exit_code})",
        )
    if info.exit_code in CLEAN_EXIT_CODES:
        return "warning", f"Contenedor {info.name} parado (salida limpia, código {info.exit_code})"
    return "critical", f"Contenedor {info.name} caído (código {info.exit_code})"
