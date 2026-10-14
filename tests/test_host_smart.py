"""Tests del script del host ``host/smart/cerbero_smart.py`` (corre como root en el host)."""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"

_spec = importlib.util.spec_from_file_location(
    "cerbero_smart", ROOT / "host/smart/cerbero_smart.py"
)
assert _spec and _spec.loader
cs = importlib.util.module_from_spec(_spec)
sys.modules["cerbero_smart"] = cs  # dataclasses lo necesita al ejecutar el módulo
_spec.loader.exec_module(cs)

NOW = datetime(2026, 10, 14, 10, 0, tzinfo=ZoneInfo("Europe/Madrid"))


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeSmartctl:
    """Responde según el tipo pedido y apunta las llamadas."""

    def __init__(self, by_type: dict[str | None, tuple[int, dict]]) -> None:
        self.by_type = by_type
        self.calls: list[tuple[str, str | None, bool]] = []

    def __call__(self, device: str, dtype: str | None, no_wake: bool) -> tuple[int, dict]:
        self.calls.append((device, dtype, no_wake))
        return self.by_type.get(dtype, (2, {}))


@pytest.fixture
def device(tmp_path: Path) -> str:
    dev = tmp_path / "disco"
    dev.touch()  # basta con que exista
    return str(dev)


def test_parse_conf() -> None:
    disks = cs.parse_conf(
        """
        # comentario
        sistema   /dev/disk/by-id/nvme-Micron_X   /
        extra     /dev/disk/by-id/usb-Seagate-0:0  /srv/extra  tipo=sat
        secure    /dev/disk/by-id/ata-WDC_Y   /srv/secure  opcional   # se monta a mano
        suelto    /dev/sdz  -
        """
    )
    assert disks == [
        cs.Disk("sistema", "/dev/disk/by-id/nvme-Micron_X", "/"),
        cs.Disk("extra", "/dev/disk/by-id/usb-Seagate-0:0", "/srv/extra", dtype="sat"),
        cs.Disk("secure", "/dev/disk/by-id/ata-WDC_Y", "/srv/secure", optional=True),
        cs.Disk("suelto", "/dev/sdz", None),
    ]


@pytest.mark.parametrize(
    "text",
    ["solo-nombre", "a /dev/x\na /dev/y", "../malo /dev/x", "a /dev/x /mnt raro"],
)
def test_parse_conf_errors(text: str) -> None:
    with pytest.raises(ValueError):
        cs.parse_conf(text)


def test_full_read(device: str) -> None:
    run = FakeSmartctl({None: (0, fixture("smartctl_ata.json"))})
    out = cs.check_disk(cs.Disk("archivos", device, "/srv/archivos"), None, NOW, run)
    head = out["cerbero"]
    assert head["estado"] == "ok"
    assert head["comprobado"] == head["leido"] == "2026-10-14T10:00:00+02:00"
    assert head["tipo"] == "auto"
    assert out["smartctl"]["model_name"] == "WDC WD50NDZW-11C8XS1"
    # Sin lectura previa no hay nada que conservar: se lee aunque despierte al disco.
    assert run.calls == [(device, None, False)]


def test_unknown_usb_bridge_falls_back_to_sat(device: str) -> None:
    run = FakeSmartctl(
        {
            None: (1, fixture("smartctl_unknown_bridge.json")),
            "sat": (0, fixture("smartctl_ata.json")),
        }
    )
    out = cs.check_disk(cs.Disk("extra", device), None, NOW, run)
    assert out["cerbero"]["estado"] == "ok"
    assert out["cerbero"]["tipo"] == "sat"
    # La siguiente vez empieza directamente por sat.
    run.calls.clear()
    cs.check_disk(cs.Disk("extra", device), out, NOW + timedelta(hours=1), run)
    assert [c[1] for c in run.calls] == ["sat"]


def test_standby_keeps_last_data_without_waking(device: str) -> None:
    disk = cs.Disk("extra", device)
    first = cs.check_disk(disk, None, NOW, FakeSmartctl({None: (0, fixture("smartctl_ata.json"))}))
    run = FakeSmartctl({None: (2, fixture("smartctl_standby.json"))})
    later = NOW + timedelta(hours=3)
    out = cs.check_disk(disk, first, later, run)
    assert run.calls == [(device, None, True)]  # -n standby
    head = out["cerbero"]
    assert head["estado"] == "standby"
    assert head["comprobado"] == later.isoformat()
    assert head["leido"] == NOW.isoformat()  # la lectura buena sigue siendo la de antes
    assert out["smartctl"] == first["smartctl"]


def test_forces_a_read_after_24h(device: str) -> None:
    disk = cs.Disk("extra", device)
    first = cs.check_disk(disk, None, NOW, FakeSmartctl({None: (0, fixture("smartctl_ata.json"))}))
    run = FakeSmartctl({None: (0, fixture("smartctl_ata.json"))})
    cs.check_disk(disk, first, NOW + timedelta(hours=25), run)
    assert run.calls[0][2] is False  # sin -n standby: se lee aunque esté dormido


def test_absent_disk_keeps_last_data(tmp_path: Path) -> None:
    previous = {"cerbero": {"leido": NOW.isoformat()}, "smartctl": {"model_name": "X"}}
    run = FakeSmartctl({})
    disk = cs.Disk("secure", str(tmp_path / "no-esta"), "/srv/secure", optional=True)
    out = cs.check_disk(disk, previous, NOW, run)
    assert out["cerbero"]["estado"] == "ausente"
    assert out["cerbero"]["opcional"] is True
    assert out["smartctl"] == {"model_name": "X"}
    assert run.calls == []


