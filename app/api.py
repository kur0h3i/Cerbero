"""API REST de Cerbero (la consume Dis).

- ``GET /api/health``  estado de cada cabeza
- ``GET /api/alerts``  alertas activas y últimas resueltas: ``{connected, alerts}``
- ``GET /api/status``  última lectura de recursos, contenedores, SMART y tareas

Sin autenticación: Cerbero solo es accesible dentro de la LAN y la tailnet.
Solo lectura: no hay ninguna ruta que modifique nada.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
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


class SmartDiskOut(BaseModel):
    nombre: str
    estado: str  # ok | standby | ausente | error
    dispositivo: str
    montaje: str | None
    opcional: bool
    comprobado: datetime | None
    leido: datetime | None  # última lectura completa (en reposo se conserva)
    error: str | None
    modelo: str | None
    protocolo: str | None
    salud: bool | None
    temp_c: float | None
    horas: int | None
    reasignados: int | None
    pendientes: int | None
    incorregibles: int | None
    aviso_critico: int | None
    errores_medio: int | None
    reserva_pct: int | None
    desgaste_pct: int | None


class TaskOut(BaseModel):
    tarea: str
    resultado: str  # ok | fallo
    fin: datetime | None
    cada_s: int
    resumen: str
    parada: bool  # el último informe es demasiado viejo


class LoginOut(BaseModel):
    fecha: datetime
    usuario: str
    ip: str
    metodo: str
    confianza: bool


class AccessOut(BaseModel):
    log: str
    legible: bool
    lineas: int  # líneas leídas desde que arrancó Cerbero
    ips_seguidas: int  # IPs con fallos recientes
    fallos_ventana: int  # fallos de login en BRUTE_FORCE_WINDOW_MIN
    ultimos_accesos: list[LoginOut]  # últimos logins aceptados, el más reciente primero


class ThresholdsOut(BaseModel):
    """Los umbrales en uso, para que quien pinte el estado sepa dónde está el límite."""

    cpu_pct: float
    cpu_lecturas: int
    ram_libre_min_gb: float
    disco_pct: float
    reinicios: int
    reinicios_ventana_min: float
    fuerza_bruta: int
    fuerza_bruta_ventana_min: float
    smart_temp_hdd_c: float
    smart_temp_nvme_c: float
    smart_max_horas: float
    cooldown_min: float
    redes_confianza: list[str]


class StatusOut(BaseModel):
    servidor: str
    umbrales: ThresholdsOut
    # ``None`` hasta la primera lectura (unos segundos tras arrancar).
    cpu_pct: float | None = None
    ram_free_gb: float | None = None
    ram_total_gb: float | None = None
    disks: list[DiskOut] = []
    containers: list[ContainerOut] = []
    smart: list[SmartDiskOut] = []
    tareas: list[TaskOut] = []
    accesos: AccessOut | None = None


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
    s = cerbero.settings
    out = StatusOut(
        servidor=s.server_name,
        umbrales=ThresholdsOut(
            cpu_pct=s.cpu_threshold,
            cpu_lecturas=s.cpu_sustained,
            ram_libre_min_gb=s.ram_free_min_gb,
            disco_pct=s.disk_threshold,
            reinicios=s.restart_loop_threshold,
            reinicios_ventana_min=s.restart_loop_window_min,
            fuerza_bruta=s.brute_force_threshold,
            fuerza_bruta_ventana_min=s.brute_force_window_min,
            smart_temp_hdd_c=s.smart_temp_hdd_max,
            smart_temp_nvme_c=s.smart_temp_nvme_max,
            smart_max_horas=s.smart_max_age_h,
            cooldown_min=s.alert_cooldown_min,
            redes_confianza=[str(n) for n in s.trusted_networks],
        ),
    )
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
    if (disks := cerbero.smart.snapshot) is not None:
        out.smart = [SmartDiskOut(**asdict(d)) for d in disks]
    if (reports := cerbero.tareas.snapshot) is not None:
        now = cerbero.settings.now()
        out.tareas = [
            TaskOut(
                tarea=r.tarea,
                resultado=r.resultado,
                fin=r.fin,
                cada_s=r.cada_s,
                resumen=r.error or r.resumen,
                parada=cerbero.tareas.is_stale(r, now),
            )
            for r in reports
        ]
    if (access := cerbero.accesos.snapshot) is not None:
        out.accesos = AccessOut(**asdict(access))
    return out
