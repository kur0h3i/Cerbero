from __future__ import annotations

from dataclasses import replace

import pytest

from app.alerter import Alerter
from app.config import Settings
from app.heads.contenedores import ContainerInfo, ContenedoresHead, parse_inspect
from app.store import AlertStore
from tests.conftest import FakeClock, FakeNotifier

pytestmark = pytest.mark.anyio


class FakeSource:
    def __init__(self, *infos: ContainerInfo) -> None:
        self.infos = list(infos)
        self.error: Exception | None = None
        self.stops: set[str] = set()

    def list(self) -> list[ContainerInfo]:
        if self.error:
            raise self.error
        return [replace(i) for i in self.infos]

    def manual_stops(self, since: float, until: float) -> set[str]:
        stops, self.stops = self.stops, set()
        return stops

    def set(self, name: str, **changes) -> None:
        self.infos = [replace(i, **changes) if i.name == name else i for i in self.infos]


class Monotonic:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def running(name: str, **kw) -> ContainerInfo:
    return ContainerInfo(name=name, id=f"id-{name}", state="running", **kw)


@pytest.fixture
def setup(settings: Settings, notifier: FakeNotifier, clock: FakeClock):
    settings = settings.model_copy(update={"alert_cooldown_min": 0, "containers_ignore": ["tarea"]})
    alerter = Alerter(settings, AlertStore(), notifier, clock=clock)
    source = FakeSource(
        running("nginx"),
        running("jellyfin"),
        ContainerInfo(name="viejo", id="id-viejo", state="exited", exit_code=1),
    )
    mono = Monotonic()
    head = ContenedoresHead(settings, alerter, source=source, clock=mono)
    return head, source, alerter, notifier, mono


async def test_stopped_before_start_do_not_alert(setup) -> None:
    head, _, alerter, notifier, _ = setup
    await head.check()
    await head.check()
    assert alerter.store.list() == []
    assert notifier.sent == []


async def test_crash_is_critical_and_recovery_resolves(setup) -> None:
    head, source, alerter, notifier, _ = setup
    await head.check()
    source.set("nginx", state="exited", exit_code=1)
    await head.check()
    alert = alerter.store.get("contenedores:caido:nginx")
    assert alert.level == "critical"
    assert "Contenedor nginx caído (código 1)" in notifier.sent[0]
    source.set("nginx", state="running", exit_code=0)
    await head.check()
    assert not alerter.is_active("contenedores:caido:nginx")
    assert "Contenedor nginx en marcha de nuevo" in notifier.sent[-1]


async def test_clean_stop_is_warning_and_not_repeated(setup) -> None:
    head, source, alerter, notifier, _ = setup
    await head.check()
    source.set("jellyfin", state="exited", exit_code=143)
    await head.check()
    await head.check()
    await head.check()
    assert alerter.store.get("contenedores:caido:jellyfin").level == "warning"
    assert len(notifier.sent) == 1
    assert "parado (salida limpia, código 143)" in notifier.sent[0]


async def test_crash_keeps_reminding(setup) -> None:
    head, source, _, notifier, _ = setup
    await head.check()
    source.set("nginx", state="dead", exit_code=137)
    await head.check()
    await head.check()
    # Sin cooldown en este test: cada lectura con el contenedor caído recuerda.
    assert len(notifier.sent) == 2
    assert "Activa desde" in notifier.sent[1]


async def test_manual_stop_is_warning_even_with_code_137(setup) -> None:
    head, source, alerter, notifier, _ = setup
    await head.check()
    # Un proceso que ignora SIGTERM: docker stop acaba matándolo (137).
    source.set("nginx", state="exited", exit_code=137)
    source.stops = {"nginx"}
    await head.check()
    await head.check()
    assert alerter.store.get("contenedores:caido:nginx").level == "warning"
    assert len(notifier.sent) == 1
    assert "Contenedor nginx parado a mano (docker stop, código 137)" in notifier.sent[0]


async def test_oom(setup) -> None:
    head, source, alerter, notifier, _ = setup
    await head.check()
    source.set("jellyfin", state="exited", exit_code=137, oom_killed=True)
    await head.check()
    assert "sin memoria (OOM, código 137)" in notifier.sent[0]
    assert alerter.store.get("contenedores:caido:jellyfin").level == "critical"


async def test_stopped_container_that_starts_and_falls_does_alert(setup) -> None:
    head, source, alerter, _, _ = setup
    await head.check()
    source.set("viejo", state="running")
    await head.check()
    source.set("viejo", state="exited", exit_code=2)
    await head.check()
    assert alerter.is_active("contenedores:caido:viejo")


