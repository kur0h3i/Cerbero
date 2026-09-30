"""API REST de Cerbero (la consume Dis).

- ``GET /api/health``  estado de cada cabeza
- ``GET /api/alerts``  alertas activas y últimas resueltas: ``{connected, alerts}``
- ``GET /api/status``  última lectura de recursos y contenedores

Sin autenticación: Cerbero solo es accesible dentro de la LAN y la tailnet.
Solo lectura: no hay ninguna ruta que modifique nada.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .store import Alert

if TYPE_CHECKING:
    from .main import Cerbero

router = APIRouter(prefix="/api")


class HealthOut(BaseModel):
    # "degraded" si alguna cabeza no está funcionando.
    status: Literal["ok", "degraded"]
    heads: dict[str, bool]


class AlertsOut(BaseModel):
    connected: bool = True
    alerts: list[Alert]


class DiskOut(BaseModel):
    mount: str
    total_gb: float
    used_gb: float
    free_gb: float
    percent: float
    error: str | None = None


class ContainerOut(BaseModel):
    name: str
    status: str
    health: str | None
    restarts: int


class StatusOut(BaseModel):
    # ``None`` hasta la primera lectura (unos segundos tras arrancar).
    cpu_pct: float | None = None
    ram_free_gb: float | None = None
    ram_total_gb: float | None = None
    disks: list[DiskOut] = []
    containers: list[ContainerOut] = []


def _cerbero(request: Request) -> Cerbero:
    return request.app.state.cerbero


@router.get("/health", response_model=HealthOut)
def health(request: Request) -> HealthOut:
    heads = {head.name: head.healthy() for head in _cerbero(request).heads}
    return HealthOut(status="ok" if all(heads.values()) else "degraded", heads=heads)


@router.get("/alerts", response_model=AlertsOut)
def alerts(request: Request, active: bool | None = None) -> AlertsOut:
    """Activas primero y luego las resueltas; ``?active=true`` deja solo las activas."""
    items = _cerbero(request).alerter.store.list()
    if active is not None:
        items = [a for a in items if a.active is active]
    return AlertsOut(alerts=items)


@router.get("/status", response_model=StatusOut)
def status(request: Request) -> StatusOut:
    cerbero = _cerbero(request)
    out = StatusOut()
    if (sample := cerbero.recursos.snapshot) is not None:
        out.cpu_pct = sample.cpu_pct
        out.ram_free_gb = sample.ram_free_gb
        out.ram_total_gb = sample.ram_total_gb
        out.disks = [DiskOut(**asdict(d)) for d in sample.disks]
    if (containers := cerbero.contenedores.snapshot) is not None:
        out.containers = [
            ContainerOut(name=c.name, status=c.state, health=c.health, restarts=c.restart_count)
            for c in containers
        ]
    return out
