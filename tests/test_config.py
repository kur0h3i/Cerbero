from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import load_settings


def test_defaults() -> None:
    s = load_settings({})
    assert s.cpu_threshold == 85
    assert s.cpu_sustained == 3
    assert s.ram_free_min_gb == 1.5
    assert s.disk_threshold == 85
    assert s.disks == ["/", "/srv/archivos", "/srv/extra"]
    assert s.alert_cooldown_min == 30
    assert s.restart_loop_threshold == 5
    assert s.restart_loop_window_min == 10
    assert s.brute_force_threshold == 10
    assert s.brute_force_window_min == 5
    assert [str(n) for n in s.trusted_networks] == [
        "192.168.1.0/24",
        "10.0.0.0/24",
        "100.64.0.0/10",
    ]
    assert not s.telegram_enabled


def test_env_overrides_and_lists() -> None:
    s = load_settings(
        {
            "CPU_THRESHOLD": "90",
            "DISKS": " /, /data ,",
            "CONTAINERS_IGNORE": "watchtower,backup",
            "TRUSTED_NETWORKS": "10.1.0.0/16",
            "TELEGRAM_TOKEN": "t",
            "TELEGRAM_CHAT_ID": "1",
            "BRUTE_FORCE_THRESHOLD": "",  # vacía = valor por defecto
        }
    )
    assert s.cpu_threshold == 90
    assert s.disks == ["/", "/data"]
    assert s.containers_ignore == ["watchtower", "backup"]
    assert [str(n) for n in s.trusted_networks] == ["10.1.0.0/16"]
    assert s.brute_force_threshold == 10
    assert s.telegram_enabled


@pytest.mark.parametrize(
    ("ip", "trusted"),
    [
        ("192.168.1.60", True),
        ("10.0.0.7", True),
        ("100.87.200.60", True),
        ("100.128.0.1", False),  # justo fuera de 100.64.0.0/10
        ("192.168.2.1", False),
        ("8.8.8.8", False),
        ("127.0.0.1", True),
        ("::1", True),
        ("::ffff:192.168.1.10", True),
        ("2001:db8::1", False),
    ],
)
def test_is_trusted(ip: str, trusted: bool) -> None:
    assert load_settings({}).is_trusted(ip) is trusted


def test_loopback_trusted_even_with_custom_networks() -> None:
    s = load_settings({"TRUSTED_NETWORKS": "10.1.0.0/16"})
    assert s.is_trusted("127.0.0.1")
    assert not s.is_trusted("192.168.1.60")


def test_host_path() -> None:
    assert load_settings({}).host_path("/srv/extra") == "/srv/extra"
    s = load_settings({"HOST_ROOT": "/hostfs/"})
    assert s.host_path("/srv/extra") == "/hostfs/srv/extra"
    assert s.host_path("/var/log/auth.log") == "/hostfs/var/log/auth.log"


@pytest.mark.parametrize(
    "env",
    [
        {"TZ": "Marte/Base"},
        {"CPU_THRESHOLD": "150"},
        {"TRUSTED_NETWORKS": "no-es-una-red"},
        {"CPU_SUSTAINED": "0"},
    ],
)
def test_invalid_values(env: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        load_settings(env)
