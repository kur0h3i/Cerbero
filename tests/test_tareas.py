from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from app.alerter import Alerter
from app.config import Settings
from app.heads.tareas import TareasHead
from app.store import AlertStore
from tests.conftest import FakeClock, FakeNotifier

pytestmark = pytest.mark.anyio

WEEK = 7 * 24 * 3600


class Host:
    """La carpeta /var/lib/cerbero/tareas del host."""

    def __init__(self, path: Path, clock: FakeClock) -> None:
        self.path, self.clock = path, clock
        path.mkdir()

    def report(
        self,
        tarea: str,
        ok: bool = True,
        cada_s: int = 1800,
        resumen: str = "Terminada correctamente",
        avisar: bool = False,
        fin: datetime | None = None,
    ) -> None:
        body = {
            "version": 1,
            "tarea": tarea,
            "resultado": "ok" if ok else "fallo",
            "fin": (fin or self.clock.now).isoformat(),
            "cada_s": cada_s,
            "resumen": resumen,
            "avisar": avisar,
            "detalle": {},
        }
        (self.path / f"{tarea}.json").write_text(json.dumps(body))


@pytest.fixture
def setup(tmp_path: Path, settings: Settings, notifier: FakeNotifier, clock: FakeClock):
    folder = tmp_path / "tareas"
    settings = settings.model_copy(update={"alert_cooldown_min": 0, "tareas_dir": str(folder)})
    alerter = Alerter(settings, AlertStore(), notifier, clock=clock)
    return TareasHead(settings, alerter, clock=clock), Host(folder, clock), alerter, notifier


async def test_healthy_tasks_are_quiet(setup) -> None:
    head, host, alerter, notifier = setup
    host.report("cola-tareas")
    host.report("limpieza-docker", cada_s=WEEK, resumen="🧹 Liberados 1 MB", avisar=True)
    await head.check()
    await head.check()
    assert alerter.store.list() == []
    assert notifier.sent == []  # lo que ya estaba al arrancar no se reenvía
    assert head.healthy()


async def test_summary_is_sent_once_per_run(setup) -> None:
    head, host, alerter, notifier = setup
    host.report("limpieza-docker", cada_s=WEEK, resumen="🧹 Nada que limpiar")
    await head.check()
    host.clock.advance(days=7)
    host.report(
        "limpieza-docker", cada_s=WEEK, resumen="🧹 Liberados 566 MB (12 imágenes)", avisar=True
    )
    await head.check()
    await head.check()
    assert len(notifier.sent) == 1
    assert notifier.sent[0].startswith("<b>🟢 [INFO] Cabeza: Tareas</b>")
    assert "Mensaje: 🧹 Liberados 566 MB (12 imágenes)" in notifier.sent[0]
    # Queda en el historial, no como alerta activa.
    (entry,) = alerter.store.list()
    assert (entry.level, entry.active) == ("info", False)


async def test_failure_is_warning_and_next_success_resolves(setup) -> None:
    head, host, alerter, notifier = setup
    host.report("cola-tareas")
    await head.check()
    host.clock.advance(minutes=30)
    host.report("cola-tareas", ok=False, resumen="Ha fallado (exit-code, exited 1)")
    await head.check()
    alert = alerter.store.get("tareas:fallo:cola-tareas")
    assert alert.level == "warning"
    assert alert.message == "La tarea cola-tareas ha fallado: Ha fallado (exit-code, exited 1)"
    host.clock.advance(minutes=30)
    host.report("cola-tareas")
    await head.check()
    assert not alerter.is_active("tareas:fallo:cola-tareas")
    assert "La tarea cola-tareas vuelve a funcionar" in notifier.sent[-1]


async def test_removed_report_closes_its_alerts(setup) -> None:
    head, host, alerter, notifier = setup
    host.report("cola-tareas", ok=False, resumen="Sin red")
    await head.check()
    assert alerter.is_active("tareas:fallo:cola-tareas")
    (host.path / "cola-tareas.json").unlink()
    await head.check()
    assert not alerter.is_active("tareas:fallo:cola-tareas")
    assert "ya no deja informe" in notifier.sent[-1]


async def test_failed_summary_is_not_sent_as_info(setup) -> None:
    head, host, _, notifier = setup
    await head.check()
    host.report("limpieza-docker", ok=False, cada_s=WEEK, resumen="No se pudo limpiar", avisar=True)
    await head.check()
    assert len(notifier.sent) == 1
    assert "🟡 [AVISO]" in notifier.sent[0]


async def test_stopped_timer(setup) -> None:
    head, host, alerter, notifier = setup
    host.report("cola-tareas")
    host.clock.advance(minutes=59)
    await head.check()
    assert not alerter.is_active("tareas:parada:cola-tareas")
    host.clock.advance(minutes=2)  # 61 min sin informe, cada 30: tolerancia de 2 periodos
    await head.check()
    assert alerter.store.get("tareas:parada:cola-tareas").message == (
        "La tarea cola-tareas no se ejecuta: último informe hace 61 min (debería cada 30 min)"
    )
    host.report("cola-tareas")
    await head.check()
    assert not alerter.is_active("tareas:parada:cola-tareas")
    assert "La tarea cola-tareas vuelve a ejecutarse" in notifier.sent[-1]


async def test_weekly_task_is_stale_after_two_weeks(setup) -> None:
    head, host, alerter, _ = setup
    host.report("limpieza-docker", cada_s=WEEK)
    host.clock.advance(days=13)
    await head.check()
    assert not alerter.is_active("tareas:parada:limpieza-docker")
    host.clock.advance(days=2)
    await head.check()
    assert (
        "hace 15 días (debería cada 7 días)"
        in alerter.store.get("tareas:parada:limpieza-docker").message
    )


async def test_naive_date_uses_cerbero_zone(setup) -> None:
    head, host, alerter, _ = setup
    host.report("cola-tareas")
    path = host.path / "cola-tareas.json"
    body = json.loads(path.read_text())
    body["fin"] = host.clock.now.replace(tzinfo=None).isoformat()
    path.write_text(json.dumps(body))
    await head.check()
    (report,) = head.snapshot
    assert report.fin == host.clock.now
    assert report.fin.tzinfo is head._settings.zone
    assert alerter.store.list() == []


async def test_broken_report(setup) -> None:
    head, host, alerter, _ = setup
    (host.path / "roto.json").write_text("{")
    (host.path / ".roto.json.x.tmp").write_text("{")  # temporal: se ignora
    await head.check()
    assert "informe ilegible" in alerter.store.get("tareas:fallo:roto").message
    assert not alerter.is_active("tareas:parada:roto")


async def test_missing_folder(setup) -> None:
    head, host, alerter, _ = setup
    host.path.rmdir()
    await head.check()
    assert "¿Está instalado host/tareas/?" in alerter.store.get("tareas:sin_datos").message
    assert not head.healthy()


async def test_empty_folder_is_fine(setup) -> None:
    head, _, alerter, _ = setup
    await head.check()
    assert alerter.store.list() == []
    assert head.healthy()
    assert head.snapshot == []
