from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.alerter import Alerter
from app.config import Settings
from app.heads.accesos import AccesosHead, LogTailer
from app.heads.contenedores import ContainerInfo, ContenedoresHead
from app.heads.recursos import DiskSample, RecursosHead, Sample
from app.main import create_app
from tests.conftest import FakeNotifier

NEW = "2026-09-30T03:14:22+02:00 server-kuro sshd-session[1]: "


class FakeSampler:
    def __init__(self) -> None:
        self.next = Sample(
            cpu_pct=12.5,
            ram_free_gb=0.9,
            ram_total_gb=11.6,
            disks=[
                DiskSample("/", 100, 40, 60, 40),
                DiskSample("/srv/extra", error="no está montado"),
            ],
        )

    def sample(self) -> Sample:
        return self.next


class FakeSource:
    def list(self) -> list[ContainerInfo]:
        return [
            ContainerInfo("nginx", "a", "running", restart_count=2, health="healthy"),
            ContainerInfo("backup", "b", "exited", exit_code=0),
        ]

    def manual_stops(self, since: float, until: float) -> set[str]:
        return set()


@pytest.fixture
def client(tmp_path: Path, settings: Settings, notifier: FakeNotifier):
    log = tmp_path / "auth.log"
    log.write_text("")

    def heads(s: Settings, alerter: Alerter):
        return (
            RecursosHead(s, alerter, sampler=FakeSampler()),  # type: ignore[arg-type]
            ContenedoresHead(s, alerter, source=FakeSource()),
            AccesosHead(s, alerter, tailer=LogTailer(str(log))),
        )

    app = create_app(settings, notifier=notifier, heads=heads)
    with TestClient(app) as c:
        c.log = log  # type: ignore[attr-defined]
        _until(lambda: c.get("/api/health").json()["status"] == "ok")
        yield c


def _until(condition, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("la condición no se cumplió a tiempo")


def test_health(client: TestClient) -> None:
    assert client.get("/api/health").json() == {
        "status": "ok",
        "heads": {"recursos": True, "contenedores": True, "accesos": True},
    }


def test_alerts_contract_with_dis(client: TestClient, notifier: FakeNotifier) -> None:
    body = client.get("/api/alerts").json()
    assert body["connected"] is True
    by_key = {a["key"]: a for a in body["alerts"]}
    ram = by_key["recursos:ram"]
    assert set(ram) == {
        "id",
        "key",
        "level",
        "head",
        "message",
        "timestamp",
        "active",
        "resolved_at",
    }
    assert ram["level"] == "critical"
    assert ram["head"] == "recursos"
    assert ram["message"] == "RAM libre 0.9 GB (umbral 1.5 GB)"
    assert ram["active"] is True
    assert "recursos:disco_ausente:/srv/extra" in by_key
    # El aviso de arranque y las dos alertas llegan a Telegram.
    assert any("Cerbero en guardia" in m for m in notifier.sent)


def test_alerts_include_security_events(client: TestClient) -> None:
    with client.log.open("a") as fh:  # type: ignore[attr-defined]
        fh.write(NEW + "Accepted publickey for kuro from 198.51.100.4 port 9 ssh2\n")
    _until(
        lambda: any(
            a["key"] == "accesos:login_externo:kuro@198.51.100.4"
            for a in client.get("/api/alerts").json()["alerts"]
        )
    )


def test_alerts_active_filter(client: TestClient) -> None:
    alerts = client.get("/api/alerts", params={"active": "false"}).json()["alerts"]
    assert alerts == []
    alerts = client.get("/api/alerts", params={"active": "true"}).json()["alerts"]
    assert alerts and all(a["active"] for a in alerts)


def test_status(client: TestClient) -> None:
    assert client.get("/api/status").json() == {
        "cpu_pct": 12.5,
        "ram_free_gb": 0.9,
        "ram_total_gb": 11.6,
        "disks": [
            {
                "mount": "/",
                "total_gb": 100.0,
                "used_gb": 40.0,
                "free_gb": 60.0,
                "percent": 40.0,
                "error": None,
            },
            {
                "mount": "/srv/extra",
                "total_gb": 0.0,
                "used_gb": 0.0,
                "free_gb": 0.0,
                "percent": 0.0,
                "error": "no está montado",
            },
        ],
        "containers": [
            {"name": "backup", "status": "exited", "health": None, "restarts": 0},
            {"name": "nginx", "status": "running", "health": "healthy", "restarts": 2},
        ],
    }


def test_health_degraded_when_a_head_is_blind(
    tmp_path: Path, settings: Settings, notifier: FakeNotifier
) -> None:
    def heads(s: Settings, alerter: Alerter):
        return (
            RecursosHead(s, alerter, sampler=FakeSampler()),  # type: ignore[arg-type]
            ContenedoresHead(s, alerter, source=FakeSource()),
            AccesosHead(s, alerter, tailer=LogTailer(str(tmp_path / "no-existe.log"))),
        )

    with TestClient(create_app(settings, notifier=notifier, heads=heads)) as c:
        _until(lambda: c.get("/api/health").json()["heads"]["recursos"])
        body = c.get("/api/health").json()
        assert body["status"] == "degraded"
        assert body["heads"]["accesos"] is False


def test_api_is_read_only(client: TestClient) -> None:
    for path in ("/api/alerts", "/api/status", "/api/health"):
        assert client.post(path).status_code == 405
        assert client.delete(path).status_code == 405
