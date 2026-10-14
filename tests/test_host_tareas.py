"""Tests de los scripts del host ``host/tareas/`` (corren como el usuario kuro)."""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

HOST = Path(__file__).resolve().parent.parent / "host/tareas"
NOW = datetime(2026, 10, 18, 4, 31, 7, tzinfo=ZoneInfo("Europe/Madrid"))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, HOST / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


informe = load("informe_tarea")
limpieza = load("cerbero_limpieza")


def test_build_report() -> None:
    report = informe.build_report(
        "limpieza-docker", True, 604800, "🧹 Liberados 566 MB", notify=True, now=NOW
    )
    assert report == {
        "version": 1,
        "tarea": "limpieza-docker",
        "resultado": "ok",
        "fin": "2026-10-18T04:31:07+02:00",
        "cada_s": 604800,
        "resumen": "🧹 Liberados 566 MB",
        "avisar": True,
        "detalle": {},
    }
    with pytest.raises(ValueError):
        informe.build_report("../fuera", True, 1, "")


def test_write_report_is_atomic_and_world_readable(tmp_path: Path) -> None:
    path = informe.write_report(informe.build_report("cola-tareas", True, 1800, "ok"), tmp_path)
    assert path == tmp_path / "cola-tareas.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert [p.name for p in tmp_path.iterdir()] == ["cola-tareas.json"]


@pytest.mark.parametrize(
    ("env", "ok", "summary"),
    [
        (
            {"SERVICE_RESULT": "success", "EXIT_CODE": "exited", "EXIT_STATUS": "0"},
            True,
            "Terminada correctamente",
        ),
        (
            {"SERVICE_RESULT": "exit-code", "EXIT_CODE": "exited", "EXIT_STATUS": "1"},
            False,
            "Ha fallado (exit-code, exited 1)",
        ),
        (
            {"SERVICE_RESULT": "timeout", "EXIT_CODE": "killed", "EXIT_STATUS": "TERM"},
            False,
            "Ha fallado (timeout, killed TERM)",
        ),
        ({}, False, "Ha fallado (sin resultado)"),
    ],
)
def test_from_systemd(env: dict, ok: bool, summary: str) -> None:
    report = informe.from_systemd("cola-tareas", 1800, env)
    assert (report["resultado"] == "ok", report["resumen"]) == (ok, summary)


def _cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: dict, *args: str) -> dict | None:
    monkeypatch.setattr(informe, "OUT_DIR", tmp_path)
    for key in ("SERVICE_RESULT", "EXIT_CODE", "EXIT_STATUS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert informe.main(["systemd", "--tarea", "limpieza-docker", "--cada", "604800", *args]) == 0
    path = tmp_path / "limpieza-docker.json"
    return json.loads(path.read_text()) if path.exists() else None


def test_cli_solo_fallos_skips_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _cli(monkeypatch, tmp_path, {"SERVICE_RESULT": "success"}, "--solo-fallos") is None


def test_cli_solo_fallos_reports_a_crash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report = _cli(monkeypatch, tmp_path, {"SERVICE_RESULT": "exit-code"}, "--solo-fallos")
    assert report["resultado"] == "fallo"


def test_cli_solo_fallos_keeps_the_tasks_own_explanation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    own = informe.build_report("limpieza-docker", False, 604800, "No se pudo limpiar: daemon caído")
    informe.write_report(own, tmp_path)
    report = _cli(monkeypatch, tmp_path, {"SERVICE_RESULT": "exit-code"}, "--solo-fallos")
    assert report["resumen"] == "No se pudo limpiar: daemon caído"


def test_explained_recently(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    informe.write_report(informe.build_report("t", False, 60, "x", now=NOW), tmp_path)
    assert informe.explained_recently(path, NOW + timedelta(minutes=5))
    assert not informe.explained_recently(path, NOW + timedelta(minutes=11))
    assert not informe.explained_recently(tmp_path / "no-existe.json", NOW)


# --- Limpieza ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Deleted Images:\nuntagged: a:1\n\nTotal reclaimed space: 566.2MB\n", 566_200_000),
        ("Total reclaimed space: 1.3GB", 1_300_000_000),
        ("Total reclaimed space: 512kB", 512_000),
        ("Total reclaimed space: 0B", 0),
        ("", 0),
    ],
)
def test_reclaimed_bytes(output: str, expected: int) -> None:
    assert limpieza.reclaimed_bytes(output) == expected


def test_summarize() -> None:
    assert limpieza.summarize(566_200_000, 12) == ("🧹 Liberados 566 MB (12 imágenes)", True)
    assert limpieza.summarize(1_300_000_000, 1) == ("🧹 Liberados 1.3 GB (1 imagen)", True)
    assert limpieza.summarize(0, 0) == ("🧹 Nada que limpiar", False)


class FakeDocker:
    def __init__(self, before: str, after: str, prune: str, fail: str | None = None) -> None:
        self.lists = [before, after]
        self.prune, self.fail = prune, fail
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(cmd)
        if "prune" in cmd:
            if self.fail:
                raise subprocess.CalledProcessError(1, cmd, "", self.fail)
            return subprocess.CompletedProcess(cmd, 0, self.prune, "")
        return subprocess.CompletedProcess(cmd, 0, self.lists.pop(0), "")


def test_limpieza_writes_summary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeDocker(
        "sha256:a\nsha256:b\nsha256:c\n", "sha256:c\n", "Total reclaimed space: 566.2MB"
    )
    monkeypatch.setattr(limpieza, "_run", fake)
    monkeypatch.setattr(informe, "OUT_DIR", tmp_path)
    monkeypatch.setattr(limpieza, "write_report", lambda r: informe.write_report(r, tmp_path))
    assert limpieza.main() == 0
    assert ["docker", "image", "prune", "--all", "--force", "--filter", "until=168h"] in fake.calls
    report = json.loads((tmp_path / "limpieza-docker.json").read_text())
    assert report["resultado"] == "ok"
    assert report["resumen"] == "🧹 Liberados 566 MB (2 imágenes)"
    assert report["avisar"] is True
    assert report["detalle"] == {
        "bytes_liberados": 566_200_000,
        "imagenes_borradas": 2,
        "imagenes": 1,
    }


def test_limpieza_reports_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeDocker(
        "", "", "", fail="Cannot connect to the Docker daemon at unix:///run/docker.sock"
    )
    monkeypatch.setattr(limpieza, "_run", fake)
    monkeypatch.setattr(limpieza, "write_report", lambda r: informe.write_report(r, tmp_path))
    assert limpieza.main() == 1
    report = json.loads((tmp_path / "limpieza-docker.json").read_text())
    assert report["resultado"] == "fallo"
    assert report["resumen"] == (
        "No se pudo limpiar: Cannot connect to the Docker daemon at unix:///run/docker.sock"
    )
