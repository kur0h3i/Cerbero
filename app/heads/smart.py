"""Cabeza 4 — Salud de discos: SMART.

Leer SMART necesita root y Cerbero no lo tiene, así que lo lee en el host
``cerbero-smart.timer`` (ver ``host/smart/``): cada hora deja un JSON por disco
en ``/var/lib/cerbero/smart/``. Esta cabeza solo lee esos ficheros (a través
de ``HOST_ROOT``) y decide qué avisar:

- 🔴 SMART ``FAILED`` o, en NVMe, un aviso crítico.
- 🔴 Sectores pendientes o incorregibles, reasignados que suben (se mantiene
  24 h tras la última subida) o errores de medio en NVMe.
- 🟡 Reasignados estables: un disco con unos pocos de hace años no merece un
  🔴 cada media hora.
- 🟡 Temperatura alta, desgaste del NVMe ≥ 90 %, SMART ilegible, un disco no
  opcional desconectado, o datos sin actualizar (el timer se ha parado).

Un disco ``opcional`` desconectado no es una alerta. Uno en reposo
(``standby``) conserva los datos de su última lectura completa.
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
from ..store import Level

log = logging.getLogger(__name__)

HEAD = "smart"
POLL_S = 120
# Un aumento de reasignados mantiene la alerta en 🔴 este tiempo.
RISE_STICKY = timedelta(hours=24)
TEMP_RESOLVE_MARGIN = 3.0
WEAR_WARNING_PCT = 90
# Atributos ATA: reasignados, pendientes e incorregibles.
ATTR_REALLOCATED, ATTR_PENDING, ATTR_UNCORRECTABLE = 5, 197, 198


@dataclass
class DiskHealth:
    nombre: str
    estado: str  # ok | standby | ausente | error
    dispositivo: str = ""
    montaje: str | None = None
    opcional: bool = False
    comprobado: datetime | None = None
    leido: datetime | None = None
    error: str | None = None
    modelo: str | None = None
    protocolo: str | None = None  # "ATA" o "NVMe"
    salud: bool | None = None  # smart_status.passed
    temp_c: float | None = None
    horas: int | None = None
    reasignados: int | None = None
    pendientes: int | None = None
    incorregibles: int | None = None
    aviso_critico: int | None = None  # NVMe
    errores_medio: int | None = None  # NVMe
    reserva_pct: int | None = None  # NVMe
    desgaste_pct: int | None = None  # NVMe

    @property
    def nvme(self) -> bool:
        return (self.protocolo or "").lower() == "nvme"

    @property
    def etiqueta(self) -> str:
        return f"{self.nombre} ({self.montaje})" if self.montaje else self.nombre


def _time(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_disk(name: str, payload: dict) -> DiskHealth:
    """Convierte el JSON que deja ``cerbero_smart.py`` en un ``DiskHealth``."""
    head = payload.get("cerbero") or {}
    data = payload.get("smartctl") or {}
    disk = DiskHealth(
        nombre=str(head.get("nombre") or name),
        estado=str(head.get("estado") or "error"),
        dispositivo=str(head.get("dispositivo") or ""),
        montaje=head.get("montaje") or None,
        opcional=bool(head.get("opcional")),
        comprobado=_time(head.get("comprobado")),
        leido=_time(head.get("leido")),
        error=head.get("error") or None,
        modelo=data.get("model_name"),
        protocolo=(data.get("device") or {}).get("protocol"),
    )
    status = data.get("smart_status") or {}
    if isinstance(status.get("passed"), bool):
        disk.salud = status["passed"]
    temp = (data.get("temperature") or {}).get("current")
    disk.temp_c = temp if isinstance(temp, int | float) else None
    disk.horas = _int((data.get("power_on_time") or {}).get("hours"))
    attrs = {
        a.get("id"): _int((a.get("raw") or {}).get("value"))
        for a in (data.get("ata_smart_attributes") or {}).get("table") or []
        if isinstance(a, dict)
    }
    disk.reasignados = attrs.get(ATTR_REALLOCATED)
    disk.pendientes = attrs.get(ATTR_PENDING)
    disk.incorregibles = attrs.get(ATTR_UNCORRECTABLE)
    nvme = data.get("nvme_smart_health_information_log") or {}
    if nvme:
        disk.aviso_critico = _int(nvme.get("critical_warning"))
        disk.errores_medio = _int(nvme.get("media_errors"))
        disk.reserva_pct = _int(nvme.get("available_spare"))
        disk.desgaste_pct = _int(nvme.get("percentage_used"))
    return disk


def read_dir(path: str) -> list[DiskHealth]:
    """Lee todos los ``<disco>.json`` de la carpeta (bloqueante: usar desde un hilo)."""
    disks = []
    for entry in sorted(os.scandir(path), key=lambda e: e.name):
        if entry.name.startswith(".") or not entry.name.endswith(".json"):
            continue  # los temporales de la escritura atómica empiezan por punto
        name = entry.name.removesuffix(".json")
        try:
            with open(entry.path, encoding="utf-8") as fh:
                payload = json.load(fh)
            disks.append(parse_disk(name, payload if isinstance(payload, dict) else {}))
        except (OSError, ValueError) as exc:
            disks.append(DiskHealth(name, "error", error=f"JSON ilegible: {exc}"))
    return disks


class SmartHead:
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
        self._dir = settings.host_path(settings.smart_dir)
        self._max_age = timedelta(hours=settings.smart_max_age_h)
        # Reasignados de la lectura anterior y cuándo subieron por última vez.
        self._reallocated: dict[str, int] = {}
        self._rose_at: dict[str, datetime] = {}
        self._fresh = False
        self._last_ok: float | None = None
        self.snapshot: list[DiskHealth] | None = None

    def healthy(self) -> bool:
        if self._last_ok is None or not self._fresh:
            return False
        return time.monotonic() - self._last_ok < 3 * POLL_S

    async def run(self) -> None:
        while True:
            try:
                await self.check()
            except Exception:
                log.exception("Error revisando SMART")
            await asyncio.sleep(POLL_S)

    async def check(self) -> None:
        key = f"{HEAD}:sin_datos"
        try:
            disks = await asyncio.to_thread(read_dir, self._dir)
        except OSError as exc:
            disks, reason = [], exc.strerror or type(exc).__name__
        else:
            reason = "no hay ningún disco" if not disks else None
        if reason:
            self.snapshot, self._fresh = [], False
            await self._alerter.fire(
                key,
                HEAD,
                "warning",
                f"Sin datos SMART en {self._settings.smart_dir}: {reason}. "
                "¿Está instalado cerbero-smart.timer? (host/smart/instalar.sh)",
            )
            self._last_ok = time.monotonic()
            return
        if self._alerter.is_active(key):
            await self._alerter.resolve(key, "Llegan datos SMART")
        self.snapshot = disks
        await self.evaluate(disks)
        self._last_ok = time.monotonic()

    async def evaluate(self, disks: list[DiskHealth]) -> None:
        now = self._clock()
        await self._check_stale(disks, now)
        for disk in disks:
            await self._check_presence(disk)
            if disk.estado == "ausente":
                continue
            await self._check_readable(disk)
            if disk.salud is None:
                continue  # aún sin ninguna lectura completa
            await self._check_failed(disk)
            await self._check_sectors(disk, now)
            await self._check_wear(disk)
            await self._check_temperature(disk)

    async def _check_stale(self, disks: list[DiskHealth], now: datetime) -> None:
        key = f"{HEAD}:antiguo"
        stale = [d for d in disks if d.comprobado is None or now - d.comprobado > self._max_age]
        self._fresh = not stale
        if stale:
            oldest = [now - d.comprobado for d in stale if d.comprobado]
            hours = f"hace {max(oldest).total_seconds() / 3600:.0f} h" if oldest else "nunca"
            names = ", ".join(d.nombre for d in stale)
            await self._alerter.fire(
                key,
                HEAD,
                "warning",
                f"Datos SMART sin actualizar ({hours}) en: {names}. "
                "¿Se ha parado cerbero-smart.timer?",
            )
        elif self._alerter.is_active(key):
            await self._alerter.resolve(key, "Los datos SMART vuelven a actualizarse")

    async def _check_presence(self, disk: DiskHealth) -> None:
        key = f"{HEAD}:ausente:{disk.nombre}"
        if disk.estado == "ausente" and not disk.opcional:
            await self._alerter.fire(
                key, HEAD, "warning", f"Disco {disk.etiqueta} no está conectado"
            )
        elif self._alerter.is_active(key):
            await self._alerter.resolve(key, f"Disco {disk.etiqueta} conectado de nuevo")

    async def _check_readable(self, disk: DiskHealth) -> None:
        key = f"{HEAD}:error:{disk.nombre}"
        if disk.estado == "error":
            await self._alerter.fire(
                key,
                HEAD,
                "warning",
                f"No se puede leer SMART de {disk.etiqueta}: {disk.error or 'sin detalle'}",
            )
        elif self._alerter.is_active(key):
            await self._alerter.resolve(key, f"SMART de {disk.etiqueta} legible de nuevo")

    async def _check_failed(self, disk: DiskHealth) -> None:
        key = f"{HEAD}:fallo:{disk.nombre}"
        reasons = []
        if disk.salud is False:
            reasons.append("SMART FAILED")
        if disk.aviso_critico:
            spare = f", reserva {disk.reserva_pct} %" if disk.reserva_pct is not None else ""
            reasons.append(f"aviso crítico NVMe 0x{disk.aviso_critico:02x}{spare}")
        if reasons:
            model = f" — {disk.modelo}" if disk.modelo else ""
            await self._alerter.fire(
                key, HEAD, "critical", f"Disco {disk.etiqueta}{model}: {', '.join(reasons)}"
            )
        elif self._alerter.is_active(key):
            await self._alerter.resolve(key, f"Disco {disk.etiqueta}: SMART correcto")

    async def _check_sectors(self, disk: DiskHealth, now: datetime) -> None:
        key = f"{HEAD}:sectores:{disk.nombre}"
        realloc = disk.reasignados or 0
        previous = self._reallocated.get(disk.nombre)
        if previous is not None and realloc > previous:
            self._rose_at[disk.nombre] = now
        self._reallocated[disk.nombre] = realloc
        rose_at = self._rose_at.get(disk.nombre)
        rising_recently = rose_at is not None and now - rose_at < RISE_STICKY

        parts = []
        if realloc:
            parts.append(f"{realloc} reasignados" + (" (subiendo)" if rising_recently else ""))
        if disk.pendientes:
            parts.append(f"{disk.pendientes} pendientes")
        if disk.incorregibles:
            parts.append(f"{disk.incorregibles} incorregibles")
        if disk.errores_medio:
            parts.append(f"{disk.errores_medio} errores de medio")
        if not parts:
            if self._alerter.is_active(key):
                await self._alerter.resolve(key, f"Disco {disk.etiqueta}: sin sectores dañados")
            return
        serious = bool(disk.pendientes or disk.incorregibles or disk.errores_medio)
        level: Level = "critical" if serious or rising_recently else "warning"
        await self._alerter.fire(key, HEAD, level, f"Disco {disk.etiqueta}: {', '.join(parts)}")

    async def _check_wear(self, disk: DiskHealth) -> None:
        key = f"{HEAD}:desgaste:{disk.nombre}"
        if disk.desgaste_pct is not None and disk.desgaste_pct >= WEAR_WARNING_PCT:
            await self._alerter.fire(
                key,
                HEAD,
                "warning",
                f"Disco {disk.etiqueta}: desgaste del {disk.desgaste_pct} % de su vida útil",
            )
        elif self._alerter.is_active(key):
            await self._alerter.resolve(key, f"Disco {disk.etiqueta}: desgaste normal")

    async def _check_temperature(self, disk: DiskHealth) -> None:
        key = f"{HEAD}:temperatura:{disk.nombre}"
        if disk.estado != "ok" or disk.temp_c is None:
            # En reposo la temperatura guardada es vieja; un disco parado no se calienta.
            if disk.estado == "standby" and self._alerter.is_active(key):
                await self._alerter.resolve(key, f"Disco {disk.etiqueta} en reposo")
            return
        s = self._settings
        limit = s.smart_temp_nvme_max if disk.nvme else s.smart_temp_hdd_max
        if disk.temp_c > limit:
            await self._alerter.fire(
                key,
                HEAD,
                "warning",
                f"Disco {disk.etiqueta} a {disk.temp_c:.0f} °C (umbral {limit:.0f} °C)",
            )
        elif disk.temp_c <= limit - TEMP_RESOLVE_MARGIN and self._alerter.is_active(key):
            await self._alerter.resolve(key, f"Disco {disk.etiqueta} a {disk.temp_c:.0f} °C")
