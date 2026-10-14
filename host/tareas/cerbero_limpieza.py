#!/usr/bin/env python3
"""Limpieza semanal de imágenes Docker, con informe para Cerbero.

Lo lanza ``cerbero-limpieza.timer`` (timer de usuario: basta con estar en el
grupo docker). Borra las imágenes sin usar de más de 7 días y deja el
resultado en ``/var/lib/cerbero/tareas/limpieza-docker.json``; Cerbero manda
por Telegram «🧹 Liberados X MB» o un 🟡 si ha fallado.

Cerbero sigue sin escribir en Docker: quien borra es este script, con los
permisos del usuario.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from informe_tarea import build_report, write_report

TASK = "limpieza-docker"
EVERY_S = 7 * 24 * 3600
PRUNE = ["docker", "image", "prune", "--all", "--force", "--filter", "until=168h"]
LIST = ["docker", "image", "ls", "--all", "--quiet", "--no-trunc"]
TIMEOUT_S = 1800

# Docker escribe el tamaño en unidades decimales: «566.2MB», «1.3GB», «0B».
_RECLAIMED = re.compile(r"Total reclaimed space:\s*([\d.]+)\s*([kMGT]?B)", re.IGNORECASE)
_UNITS = {"b": 1, "kb": 10**3, "mb": 10**6, "gb": 10**9, "tb": 10**12}


def reclaimed_bytes(output: str) -> int:
    m = _RECLAIMED.search(output)
    return int(float(m[1]) * _UNITS[m[2].lower()]) if m else 0


def human(size: int) -> str:
    return f"{size / 10**9:.1f} GB" if size >= 10**9 else f"{size / 10**6:.0f} MB"


def summarize(freed: int, removed: int) -> tuple[str, bool]:
    """Resumen y si merece un aviso (no se avisa de que no había nada que borrar)."""
    if freed <= 0 and removed == 0:
        return "🧹 Nada que limpiar", False
    images = "1 imagen" if removed == 1 else f"{removed} imágenes"
    return f"🧹 Liberados {human(freed)} ({images})", True


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S, check=True)


def main() -> int:
    try:
        before = set(_run(LIST).stdout.split())
        prune = _run(PRUNE)
        after = set(_run(LIST).stdout.split())
    except (OSError, subprocess.SubprocessError) as exc:
        stderr = getattr(exc, "stderr", None) or ""
        reason = (stderr.strip().splitlines() or [str(exc)])[-1][:200]
        write_report(build_report(TASK, False, EVERY_S, f"No se pudo limpiar: {reason}"))
        print(f"Fallo: {reason}", file=sys.stderr)
        # El fallo ya está explicado en el informe; el ExecStopPost no lo pisa.
        return 1
    freed = reclaimed_bytes(prune.stdout)
    removed = len(before - after)
    summary, notify = summarize(freed, removed)
    detail = {"bytes_liberados": freed, "imagenes_borradas": removed, "imagenes": len(after)}
    write_report(build_report(TASK, True, EVERY_S, summary, notify, detail))
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
