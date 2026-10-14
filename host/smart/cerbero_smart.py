#!/usr/bin/env python3
"""Volcado SMART para Cerbero. Corre en el host como root, desde cerbero-smart.timer.

Lee ``/etc/cerbero/discos.conf`` y, por cada disco, guarda la salida de
``smartctl --json -a`` en ``/var/lib/cerbero/smart/<nombre>.json`` junto con una
cabecera ``cerbero`` que dice cuándo se comprobó y cómo fue. Cerbero, que no
tiene privilegios, solo lee esos ficheros.

- Escritura atómica (temporal + rename): Cerbero nunca lee un JSON a medias.
- No despierta un disco en reposo (``-n standby``) salvo que la última lectura
  completa tenga más de 24 h; mientras, conserva los datos de esa lectura.
- Si el modo automático no entiende el puente USB, prueba ``-d sat``.
- Un disco desconectado queda como ``ausente``.

Uso::

    cerbero_smart.py              vuelca todos los discos de discos.conf
    cerbero_smart.py --detectar   propone un discos.conf para este servidor

Solo usa la biblioteca estándar (Python 3.11+).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

CONF = "/etc/cerbero/discos.conf"
OUT_DIR = "/var/lib/cerbero/smart"
BY_ID = "/dev/disk/by-id"
# Pasado este tiempo sin una lectura completa, se lee aunque despierte al disco.
FORCE_AFTER = timedelta(hours=24)
TIMEOUT_S = 120
FORMAT_VERSION = 1

# Bit del código de salida de smartctl: no se pudo abrir el disco (o, con -n,
# estaba en reposo y no se leyó). Ver «EXIT STATUS» en man smartctl.
EXIT_OPEN = 2
_POWER_MODE = re.compile(r"Device is in (STANDBY|SLEEP)\b", re.IGNORECASE)
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# ``run(dispositivo, tipo, no_despertar) -> (código, json)``; inyectable en tests.
Runner = Callable[[str, str | None, bool], tuple[int, dict]]


@dataclass(frozen=True)
class Disk:
    name: str
    device: str
    mount: str | None = None
    optional: bool = False
    dtype: str | None = None  # None = automático


def parse_conf(text: str) -> list[Disk]:
    """``nombre dispositivo [montaje|-] [opcional] [tipo=sat]``, uno por línea."""
    disks: list[Disk] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, *rest = line.split()
        if not rest:
            raise ValueError(f"línea {lineno}: falta el dispositivo")
        if not _NAME.match(name) or name in seen:
            raise ValueError(f"línea {lineno}: nombre no válido o repetido: {name!r}")
        seen.add(name)
        device, *opts = rest
        mount = None
        if opts and (opts[0].startswith("/") or opts[0] == "-"):
            mount = None if opts[0] == "-" else opts[0]
            opts = opts[1:]
        optional, dtype = False, None
        for opt in opts:
            if opt == "opcional":
                optional = True
            elif opt.startswith("tipo="):
                dtype = opt.split("=", 1)[1] or None
            else:
                raise ValueError(f"línea {lineno}: opción desconocida {opt!r}")
        disks.append(Disk(name, device, mount, optional, dtype))
    return disks


def run_smartctl(device: str, dtype: str | None, no_wake: bool) -> tuple[int, dict]:
    cmd = ["smartctl", "--json=c", "-a"]
    if no_wake:
        cmd += ["-n", "standby"]
    if dtype:
        cmd += ["-d", dtype]
    cmd.append(device)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return EXIT_OPEN, {"cerbero_error": f"smartctl no respondió en {TIMEOUT_S} s"}
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        data = {"cerbero_error": (proc.stderr or proc.stdout).strip()[:300]}
    return proc.returncode, data if isinstance(data, dict) else {}


def _messages(data: dict) -> list[str]:
    msgs = (data.get("smartctl") or {}).get("messages") or []
    return [m.get("string", "") for m in msgs if isinstance(m, dict)]


def _in_standby(code: int, data: dict) -> bool:
    return bool(code & EXIT_OPEN) and any(_POWER_MODE.search(m) for m in _messages(data))


# Qué significa cada bit del código de salida cuando no hay datos (man smartctl).
_EXIT_REASONS = {
    1: "argumentos no válidos",
    2: "no se pudo abrir el disco o no respondió a IDENTIFY (¿puente USB sin SMART?)",
    4: "falló un comando SMART",
}


def _error_text(code: int, data: dict) -> str:
    """Motivo legible de que no haya datos SMART.

    Algunos errores (como «Read Device Identity failed») smartctl no los mete en
    los mensajes del JSON: entonces queda el código de salida.
    """
    if err := data.get("cerbero_error"):
        return str(err)
    if msgs := [m for m in _messages(data) if m]:
        return "; ".join(msgs)[:300]
    reasons = [text for bit, text in _EXIT_REASONS.items() if code & bit]
    return f"código {code}" + (f": {', '.join(reasons)}" if reasons else "")


def _parse_time(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def check_disk(disk: Disk, previous: dict | None, now: datetime, run: Runner) -> dict:
    """Comprueba un disco y devuelve el JSON a guardar."""
    prev_head = (previous or {}).get("cerbero") or {}
    prev_data = (previous or {}).get("smartctl")
    last_read = _parse_time(prev_head.get("leido"))
    header = {
        "version": FORMAT_VERSION,
        "nombre": disk.name,
        "dispositivo": disk.device,
        "montaje": disk.mount,
        "opcional": disk.optional,
        "comprobado": now.isoformat(timespec="seconds"),
        "leido": prev_head.get("leido"),
        "tipo": prev_head.get("tipo") or disk.dtype,
        "estado": "ok",
        "error": None,
    }
    if not os.path.exists(disk.device):
        header["estado"] = "ausente"
        return {"cerbero": header, "smartctl": prev_data}

    no_wake = last_read is not None and now - last_read < FORCE_AFTER
    # Si la vez anterior hizo falta ``-d sat``, se empieza por ahí.
    dtype = disk.dtype or (prev_head.get("tipo") if prev_head.get("tipo") != "auto" else None)
    code, data = run(disk.device, dtype, no_wake)
    if _in_standby(code, data):
        header["estado"] = "standby"
        return {"cerbero": header, "smartctl": prev_data}
    errors = {"auto" if dtype is None else dtype: _error_text(code, data)}
    if "smart_status" not in data and dtype is None:
        # Puente USB que el modo automático no reconoce: casi siempre es SAT.
        code, retry = run(disk.device, "sat", no_wake)
        if _in_standby(code, retry):
            header.update(estado="standby", tipo="sat")
            return {"cerbero": header, "smartctl": prev_data}
        if "smart_status" in retry:
            data, dtype = retry, "sat"
        else:
            errors["sat"] = _error_text(code, retry)
    if "smart_status" not in data:
        texts = list(dict.fromkeys(errors.values()))
        error = texts[0] if len(texts) == 1 else "; ".join(f"{t}: {e}" for t, e in errors.items())
        header.update(estado="error", error=error)
        return {"cerbero": header, "smartctl": prev_data}
    header.update(leido=header["comprobado"], tipo=dtype or "auto")
    return {"cerbero": header, "smartctl": data}


def write_atomic(path: Path, payload: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.chmod(tmp, 0o644)  # Cerbero lo lee como otro usuario
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def dump_all(disks: list[Disk], out_dir: Path, now: datetime, run: Runner = run_smartctl) -> int:
    """Vuelca todos los discos; devuelve cuántos dieron error (para el log del timer)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    errors = 0
    for disk in disks:
        path = out_dir / f"{disk.name}.json"
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = None
        result = check_disk(disk, previous, now, run)
        write_atomic(path, result)
        head = result["cerbero"]
        print(f"{disk.name}: {head['estado']}" + (f" ({head['error']})" if head["error"] else ""))
        errors += head["estado"] == "error"
    # Los discos que ya no están en discos.conf dejan de vigilarse: si su JSON se
    # quedara, Cerbero avisaría de que lleva horas sin actualizarse.
    configured = {f"{d.name}.json" for d in disks}
    for old in out_dir.glob("*.json"):
        if old.name not in configured:
            old.unlink()
            print(f"{old.stem}: ya no está en discos.conf, borrado")
    return errors


