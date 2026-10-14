from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.alerter import Alerter
from app.config import Settings
from app.heads.smart import SmartHead, parse_disk, read_dir
from app.store import AlertStore
from tests.conftest import FakeClock, FakeNotifier

pytestmark = pytest.mark.anyio
FIXTURES = Path(__file__).parent / "fixtures"


def smartctl(name: str) -> dict:
    return json.loads((FIXTURES / f"smartctl_{name}.json").read_text())


def payload(
    nombre: str,
    when: datetime,
    data: dict | None,
    estado: str = "ok",
    montaje: str | None = None,
    opcional: bool = False,
    error: str | None = None,
) -> dict:
    return {
        "cerbero": {
            "version": 1,
            "nombre": nombre,
            "dispositivo": f"/dev/disk/by-id/{nombre}",
            "montaje": montaje,
            "opcional": opcional,
            "comprobado": when.isoformat(),
            "leido": when.isoformat() if data else None,
            "estado": estado,
            "error": error,
        },
        "smartctl": data,
    }


def ata(**attrs: int) -> dict:
    """SMART de un disco ATA cambiando el valor bruto de algunos atributos (id → valor)."""
    data = copy.deepcopy(smartctl("ata"))
    for attr in data["ata_smart_attributes"]["table"]:
        if (value := attrs.get(f"a{attr['id']}")) is not None:
            attr["raw"]["value"] = value
    return data


class Host:
    """La carpeta /var/lib/cerbero/smart del host."""

    def __init__(self, path: Path, clock: FakeClock) -> None:
        self.path, self.clock = path, clock
        path.mkdir()

    def write(self, nombre: str, data: dict | None, **kw) -> None:
        body = payload(nombre, self.clock.now, data, **kw)
        (self.path / f"{nombre}.json").write_text(json.dumps(body))


@pytest.fixture
def setup(tmp_path: Path, settings: Settings, notifier: FakeNotifier, clock: FakeClock):
    smart_dir = tmp_path / "smart"
    settings = settings.model_copy(update={"alert_cooldown_min": 0, "smart_dir": str(smart_dir)})
    alerter = Alerter(settings, AlertStore(), notifier, clock=clock)
    head = SmartHead(settings, alerter, clock=clock)
    return head, Host(smart_dir, clock), alerter, notifier


def test_parse_ata_and_nvme(clock: FakeClock) -> None:
    disk = parse_disk(
        "archivos", payload("archivos", clock.now, smartctl("ata"), montaje="/srv/archivos")
    )
    assert (disk.modelo, disk.protocolo, disk.salud) == ("WDC WD50NDZW-11C8XS1", "ATA", True)
    assert (disk.temp_c, disk.horas) == (38, 9012)
    assert (disk.reasignados, disk.pendientes, disk.incorregibles) == (0, 0, 0)
    assert disk.etiqueta == "archivos (/srv/archivos)"
    nvme = parse_disk("sistema", payload("sistema", clock.now, smartctl("nvme")))
    assert nvme.nvme and nvme.salud
    assert (nvme.temp_c, nvme.reserva_pct, nvme.desgaste_pct, nvme.errores_medio) == (41, 100, 3, 0)
    assert nvme.reasignados is None


def test_read_dir_skips_temp_files_and_reports_broken_json(tmp_path: Path) -> None:
    (tmp_path / "bueno.json").write_text(json.dumps({"cerbero": {"estado": "ok"}}))
    (tmp_path / ".bueno.json.abc.tmp").write_text("{")
    (tmp_path / "roto.json").write_text("{")
    disks = read_dir(str(tmp_path))
    assert [(d.nombre, d.estado) for d in disks] == [("bueno", "ok"), ("roto", "error")]
    assert "JSON ilegible" in disks[1].error


async def test_healthy_disks_are_quiet(setup) -> None:
    head, host, alerter, _ = setup
    host.write("sistema", smartctl("nvme"), montaje="/")
    host.write("archivos", smartctl("ata"), montaje="/srv/archivos")
    await head.check()
    assert alerter.store.list() == []
    assert head.healthy()
    assert [d.nombre for d in head.snapshot] == ["archivos", "sistema"]