async def test_restart_loop(setup) -> None:
    head, source, alerter, notifier, mono = setup
    await head.check()
    source.set("nginx", restart_count=3)
    mono.t += 30
    await head.check()
    assert not alerter.is_active("contenedores:reinicios:nginx")
    source.set("nginx", restart_count=6)  # 6 reinicios en 1 min > 5
    mono.t += 30
    await head.check()
    assert alerter.is_active("contenedores:reinicios:nginx")
    assert "en bucle de reinicios: 6 en 10 min (umbral 5)" in notifier.sent[0]
    # Pasada la ventana sin reinicios nuevos, se da por estable.
    mono.t += 10 * 60 + 1
    await head.check()
    assert not alerter.is_active("contenedores:reinicios:nginx")
    assert "estable: sin reinicios en 10 min" in notifier.sent[-1]


async def test_restarts_spread_over_time_do_not_alert(setup) -> None:
    head, source, alerter, _, mono = setup
    await head.check()
    for count in range(1, 12):
        mono.t += 5 * 60  # un reinicio cada 5 min: 2 por ventana
        source.set("nginx", restart_count=count)
        await head.check()
    assert not alerter.is_active("contenedores:reinicios:nginx")


async def test_recreated_container_resets_restart_count(setup) -> None:
    head, source, alerter, _, _ = setup
    source.set("nginx", restart_count=40)
    await head.check()
    source.set("nginx", id="id-nuevo", restart_count=0)
    await head.check()
    source.set("nginx", restart_count=2)
    await head.check()
    assert not alerter.is_active("contenedores:reinicios:nginx")


async def test_unhealthy(setup) -> None:
    head, source, alerter, notifier, _ = setup
    source.set("nginx", health="unhealthy", failing_streak=3, health_output="curl: (7) refused")
    await head.check()  # también en la lectura de referencia: es un problema en curso
    assert alerter.store.get("contenedores:unhealthy:nginx").level == "warning"
    assert (
        "Contenedor nginx unhealthy (3 comprobaciones fallidas seguidas): curl: (7) refused"
        in notifier.sent[0]
    )
    source.set("nginx", health="starting")
    await head.check()
    assert alerter.is_active("contenedores:unhealthy:nginx")
    source.set("nginx", health="healthy")
    await head.check()
    assert not alerter.is_active("contenedores:unhealthy:nginx")


async def test_removed_container_closes_its_alerts(setup) -> None:
    head, source, alerter, _, _ = setup
    await head.check()
    source.set("nginx", state="exited", exit_code=1)
    await head.check()
    source.infos = [i for i in source.infos if i.name != "nginx"]
    await head.check()
    assert not alerter.is_active("contenedores:caido:nginx")


async def test_ignored_containers(setup) -> None:
    head, source, alerter, _, _ = setup
    source.infos.append(running("tarea"))
    await head.check()
    source.set("tarea", state="exited", exit_code=1)
    await head.check()
    assert alerter.store.list() == []
    # Pero siguen apareciendo en el estado.
    assert "tarea" in [c.name for c in head.snapshot]


async def test_docker_unreachable(setup) -> None:
    head, source, alerter, notifier, _ = setup
    await head.check()
    assert head.healthy()
    source.error = ConnectionError("socket no encontrado")
    await head.check()
    assert not alerter.is_active("contenedores:docker")
    await head.check()
    assert alerter.is_active("contenedores:docker")
    assert "No se puede consultar Docker: socket no encontrado" in notifier.sent[0]
    source.error = None
    await head.check()
    assert not alerter.is_active("contenedores:docker")
    assert "Docker responde de nuevo" in notifier.sent[-1]


def test_parse_inspect() -> None:
    info = parse_inspect(
        {
            "Id": "abc",
            "Name": "/nginx",
            "RestartCount": 4,
            "State": {
                "Status": "running",
                "ExitCode": 0,
                "OOMKilled": False,
                "Health": {
                    "Status": "unhealthy",
                    "FailingStreak": 5,
                    "Log": [
                        {"ExitCode": 0, "Output": "ok"},
                        {"ExitCode": 1, "Output": "  curl: (7)\n  Failed to connect  "},
                    ],
                },
            },
        }
    )
    assert info == ContainerInfo(
        name="nginx",
        id="abc",
        state="running",
        restart_count=4,
        health="unhealthy",
        health_output="curl: (7) Failed to connect",
        failing_streak=5,
    )
    bare = parse_inspect({"Id": "x", "Name": "/y", "State": {"Status": "exited", "ExitCode": 3}})
    assert bare.health is None and bare.exit_code == 3
