from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.alerter import Alerter
from app.config import Settings
from app.heads.accesos import AccesosHead, LogTailer
from app.heads.contenedores import ContainerInfo, ContenedoresHead
from app.heads.recursos import DiskSample, RecursosHead, Sample
from app.heads.smart import SmartHead
from app.heads.tareas import TareasHead
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
    smart_dir = tmp_path / "smart"
    smart_dir.mkdir()
    disk = json.loads((Path(__file__).parent / "fixtures/smartctl_nvme.json").read_text())
    now = settings.now().isoformat()
    head = {"nombre": "sistema", "montaje": "/", "estado": "ok", "comprobado": now, "leido": now}
    (smart_dir / "sistema.json").write_text(json.dumps({"cerbero": head, "smartctl": disk}))
    tareas_dir = tmp_path / "tareas"
    tareas_dir.mkdir()
    informe = {
        "tarea": "cola-tareas",
        "resultado": "ok",
        "fin": settings.now().replace(tzinfo=None).isoformat(),  # sin zona
        "cada_s": 1800,
        "resumen": "ok",
    }
    (tareas_dir / "cola-tareas.json").write_text(json.dumps(informe))
    settings = settings.model_copy(
        update={"smart_dir": str(smart_dir), "tareas_dir": str(tareas_dir)}
    )

    def heads(s: Settings, alerter: Alerter):
        return (
            RecursosHead(s, alerter, sampler=FakeSampler()),  # type: ignore[arg-type]
            ContenedoresHead(s, alerter, source=FakeSource()),
            AccesosHead(s, alerter, tailer=LogTailer(str(log))),
            SmartHead(s, alerter),
            TareasHead(s, alerter),
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
        "heads": {
            "recursos": True,
            "contenedores": True,
            "accesos": True,
            "smart": True,
            "tareas": True,
        },
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


def test_status_lists_recent_logins(client: TestClient) -> None:
    with client.log.open("a") as fh:  # type: ignore[attr-defined]
        fh.write(NEW + "Accepted publickey for kuro from 192.168.1.20 port 9 ssh2\n")
        fh.write(NEW + "Accepted password for kuro from 198.51.100.4 port 9 ssh2\n")
        fh.write(NEW + "Failed password for root from 203.0.113.5 port 1 ssh2\n")

    def accesos() -> dict:
        return client.get("/api/status").json()["accesos"]

    _until(lambda: accesos()["lineas"] == 3)
    data = accesos()
    assert [(a["ip"], a["confianza"]) for a in data["ultimos_accesos"]] == [
        ("198.51.100.4", False),  # el más reciente primero
        ("192.168.1.20", True),
    ]
    assert (data["ips_seguidas"], data["fallos_ventana"]) == (1, 1)


def test_alerts_active_filter(client: TestClient) -> None:
    alerts = client.get("/api/alerts", params={"active": "false"}).json()["alerts"]
    assert alerts == []
    alerts = client.get("/api/alerts", params={"active": "true"}).json()["alerts"]
    assert alerts and all(a["active"] for a in alerts)


def test_status(client: TestClient) -> None:
    body = client.get("/api/status").json()
    assert body.pop("servidor") == "server-kuro"
    umbrales = body.pop("umbrales")
    assert (umbrales["cpu_pct"], umbrales["ram_libre_min_gb"], umbrales["fuerza_bruta"]) == (
        85,
        1.5,
        10,
    )
    assert umbrales["redes_confianza"] == ["192.168.1.0/24", "10.0.0.0/24", "100.64.0.0/10"]
    accesos = body.pop("accesos")
    assert (accesos["legible"], accesos["ultimos_accesos"]) == (True, [])
    (task,) = body.pop("tareas")
    assert (task["tarea"], task["resultado"], task["parada"]) == ("cola-tareas", "ok", False)
    (disk,) = body.pop("smart")
    assert disk["nombre"] == "sistema"
    assert disk["montaje"] == "/"
    assert disk["modelo"] == "Micron_2400_MTFDKBA512QFM"
    assert (disk["salud"], disk["temp_c"], disk["desgaste_pct"]) == (True, 41, 3)
    assert body == {
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


def test_status_survives_a_report_date_without_zone(client: TestClient) -> None:
    response = client.get("/api/status")
    assert response.status_code == 200
    (task,) = response.json()["tareas"]
    assert task["fin"].endswith(("+01:00", "+02:00")) and task["parada"] is False


def test_health_degraded_when_a_head_is_blind(
    tmp_path: Path, settings: Settings, notifier: FakeNotifier
) -> None:
    def heads(s: Settings, alerter: Alerter):
        return (
            RecursosHead(s, alerter, sampler=FakeSampler()),  # type: ignore[arg-type]
            ContenedoresHead(s, alerter, source=FakeSource()),
            AccesosHead(s, alerter, tailer=LogTailer(str(tmp_path / "no-existe.log"))),
            SmartHead(s, alerter),
            TareasHead(s, alerter),
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


def test_favicon_for_dis(client: TestClient) -> None:
    resp = client.get("/favicon.svg")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/svg+xml"
    assert resp.text.lstrip().startswith("<svg")


def test_web_is_served_with_strict_headers(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "<title>Cerbero</title>" in resp.text
    csp = resp.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    for path in ("/static/app.js", "/static/app.css", "/static/theme.js", "/logo.svg"):
        asset = client.get(path)
        assert asset.status_code == 200, path
        assert asset.headers["x-content-type-options"] == "nosniff"
    # Swagger carga JS de un CDN: la CSP de la web no debe romperlo.
    assert "content-security-policy" not in client.get("/api/docs").headers


def test_web_has_no_inline_scripts(client: TestClient) -> None:
    import re

    html = client.get("/").text
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S)
    assert scripts and all(not body.strip() for body in scripts)
    assert "style=" not in html
