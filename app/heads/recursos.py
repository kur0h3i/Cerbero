"""Cabeza 1 — Recursos: CPU, RAM y discos del host con psutil.

Dentro de Docker, ``/proc/stat`` y ``/proc/meminfo`` ya son los del host (no
están aislados por namespace), así que CPU y RAM no necesitan ``pid: host``.
Los discos se miden a través de la raíz del host montada en ``HOST_ROOT``.

- CPU: la media desde la lectura anterior (todo el intervalo de polling) por
  encima de ``CPU_THRESHOLD`` durante ``CPU_SUSTAINED`` lecturas seguidas.
- RAM: la memoria *disponible* (MemAvailable, incluye la caché que el kernel
  puede liberar) por debajo de ``RAM_FREE_MIN_GB``. La "libre" a secas no
  cuenta la caché y daría falsos críticos.
- Discos: uso por encima de ``DISK_THRESHOLD`` en cada punto de montaje; y
  aviso si uno no está montado (se estaría midiendo, y llenando, el raíz).

Para dar una alerta por resuelta se exige un pequeño margen por debajo del
umbral, y así no va y viene con cada lectura.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

import psutil

from ..alerter import Alerter
from ..config import Settings
from ..store import Level

log = logging.getLogger(__name__)

GB = 1024**3
HEAD = "recursos"

CPU_RESOLVE_MARGIN = 5.0  # puntos por debajo de CPU_THRESHOLD
RAM_RESOLVE_MARGIN_GB = 0.25  # GB por encima de RAM_FREE_MIN_GB
DISK_RESOLVE_MARGIN = 2.0  # puntos por debajo de DISK_THRESHOLD
DISK_CRITICAL = 95.0  # a partir de aquí un disco lleno es crítico
# Si la lectura anterior de CPU es tan reciente, la media no significa nada.
CPU_MIN_SPAN_S = 0.5


@dataclass
class DiskSample:
    mount: str
    total_gb: float = 0.0
    used_gb: float = 0.0
    free_gb: float = 0.0
    percent: float = 0.0
    # Motivo por el que no se ha podido medir (no existe, no está montado...).
    error: str | None = None


@dataclass
class Sample:
    cpu_pct: float
    ram_free_gb: float
    ram_total_gb: float
    disks: list[DiskSample] = field(default_factory=list)


class Sampler:
    """Lee CPU, RAM y discos (llamadas bloqueantes: usar desde un hilo)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        psutil.cpu_percent(interval=None)  # la primera llamada solo fija la referencia
        self._last_cpu = time.monotonic()

    def sample(self) -> Sample:
        span = time.monotonic() - self._last_cpu
        cpu = psutil.cpu_percent(interval=CPU_MIN_SPAN_S if span < CPU_MIN_SPAN_S else None)
        self._last_cpu = time.monotonic()
        mem = psutil.virtual_memory()
        return Sample(
            cpu_pct=round(cpu, 1),
            ram_free_gb=round(mem.available / GB, 2),
            ram_total_gb=round(mem.total / GB, 2),
            disks=[self.disk(mount) for mount in self._settings.disks],
        )

    def disk(self, mount: str) -> DiskSample:
        path = self._settings.host_path(mount)
        if not os.path.isdir(path):
            return DiskSample(mount, error="no existe")
        # Un punto de montaje sin su disco es un directorio vacío del raíz:
        # medirlo daría el uso del raíz y lo que se escriba ahí lo llenaría.
        if mount.rstrip("/") and not os.path.ismount(path):
            return DiskSample(mount, error="no está montado")
        try:
            usage = psutil.disk_usage(path)
        except OSError as exc:
            return DiskSample(mount, error=exc.strerror or type(exc).__name__)
        return DiskSample(
            mount,
            total_gb=round(usage.total / GB, 1),
            used_gb=round(usage.used / GB, 1),
            free_gb=round(usage.free / GB, 1),
            percent=round(usage.percent, 1),
        )


class RecursosHead:
    name = HEAD

    def __init__(
        self, settings: Settings, alerter: Alerter, sampler: Sampler | None = None
    ) -> None:
        self._settings = settings
        self._alerter = alerter
        self._sampler = sampler or Sampler(settings)
        self._cpu_high = 0
        self._last_ok: float | None = None
        self.snapshot: Sample | None = None

    def healthy(self) -> bool:
        if self._last_ok is None:
            return False
        return time.monotonic() - self._last_ok < 3 * self._settings.poll_interval_s

    async def run(self) -> None:
        while True:
            try:
                await self.check()
            except Exception:
                log.exception("Error leyendo los recursos del host")
            await asyncio.sleep(self._settings.poll_interval_s)

    async def check(self) -> None:
        sample = await asyncio.to_thread(self._sampler.sample)
        self.snapshot = sample
        await self.evaluate(sample)
        self._last_ok = time.monotonic()

    async def evaluate(self, sample: Sample) -> None:
        await self._check_cpu(sample.cpu_pct)
        await self._check_ram(sample.ram_free_gb)
        for disk in sample.disks:
            await self._check_disk(disk)

    async def _check_cpu(self, pct: float) -> None:
        s = self._settings
        key = f"{HEAD}:cpu"
        self._cpu_high = self._cpu_high + 1 if pct > s.cpu_threshold else 0
        if self._cpu_high >= s.cpu_sustained:
            minutes = self._cpu_high * s.poll_interval_s / 60
            await self._alerter.fire(
                key,
                HEAD,
                "warning",
                f"CPU al {pct:.0f} % sostenida {minutes:.1f} min (umbral {s.cpu_threshold:.0f} %)",
            )
        elif pct < s.cpu_threshold - CPU_RESOLVE_MARGIN and self._alerter.is_active(key):
            await self._alerter.resolve(key, f"CPU normalizada ({pct:.0f} %)")

    async def _check_ram(self, free_gb: float) -> None:
        s = self._settings
        key = f"{HEAD}:ram"
        if free_gb < s.ram_free_min_gb:
            await self._alerter.fire(
                key,
                HEAD,
                "critical",
                f"RAM libre {free_gb:.1f} GB (umbral {s.ram_free_min_gb:g} GB)",
            )
        elif free_gb >= s.ram_free_min_gb + RAM_RESOLVE_MARGIN_GB and self._alerter.is_active(key):
            await self._alerter.resolve(key, f"RAM libre recuperada ({free_gb:.1f} GB)")

    async def _check_disk(self, disk: DiskSample) -> None:
        s = self._settings
        missing_key = f"{HEAD}:disco_ausente:{disk.mount}"
        key = f"{HEAD}:disco:{disk.mount}"
        if disk.error:
            await self._alerter.fire(
                missing_key, HEAD, "warning", f"Disco {disk.mount}: {disk.error}"
            )
            return
        if self._alerter.is_active(missing_key):
            await self._alerter.resolve(missing_key, f"Disco {disk.mount} disponible de nuevo")

        if disk.percent > s.disk_threshold:
            level: Level = "critical" if disk.percent >= DISK_CRITICAL else "warning"
            await self._alerter.fire(
                key,
                HEAD,
                level,
                f"Disco {disk.mount} al {disk.percent:.0f} % ({disk.free_gb:g} GB libres, "
                f"umbral {s.disk_threshold:.0f} %)",
            )
        elif disk.percent < s.disk_threshold - DISK_RESOLVE_MARGIN and self._alerter.is_active(key):
            await self._alerter.resolve(key, f"Disco {disk.mount} al {disk.percent:.0f} %")