# --- Detección ---------------------------------------------------------------------

_ID_PREFERENCE = ("nvme-", "ata-", "usb-", "scsi-", "wwn-")


def preferred_ids(entries: dict[str, str]) -> dict[str, str]:
    """``{nombre en by-id: dispositivo}`` → ``{dispositivo: mejor nombre by-id}``.

    Prefiere nombres con modelo y número de serie (nvme-, ata-, usb-) a los
    identificadores opacos (eui., wwn-), y el más corto (sin el sufijo ``_1``).
    """
    best: dict[str, tuple[int, int, str]] = {}
    for name, device in entries.items():
        if "-part" in name or name.startswith(("dm-", "nvme-eui.", "md-")):
            continue
        rank = next((i for i, p in enumerate(_ID_PREFERENCE) if name.startswith(p)), 99)
        key = (rank, len(name), name)
        if device not in best or key < best[device]:
            best[device] = key
    return {dev: key[2] for dev, key in best.items()}


def _mounts(node: dict, crypt: bool = False) -> list[tuple[str, bool]]:
    found = [(m, crypt) for m in node.get("mountpoints") or [] if m and m != "[SWAP]"]
    for child in node.get("children") or []:
        found += _mounts(child, crypt or child.get("type") == "crypt")
    return found


def detect(run: Runner = run_smartctl) -> str:
    lsblk = subprocess.run(
        ["lsblk", "--json", "-o", "NAME,PATH,TYPE,TRAN,MODEL,MOUNTPOINTS"],
        capture_output=True,
        text=True,
        check=True,
    )
    ids = preferred_ids(
        {e.name: os.path.realpath(e.path) for e in os.scandir(BY_ID)}
        if os.path.isdir(BY_ID)
        else {}
    )
    lines = [
        "# Discos que vigila Cerbero (generado por cerbero_smart.py --detectar).",
        "# nombre  dispositivo  [montaje|-]  [opcional]  [tipo=sat]",
        "# «opcional»: si está desconectado no es una alerta.",
    ]
    for dev in json.loads(lsblk.stdout).get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        mounts = _mounts(dev)
        root = [m for m in mounts if m[0] == "/"]
        mount, crypt = (root or sorted(mounts, key=lambda m: len(m[0])) or [("-", False)])[0]
        name = "sistema" if mount == "/" else (Path(mount).name if mount != "-" else dev["name"])
        device = f"{BY_ID}/{ids[dev['path']]}" if dev["path"] in ids else dev["path"]
        code, data = run(device, None, False)
        dtype = None
        if "smart_status" not in data:
            code, data = run(device, "sat", False)
            dtype = "sat" if "smart_status" in data else None
        model = f"{dev.get('model') or ''} ({dev.get('tran') or '?'})".strip()
        error = None if "smart_status" in data else _error_text(code, data)
        lines.append(conf_line(name, device, mount, crypt, dtype, model, error))
    return "\n".join(lines) + "\n"


def conf_line(
    name: str,
    device: str,
    mount: str,
    optional: bool,
    dtype: str | None,
    model: str,
    error: str | None,
) -> str:
    """Línea de discos.conf. Un disco sin SMART queda comentado: algunos puentes
    USB se reinician (y desmontan el disco) si se les insiste con comandos que
    no entienden, así que el timer no debe tocarlo."""
    opts = " ".join(
        o for o in ("opcional" if optional else "", f"tipo={dtype}" if dtype else "") if o
    )
    line = f"{name:<9} {device}  {mount}  {opts}".rstrip()
    if error:
        return f"# {line}  # {model} — SIN SMART, desactivado: {error}"
    return f"{line}  # {model}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--conf", default=CONF)
    parser.add_argument("--dir", default=OUT_DIR)
    parser.add_argument("--detectar", action="store_true", help="propone un discos.conf")
    args = parser.parse_args(argv)
    if args.detectar:
        sys.stdout.write(detect())
        return 0
    disks = parse_conf(Path(args.conf).read_text(encoding="utf-8"))
    errors = dump_all(disks, Path(args.dir), datetime.now().astimezone())
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
