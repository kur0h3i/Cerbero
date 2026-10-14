#!/usr/bin/env python3
"""Informes de tareas del host para Cerbero.

Cada tarea programada (un timer de systemd) deja su último resultado en
``/var/lib/cerbero/tareas/<tarea>.json``; Cerbero lo lee y avisa si falla, si
deja de ejecutarse o, si la tarea lo pide, con un resumen de lo que hizo::

    {"version": 1, "tarea": "limpieza-docker", "resultado": "ok",
     "fin": "2026-10-18T04:31:07+02:00", "cada_s": 604800,
     "resumen": "🧹 Liberados 566 MB (12 imágenes)", "avisar": true, "detalle": {...}}

Uso desde una unidad de systemd (``ExecStopPost=`` se ejecuta también cuando
el servicio falla, y systemd le pasa ``$SERVICE_RESULT`` y ``$EXIT_STATUS``)::

    ExecStopPost=/usr/bin/python3 %h/.local/lib/cerbero/informe_tarea.py \\
        systemd --tarea cola-tareas --cada 1800

O desde otro script, importando ``write_report``.

Solo usa la biblioteca estándar (Python 3.11+).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

OUT_DIR = Path(os.environ.get("CERBERO_TAREAS_DIR", "/var/lib/cerbero/tareas"))
FORMAT_VERSION = 1
# Con --solo-fallos, un fallo que la propia tarea explicó hace menos de esto no
# se pisa con el genérico de systemd.
OWN_REPORT_S = 600
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def build_report(
    task: str,
    ok: bool,
    every_s: int,
    summary: str,
    notify: bool = False,
    detail: dict | None = None,
    now: datetime | None = None,
) -> dict:
    if not _NAME.match(task):
        raise ValueError(f"nombre de tarea no válido: {task!r}")
    when = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    return {
        "version": FORMAT_VERSION,
        "tarea": task,
        "resultado": "ok" if ok else "fallo",
        "fin": when,
        "cada_s": int(every_s),
        "resumen": summary,
        "avisar": notify,
        "detalle": detail or {},
    }


def write_report(report: dict, out_dir: Path | None = None) -> Path:
    """Escribe el informe de forma atómica (Cerbero nunca lee uno a medias)."""
    out_dir = out_dir or OUT_DIR
    path = out_dir / f"{report['tarea']}.json"
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False)
        os.chmod(tmp, 0o644)  # Cerbero lo lee como otro usuario
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def from_systemd(task: str, every_s: int, env: dict[str, str]) -> dict:
    """Informe a partir de las variables que systemd pasa a ``ExecStopPost=``."""
    result = env.get("SERVICE_RESULT", "")
    ok = result == "success"
    if ok:
        summary = "Terminada correctamente"
    else:
        # EXIT_CODE: exited, killed, dumped...; EXIT_STATUS: el código o la señal.
        how = " ".join(v for v in (env.get("EXIT_CODE"), env.get("EXIT_STATUS")) if v)
        summary = f"Ha fallado ({result or 'sin resultado'}" + (f", {how})" if how else ")")
    detail = {k: env[k] for k in ("SERVICE_RESULT", "EXIT_CODE", "EXIT_STATUS") if k in env}
    return build_report(task, ok, every_s, summary, detail=detail)


def check_output(report: dict, path: Path, now: datetime) -> dict:
    """Si la tarea terminó bien, comprueba que ha actualizado su fichero de salida.

    Un servicio puede «terminar bien» sin haber hecho su trabajo. Se da por no
    actualizado si el fichero tiene más de un periodo (``cada_s``): cada
    ejecución debería haberlo reescrito.
    """
    if report["resultado"] != "ok":
        return report
    try:
        age = now.timestamp() - path.stat().st_mtime
    except OSError as exc:
        problem = f"no se puede leer {path}: {exc.strerror or type(exc).__name__}"
    else:
        if age <= report["cada_s"]:
            report["detalle"]["salida_actualizada_hace_s"] = int(age)
            return report
        problem = f"terminó bien pero {path.name} lleva {age / 60:.0f} min sin actualizarse"
    report.update(resultado="fallo", resumen=problem.capitalize())
    return report


def explained_recently(path: Path, now: datetime, within_s: int = OWN_REPORT_S) -> bool:
    """¿La tarea ya dejó un informe de fallo propio (con más detalle) hace poco?"""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        when = datetime.fromisoformat(report["fin"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return report.get("resultado") == "fallo" and (now - when).total_seconds() < within_s


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Informe de una tarea para Cerbero")
    sub = parser.add_subparsers(dest="modo", required=True)

    sd = sub.add_parser("systemd", help="desde ExecStopPost= de una unidad")
    sd.add_argument("--tarea", required=True)
    sd.add_argument("--cada", type=int, required=True, help="segundos entre ejecuciones")
    sd.add_argument(
        "--solo-fallos",
        action="store_true",
        help="no escribir si fue bien (la propia tarea deja un informe más detallado)",
    )
    sd.add_argument(
        "--fichero",
        type=Path,
        help="fichero que la tarea debe reescribir en cada ejecución (su JSON de estado)",
    )

    w = sub.add_parser("escribir", help="informe a mano")
    w.add_argument("--tarea", required=True)
    w.add_argument("--resultado", choices=("ok", "fallo"), required=True)
    w.add_argument("--cada", type=int, required=True)
    w.add_argument("--resumen", default="")
    w.add_argument("--avisar", action="store_true")

    args = parser.parse_args(argv)
    if args.modo == "systemd":
        report = from_systemd(args.tarea, args.cada, dict(os.environ))
        if args.fichero:
            report = check_output(report, args.fichero, datetime.now().astimezone())
        if args.solo_fallos:
            own = OUT_DIR / f"{args.tarea}.json"
            now = datetime.now().astimezone()
            if report["resultado"] == "ok" or explained_recently(own, now):
                return 0
    else:
        report = build_report(
            args.tarea, args.resultado == "ok", args.cada, args.resumen, args.avisar
        )
    path = write_report(report)
    print(f"{path}: {report['resultado']} — {report['resumen']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