async def test_no_data_yet(setup) -> None:
    head, host, alerter, notifier = setup
    host.path.rmdir()
    await head.check()
    assert alerter.is_active("smart:sin_datos")
    assert "host/smart/instalar.sh" in notifier.sent[0]
    assert not head.healthy()
    host.path.mkdir()
    host.write("archivos", smartctl("ata"))
    await head.check()
    assert not alerter.is_active("smart:sin_datos")


async def test_failed_is_critical(setup) -> None:
    head, host, alerter, _ = setup
    data = smartctl("ata")
    data["smart_status"]["passed"] = False
    host.write("extra", data, montaje="/srv/extra")
    await head.check()
    alert = alerter.store.get("smart:fallo:extra")
    assert alert.level == "critical"
    assert alert.message == "Disco extra (/srv/extra) — WDC WD50NDZW-11C8XS1: SMART FAILED"


async def test_nvme_critical_warning(setup) -> None:
    head, host, alerter, _ = setup
    data = smartctl("nvme")
    data["nvme_smart_health_information_log"].update(critical_warning=1, available_spare=4)
    host.write("sistema", data)
    await head.check()
    assert (
        "aviso crítico NVMe 0x01, reserva 4 %" in alerter.store.get("smart:fallo:sistema").message
    )


async def test_pending_sectors_are_critical(setup) -> None:
    head, host, alerter, _ = setup
    host.write("archivos", ata(a197=8, a198=2))
    await head.check()
    alert = alerter.store.get("smart:sectores:archivos")
    assert alert.level == "critical"
    assert alert.message == "Disco archivos: 8 pendientes, 2 incorregibles"


async def test_stable_reallocated_is_warning_and_rising_is_critical(setup) -> None:
    head, host, alerter, _ = setup
    host.write("extra", ata(a5=8))
    await head.check()
    alert = alerter.store.get("smart:sectores:extra")
    assert alert.level == "warning"
    assert alert.message == "Disco extra: 8 reasignados"
    host.clock.advance(hours=1)
    host.write("extra", ata(a5=12))
    await head.check()
    assert alert.level == "critical"
    assert alert.message == "Disco extra: 12 reasignados (subiendo)"
    # Se mantiene en 🔴 un día después de la última subida; luego vuelve a 🟡.
    host.clock.advance(hours=23)
    host.write("extra", ata(a5=12))
    await head.check()
    assert alert.level == "critical"
    host.clock.advance(hours=2)
    host.write("extra", ata(a5=12))
    await head.check()
    assert alert.level == "warning"


async def test_temperature(setup) -> None:
    head, host, alerter, _ = setup
    hot = smartctl("ata")
    hot["temperature"]["current"] = 58
    host.write("archivos", hot)
    await head.check()
    assert alerter.store.get("smart:temperatura:archivos").message == (
        "Disco archivos a 58 °C (umbral 55 °C)"
    )
    # 65 °C es normal para un NVMe (umbral 70) pero no para un disco duro.
    nvme = smartctl("nvme")
    nvme["temperature"]["current"] = 65
    host.write("sistema", nvme)
    await head.check()
    assert not alerter.is_active("smart:temperatura:sistema")
    # Con margen para resolver: a 54 °C sigue, a 51 °C se resuelve.
    hot["temperature"]["current"] = 54
    host.write("archivos", hot)
    await head.check()
    assert alerter.is_active("smart:temperatura:archivos")
    hot["temperature"]["current"] = 51
    host.write("archivos", hot)
    await head.check()
    assert not alerter.is_active("smart:temperatura:archivos")