def test_unreadable_smart_is_an_error(device: str) -> None:
    run = FakeSmartctl({None: (1, fixture("smartctl_unknown_bridge.json"))})
    out = cs.check_disk(cs.Disk("extra", device), None, NOW, run)
    assert out["cerbero"]["estado"] == "error"
    assert "Unknown USB bridge" in out["cerbero"]["error"]


def test_error_without_messages_explains_the_exit_code(device: str) -> None:
    # Así responde un puente USB que rechaza el passthrough ATA (Seagate 0bc2:2036):
    # «Read Device Identity failed» va fuera del JSON y solo queda el código 2.
    bare = {"smartctl": {"exit_status": 2}, "device": {"name": device}}
    run = FakeSmartctl({None: (2, bare), "sat": (2, bare)})
    out = cs.check_disk(cs.Disk("extra", device), None, NOW, run)
    assert out["cerbero"]["error"] == (
        "código 2: no se pudo abrir el disco o no respondió a IDENTIFY (¿puente USB sin SMART?)"
    )


def test_error_shows_both_attempts_when_they_differ(device: str) -> None:
    run = FakeSmartctl(
        {None: (1, fixture("smartctl_unknown_bridge.json")), "sat": (2, {"smartctl": {}})}
    )
    out = cs.check_disk(cs.Disk("extra", device), None, NOW, run)
    assert out["cerbero"]["error"].startswith("auto: /dev/sda: Unknown USB bridge")
    assert "; sat: código 2: no se pudo abrir" in out["cerbero"]["error"]


def test_detected_disk_without_smart_is_commented_out() -> None:
    ok = cs.conf_line(
        "archivos", "/dev/disk/by-id/ata-X", "/srv/archivos", False, None, "WDC", None
    )
    assert ok == "archivos  /dev/disk/by-id/ata-X  /srv/archivos  # WDC"
    bad = cs.conf_line(
        "extra", "/dev/disk/by-id/usb-Y", "/srv/extra", False, None, "Exp", "código 2"
    )
    assert bad.startswith("# extra")
    assert cs.parse_conf(bad) == []  # el timer no lo toca
    secure = cs.conf_line("secure", "/dev/x", "/srv/secure", True, "sat", "WDC", None)
    assert cs.parse_conf(secure) == [cs.Disk("secure", "/dev/x", "/srv/secure", True, "sat")]


def test_dump_all_writes_atomically_and_world_readable(tmp_path: Path, device: str) -> None:
    out_dir = tmp_path / "smart"
    disks = [cs.Disk("archivos", device), cs.Disk("secure", str(tmp_path / "x"), optional=True)]
    run = FakeSmartctl({None: (0, fixture("smartctl_ata.json"))})
    errors = cs.dump_all(disks, out_dir, NOW, run)
    assert errors == 0
    assert sorted(p.name for p in out_dir.iterdir()) == ["archivos.json", "secure.json"]
    path = out_dir / "archivos.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert json.loads(path.read_text())["cerbero"]["estado"] == "ok"


def test_preferred_ids_picks_readable_names() -> None:
    ids = cs.preferred_ids(
        {
            "nvme-eui.00000000000000010000000000000001": "/dev/nvme0n1",
            "nvme-Micron_2400_MTFDKBA512QFM_0000AAAA1111": "/dev/nvme0n1",
            "nvme-Micron_2400_MTFDKBA512QFM_0000AAAA1111_1": "/dev/nvme0n1",
            "ata-WDC_WD50NDZW-11C8XS1_WD-WX00AAAA1111": "/dev/sdb",
            "usb-WD_easystore_2647_000000000000000000000001-0:0": "/dev/sdb",
            "wwn-0x5000000000000001": "/dev/sdb",
            "ata-WDC_WD50NDZW-11C8XS1_WD-WX00AAAA1111-part1": "/dev/sdb1",
            "usb-Seagate_Expansion_HDD_00000000AAAA1111-0:0": "/dev/sda",
            "dm-name-sdc_secure": "/dev/dm-0",
        }
    )
    assert ids == {
        "/dev/nvme0n1": "nvme-Micron_2400_MTFDKBA512QFM_0000AAAA1111",
        "/dev/sdb": "ata-WDC_WD50NDZW-11C8XS1_WD-WX00AAAA1111",
        "/dev/sda": "usb-Seagate_Expansion_HDD_00000000AAAA1111-0:0",
    }


def test_mounts_follow_luks_children() -> None:
    node = {
        "name": "sdc",
        "type": "disk",
        "mountpoints": [None],
        "children": [
            {
                "name": "sdc1",
                "type": "part",
                "mountpoints": [None],
                "children": [
                    {"name": "sdc_secure", "type": "crypt", "mountpoints": ["/srv/secure"]}
                ],
            }
        ],
    }
    assert cs._mounts(node) == [("/srv/secure", True)]


def test_disks_removed_from_conf_are_forgotten(tmp_path: Path, device: str) -> None:
    out_dir = tmp_path / "smart"
    out_dir.mkdir()
    (out_dir / "extra.json").write_text("{}")  # de cuando estaba en discos.conf
    run = FakeSmartctl({None: (0, fixture("smartctl_ata.json"))})
    cs.dump_all([cs.Disk("archivos", device)], out_dir, NOW, run)
    assert sorted(p.name for p in out_dir.iterdir()) == ["archivos.json"]
