"""Tareas del host: timers de systemd que dejan un informe para Cerbero.

Cada tarea escribe su último resultado en ``/var/lib/cerbero/tareas/<tarea>.json``
(formato en ``host/tareas/informe_tarea.py``); esta cabeza lo lee a través de
``HOST_ROOT``. Sirve para cualquier tarea: hoy la limpieza semanal de imágenes
y la cola de tareas del vault; mañana, el backup.

- 🟡 la tarea ha fallado (se resuelve con el siguiente informe correcto).
- 🟡 la tarea no se ejecuta: su informe es más viejo que su periodo por
  ``TAREAS_TOLERANCIA`` (el timer se ha parado o la unidad ya no existe).
- 🟢 si el informe lo pide (``avisar``), su resumen por Telegram una vez por
  ejecución: «🧹 Liberados 566 MB». Los informes que ya estaban al arrancar
  Cerbero no se reenvían.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..alerter import Alerter
from ..config import Settings

log = logging.getLogger(__name__)

HEAD = "tareas"
POLL_S = 60


@dataclass
class TaskReport:
    tarea: str
    resultado: str  # ok | fallo
    fin: datetime | None
    cada_s: int
    resumen: str = ""
    avisar: bool = False
    error: str | None = None  # el informe no se pudo leer

    @property
    def ok(self) -> bool:
        return self.resultado == "ok"


def parse_report(name: str, payload: dict) -> TaskReport:
    try:
        fin = datetime.fromisoformat(str(payload.get("fin")))
    except ValueError:
        fin = None
    every = payload.get("cada_s")
    return TaskReport(
        tarea=str(payload.get("tarea") or name),
        resultado="ok" if payload.get("resultado") == "ok" else "fallo",
        fin=fin,
        cada_s=every if isinstance(every, int) and every > 0 else 0,
        resumen=str(payload.get("resumen") or ""),
        avisar=bool(payload.get("avisar")),
    )


def read_dir(path: str) -> list[TaskReport]:
    """Lee los informes de la carpeta (bloqueante: usar desde un hilo)."""
    reports = []
    for entry in sorted(os.scandir(path), key=lambda e: e.name):
        if entry.name.startswith(".") or not entry.name.endswith(".json"):
            continue  # temporales de la escritura atómica
        name = entry.name.removesuffix(".json")
        try:
            with open(entry.path, encoding="utf-8") as fh:
                payload = json.load(fh)
            reports.append(parse_report(name, payload if isinstance(payload, dict) else {}))
        except (OSError, ValueError) as exc:
            reports.append(TaskReport(name, "fallo", None, 0, error=f"informe ilegible: {exc}"))
    return reports


def _ago(delta: timedelta) -> str:
    minutes = delta.total_seconds() / 60
    if minutes < 90:
        return f"{minutes:.0f} min"
    hours = minutes / 60
    return f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.0f} días"


class TareasHead:
    name = HEAD

    def __init__(
        self,
        settings: Settings,
        alerter: Alerter,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._alerter = alerter
        self._clock = clock or settings.now
        self._dir = settings.host_path(settings.tareas_dir)
        # Último informe visto de cada tarea (por su ``fin``); ``None`` hasta la
        # primera lectura, que solo sirve de referencia.
        self._seen: dict[str, datetime | None] | None = None
        self._readable = False
        self._last_ok: float | None = None
        self.snapshot: list[TaskReport] | None = None

    def healthy(self) -> bool:
        if self._last_ok is None or not self._readable:
            return False
        return time.monotonic() - self._last_ok < 3 * POLL_S

    def is_stale(self, report: TaskReport, now: datetime) -> bool:
        if report.fin is None:
            return True
        if not report.cada_s:
            return False
        limit = timedelta(seconds=report.cada_s * self._settings.tareas_tolerancia)
        return now - report.fin > limit

    async def run(self) -> None:
        while True:
            try:
                await self.check()
            except Exception:
                log.exception("Error revisando las tareas")
            await asyncio.sleep(POLL_S)

    async def check(self) -> None:
        key = f"{HEAD}:sin_datos"
        try:
            reports = await asyncio.to_thread(read_dir, self._dir)
        except OSError as exc:
            self.snapshot, self._readable = [], False
            await self._alerter.fire(
                key,
                HEAD,
                "warning",
                f"No se pueden leer los informes de tareas en {self._settings.tareas_dir}: "
                f"{exc.strerror or type(exc).__name__}. ¿Está instalado host/tareas/?",
            )
            self._last_ok = time.monotonic()
            return
        self._readable = True
        if self._alerter.is_active(key):
            await self._alerter.resolve(key, "Informes de tareas legibles")
        self.snapshot = reports
        await self.evaluate(reports)
        self._last_ok = time.monotonic()

    async def evaluate(self, reports: list[TaskReport]) -> None:
        now = self._clock()
        first = self._seen is None
        seen = self._seen or {}
        for report in reports:
            new = not first and report.fin is not None and report.fin != seen.get(report.tarea)
            seen[report.tarea] = report.fin
            await self._check_failed(report)
            await self._check_stale(report, now)
            if new and report.ok and report.avisar and report.resumen:
                await self._alerter.notify(f"{HEAD}:aviso:{report.tarea}", HEAD, report.resumen)
        self._seen = seen

    async def _check_failed(self, report: TaskReport) -> None:
        key = f"{HEAD}:fallo:{report.tarea}"
        if not report.ok:
            reason = report.error or report.resumen or "sin detalle"
            await self._alerter.fire(
                key, HEAD, "warning", f"La tarea {report.tarea} ha fallado: {reason}"
            )
        elif self._alerter.is_active(key):
            await self._alerter.resolve(key, f"La tarea {report.tarea} vuelve a funcionar")

    async def _check_stale(self, report: TaskReport, now: datetime) -> None:
        key = f"{HEAD}:parada:{report.tarea}"
        if report.error:
            return  # ya avisa el fallo
        if self.is_stale(report, now):
            last = f"hace {_ago(now - report.fin)}" if report.fin else "nunca"
            every = (
                f" (debería cada {_ago(timedelta(seconds=report.cada_s))})" if report.cada_s else ""
            )
            await self._alerter.fire(
                key,
                HEAD,
                "warning",
                f"La tarea {report.tarea} no se ejecuta: último informe {last}{every}",
            )
        elif self._alerter.is_active(key):
            await self._alerter.resolve(key, f"La tarea {report.tarea} vuelve a ejecutarse")