async def test_standby_keeps_last_data_and_cools_down(setup) -> None:
    head, host, alerter, _ = setup
    hot = smartctl("ata")
    hot["temperature"]["current"] = 58
    host.write("extra", hot)
    await head.check()
    assert alerter.is_active("smart:temperatura:extra")
    host.clock.advance(hours=1)
    host.write("extra", hot, estado="standby")
    await head.check()
    assert not alerter.is_active("smart:temperatura:extra")
    assert head.snapshot[0].estado == "standby"
    assert head.snapshot[0].salud is True  # datos de la última lectura completa


async def test_optional_disk_unplugged_is_not_an_alert(setup) -> None:
    head, host, alerter, _ = setup
    host.write("secure", None, estado="ausente", montaje="/srv/secure", opcional=True)
    host.write("extra", None, estado="ausente", montaje="/srv/extra")
    await head.check()
    assert not alerter.is_active("smart:ausente:secure")
    assert alerter.store.get("smart:ausente:extra").message == (
        "Disco extra (/srv/extra) no está conectado"
    )


async def test_unreadable_smart(setup) -> None:
    head, host, alerter, _ = setup
    host.write("extra", None, estado="error", error="Unknown USB bridge")
    await head.check()
    assert alerter.store.get("smart:error:extra").message == (
        "No se puede leer SMART de extra: Unknown USB bridge"
    )


async def test_removed_disk_closes_its_alerts(setup) -> None:
    head, host, alerter, notifier = setup
    host.write("extra", None, estado="error", error="Unknown USB bridge")
    host.write("archivos", smartctl("ata"))
    await head.check()
    assert alerter.is_active("smart:error:extra")
    (host.path / "extra.json").unlink()
    await head.check()
    assert not alerter.is_active("smart:error:extra")
    assert "ya no se vigila" in notifier.sent[-1]


async def test_unreadable_dir_does_not_close_alerts(setup) -> None:
    head, host, alerter, _ = setup
    host.write("extra", None, estado="error", error="Unknown USB bridge")
    await head.check()
    for f in host.path.iterdir():
        f.unlink()
    host.path.rmdir()
    await head.check()
    assert alerter.is_active("smart:error:extra")
    assert alerter.is_active("smart:sin_datos")


async def test_stale_data_means_the_timer_stopped(setup) -> None:
    head, host, alerter, _ = setup
    host.write("archivos", smartctl("ata"))
    host.write("sistema", smartctl("nvme"))
    host.clock.advance(hours=4)
    host.write("sistema", smartctl("nvme"))  # solo uno se ha actualizado
    await head.check()
    alert = alerter.store.get("smart:antiguo")
    assert alert.level == "warning"
    assert alert.message == (
        "Datos SMART sin actualizar (hace 4 h) en: archivos. ¿Se ha parado cerbero-smart.timer?"
    )
    assert not head.healthy()
    host.write("archivos", smartctl("ata"))
    await head.check()
    assert not alerter.is_active("smart:antiguo")
    assert head.healthy()


async def test_naive_date_uses_cerbero_zone(setup) -> None:
    head, host, alerter, _ = setup
    host.write("archivos", smartctl("ata"))
    path = host.path / "archivos.json"
    body = json.loads(path.read_text())
    body["cerbero"]["comprobado"] = host.clock.now.replace(tzinfo=None).isoformat()
    path.write_text(json.dumps(body))
    await head.check()
    (disk,) = head.snapshot
    assert disk.comprobado == host.clock.now
    assert disk.comprobado.tzinfo is head._settings.zone
    assert not alerter.is_active("smart:antiguo")


async def test_nvme_wear(setup) -> None:
    head, host, alerter, _ = setup
    data = smartctl("nvme")
    data["nvme_smart_health_information_log"]["percentage_used"] = 92
    host.write("sistema", data)
    await head.check()
    assert alerter.store.get("smart:desgaste:sistema").message == (
        "Disco sistema: desgaste del 92 % de su vida útil"
    )


def test_clock_is_aware(clock: FakeClock) -> None:
    # Las fechas del host llevan zona; las del reloj también (si no, no se pueden restar).
    assert clock.now.tzinfo is not None
    assert (clock.now + timedelta(hours=1)).tzinfo is not None
