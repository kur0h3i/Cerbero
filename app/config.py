"""Configuración de Cerbero.

Todo se lee de variables de entorno (en Docker, del fichero ``.env``). Cada campo
de ``Settings`` se sobrescribe con la variable del mismo nombre en mayúsculas:
``cpu_threshold`` ← ``CPU_THRESHOLD``. Las listas van separadas por comas
(``DISKS=/,/srv/archivos``). Una variable vacía equivale a no definirla.

Las rutas del host (discos y ``auth.log``) se escriben tal como son en el host;
``HOST_ROOT`` indica dónde está montada su raíz (``/hostfs`` dentro de Docker).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import datetime
from functools import cached_property
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, IPvAnyNetwork, field_validator

log = logging.getLogger(__name__)

# Rangos de red de confianza. Cualquier acceso SSH desde fuera de ellos es sospechoso.
TRUSTED_NETWORKS: list[str] = [
    "192.168.1.0/24",  # LAN doméstica (server-kuro en 192.168.1.60)
    "10.0.0.0/24",  # WireGuard
    "100.64.0.0/10",  # Tailscale (CGNAT; server-kuro en 100.87.200.60)
]
# El propio servidor siempre es de confianza, aunque se cambie TRUSTED_NETWORKS.
LOOPBACK_NETWORKS: list[str] = ["127.0.0.0/8", "::1/128"]

# Puntos de montaje vigilados por defecto.
DEFAULT_DISKS: list[str] = ["/", "/srv/archivos", "/srv/extra"]

IPAddress = IPv4Address | IPv6Address


class Settings(BaseModel):
    # --- Telegram ---------------------------------------------------------------
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    # Nombre del servidor en los mensajes y zona horaria de las fechas.
    server_name: str = "server-kuro"
    tz: str = "Europe/Madrid"
    # Una alerta con la misma clave no se reenvía hasta pasado este tiempo.
    alert_cooldown_min: float = Field(default=30, ge=0)
    # Mensaje 🟢 al arrancar (sirve para enterarse de un reinicio del servidor).
    notify_startup: bool = True

    # --- Cabeza 1: recursos -----------------------------------------------------
    poll_interval_s: float = Field(default=30, gt=0)
    cpu_threshold: float = Field(default=85, gt=0, le=100)
    cpu_sustained: int = Field(default=3, ge=1)
    ram_free_min_gb: float = Field(default=1.5, gt=0)
    disk_threshold: float = Field(default=85, gt=0, le=100)
    disks: list[str] = Field(default_factory=lambda: list(DEFAULT_DISKS))
    host_root: str = "/"

    # --- Cabeza 2: contenedores -------------------------------------------------
    restart_loop_threshold: int = Field(default=5, ge=1)
    restart_loop_window_min: float = Field(default=10, gt=0)
    containers_ignore: list[str] = Field(default_factory=list)

    # --- Cabeza 3: accesos ------------------------------------------------------
    auth_log: str = "/var/log/auth.log"
    brute_force_threshold: int = Field(default=10, ge=1)
    brute_force_window_min: float = Field(default=5, gt=0)
    trusted_networks: list[IPvAnyNetwork] = Field(
        default_factory=lambda: [ip_network(n) for n in TRUSTED_NETWORKS]
    )

    log_level: str = "INFO"

    @field_validator("disks", "containers_ignore", "trusted_networks", mode="before")
    @classmethod
    def _split_commas(cls, v: object) -> object:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("tz")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        v = v.strip().lstrip(":")
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"zona horaria desconocida: {v!r}") from exc
        return v

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()

    @cached_property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @cached_property
    def trusted_all(self) -> list[IPvAnyNetwork]:
        """Rangos de confianza más loopback."""
        return [*self.trusted_networks, *(ip_network(n) for n in LOOPBACK_NETWORKS)]

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    def now(self) -> datetime:
        """Hora actual en la zona configurada, sin microsegundos."""
        return datetime.now(self.zone).replace(microsecond=0)

    def host_path(self, path: str) -> str:
        """Ruta del host vista desde Cerbero (``/srv/extra`` → ``/hostfs/srv/extra``)."""
        root = self.host_root.rstrip("/")
        return f"{root}/{path.lstrip('/')}" if root else path

    def is_trusted(self, ip: IPAddress | str) -> bool:
        if isinstance(ip, str):
            ip = ip_address(ip)
        if isinstance(ip, IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in net for net in self.trusted_all)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Construye ``Settings`` a partir del entorno (``os.environ`` por defecto)."""
    env = os.environ if env is None else env
    data: dict[str, str] = {}
    for name in Settings.model_fields:
        value = env.get(name.upper())
        if value is not None and value.strip():
            data[name] = value.strip()
    return Settings.model_validate(data)
