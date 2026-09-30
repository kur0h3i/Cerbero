from __future__ import annotations

import asyncio
import os
from ipaddress import ip_address
from pathlib import Path

import pytest

from app.alerter import Alerter
from app.config import Settings
from app.heads.accesos import (
    AccesosHead,
    AccessAnalyzer,
    Fire,
    LogTailer,
    Resolve,
    SshEvent,
    parse_line,
)
from app.store import AlertStore
from tests.conftest import FakeClock, FakeNotifier

NEW = "2026-09-30T03:14:22.123456+02:00 server-kuro sshd-session[4242]: "
OLD = "Sep 30 03:14:22 server-kuro sshd[4242]: "


# --- Parser ------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", [NEW, OLD, "Sep  3 03:14:22 server-kuro sshd[1]: "])
def test_parse_failed_password_both_formats(prefix: str) -> None:
    e = parse_line(prefix + "Failed password for root from 203.0.113.5 port 51234 ssh2\n")
    assert e == SshEvent("failed", ip_address("203.0.113.5"), 51234, "root", True, "password")


@pytest.mark.parametrize(
    ("msg", "expected"),
    [
        (
            "Failed password for invalid user admin from 203.0.113.5 port 1 ssh2",
            ("failed", "203.0.113.5", "admin", False, "password"),
        ),
        (
            "Failed publickey for root from 203.0.113.5 port 1 ssh2: RSA SHA256:abc",
            ("failed", "203.0.113.5", "root", True, "publickey"),
        ),
        (
            "Accepted publickey for kuro from 192.168.1.20 port 5 ssh2: ED25519 SHA256:x",
            ("accepted", "192.168.1.20", "kuro", True, "publickey"),
        ),
        (
            "Accepted password for kuro from 2001:db8::5 port 22 ssh2",
            ("accepted", "2001:db8::5", "kuro", True, "password"),
        ),
        (
            "Accepted publickey for kuro from ::ffff:203.0.113.7 port 22 ssh2",
            ("accepted", "203.0.113.7", "kuro", True, "publickey"),
        ),
        (
            "Connection closed by authenticating user root 203.0.113.9 port 4 [preauth]",
            ("closed", "203.0.113.9", "root", True, ""),
        ),
        (
            "Disconnected from invalid user test 203.0.113.9 port 4 [preauth]",
            ("closed", "203.0.113.9", "test", False, ""),
        ),
        (
            "Disconnecting authenticating user root 203.0.113.5 port 5: "
            "Too many authentication failures [preauth]",
            ("closed", "203.0.113.5", "root", True, ""),
        ),
        (
            "Failed password for invalid user  from 203.0.113.6 port 5 ssh2",
            ("failed", "203.0.113.6", "", False, "password"),
        ),
    ],
)
def test_parse_messages(msg: str, expected: tuple) -> None:
    e = parse_line(NEW + msg)
    assert e is not None
    assert (e.kind, str(e.ip), e.user, e.valid_user, e.method) == expected


@pytest.mark.parametrize(
    "line",
    [
        NEW + "Invalid user test from 203.0.113.9 port 40000",
        NEW + "Received disconnect from 203.0.113.9 port 4:11: Bye Bye [preauth]",
        NEW + "pam_unix(sshd:auth): authentication failure; rhost=203.0.113.5 user=root",
        "2026-09-30T03:14:22+02:00 server-kuro sudo: kuro : TTY=pts/0 ; COMMAND=/bin/ls",
        "2026-09-30T03:14:22+02:00 server-kuro CRON[1]: pam_unix(cron:session): session opened",
        # Otro programa que escribe texto que parece de sshd no cuela.
        "2026-09-30T03:14:22+02:00 server-kuro app[9]: sshd[1]: Accepted password for "
        "kuro from 8.8.8.8 port 1 ssh2",
        NEW + "Accepted password for kuro from no-es-una-ip port 1 ssh2",
        "",
    ],
)
def test_parse_ignores(line: str) -> None:
    assert parse_line(line) is None


def test_parse_username_cannot_spoof_the_ip() -> None:
    # El usuario lo elige el atacante; la IP buena es la última que escribe sshd.
    e = parse_line(
        NEW + "Failed password for invalid user x from 192.168.1.5 port 1 ssh2 "
        "from 203.0.113.66 port 5555 ssh2"
    )
    assert e is not None
    assert str(e.ip) == "203.0.113.66"
    assert e.port == 5555
    assert not e.valid_user
    e = parse_line(
        NEW + "Connection closed by invalid user a 192.168.1.5 port 1 203.0.113.66 port 7 [preauth]"
    )
    assert e is not None and str(e.ip) == "203.0.113.66"


# --- Analizador --------------------------------------------------------------------


def failed(ip: str, user: str = "root", port: int = 1000, valid: bool = True) -> SshEvent:
    return SshEvent("failed", ip_address(ip), port, user, valid, "password")


def closed(ip: str, user: str = "root", port: int = 1000, valid: bool = True) -> SshEvent:
    return SshEvent("closed", ip_address(ip), port, user, valid)


def fires(actions) -> list[Fire]:
    return [a for a in actions if isinstance(a, Fire)]


def keys(actions) -> list[str]:
    return [a.key for a in actions]


def test_brute_force_needs_more_than_threshold(settings: Settings) -> None:
    an = AccessAnalyzer(settings)
    actions = []
    for i in range(10):
        actions += an.feed(failed("203.0.113.5", "admin", port=i, valid=False), now=float(i))
    assert actions == []
    actions = an.feed(failed("203.0.113.5", "root", port=11, valid=False), now=11.0)
    (fire,) = actions
    assert fire.key == "accesos:fuerza_bruta:203.0.113.5"
    assert fire.level == "critical"
    assert fire.message == (
        "Fuerza bruta SSH desde 203.0.113.5 (externa): 11 fallos en 5 min (usuarios: admin, root)"
    )


def test_brute_force_window(settings: Settings) -> None:
    an = AccessAnalyzer(settings)
    actions = []
    for i in range(30):  # un fallo cada 40 s: 7-8 por ventana de 5 min
        actions += an.feed(failed("203.0.113.5", port=i, valid=False), now=i * 40.0)
    assert fires(actions) == []


def test_brute_force_from_trusted_ip_is_warning(settings: Settings) -> None:
    an = AccessAnalyzer(settings)
    actions = []
    for i in range(11):
        actions += an.feed(failed("192.168.1.33", port=i), now=float(i))
    (fire,) = fires(actions)
    assert fire.level == "warning"
    assert "(de confianza)" in fire.message


def test_brute_force_is_throttled_and_resolves(settings: Settings) -> None:
    an = AccessAnalyzer(settings)
    actions = []
    for i in range(100):  # 100 fallos en 10 s
        actions += an.feed(failed("203.0.113.5", port=i, valid=False), now=i / 10)
    assert len(fires(actions)) == 1  # se actualiza como mucho cada 30 s
    actions = an.feed(failed("203.0.113.5", port=500, valid=False), now=40.0)
    assert "101 fallos" in fires(actions)[0].message
    assert an.tick(now=200.0) == []  # aún dentro de la ventana
    (res,) = an.tick(now=40.0 + 5 * 60 + 1)
    assert res == Resolve(
        "accesos:fuerza_bruta:203.0.113.5",
        "Cesa la fuerza bruta SSH desde 203.0.113.5 (101 fallos en total)",
    )
    assert an.tracked_ips == 0


def test_tick_delivers_the_throttled_update(settings: Settings) -> None:
    an = AccessAnalyzer(settings)
    for i in range(40):  # una ráfaga de 4 s: solo se envía el cruce del umbral
        an.feed(failed("203.0.113.5", port=i, valid=False), now=i / 10)
    (fire,) = fires(an.tick(now=10.0))
    assert "40 fallos" in fire.message
    assert an.tick(now=20.0) == []  # nada nuevo que entregar


def test_closed_connection_counts_only_without_failed_lines(settings: Settings) -> None:
    an = AccessAnalyzer(settings)
    # Con clave pública no hay líneas "Failed": solo el cierre de cada conexión.
    for i in range(11):
        an.feed(closed("203.0.113.8", port=i, valid=False), now=float(i))
    assert an.tick(now=12.0) == []
    assert an._failures[ip_address("203.0.113.8")].total == 11
    # Con contraseña: 3 "Failed" + cierre de la misma conexión = 3 fallos, no 4.
    an2 = AccessAnalyzer(settings)
    for _ in range(3):
        an2.feed(failed("203.0.113.9", port=77, valid=False), now=1.0)
    an2.feed(closed("203.0.113.9", port=77, valid=False), now=2.0)
    assert an2._failures[ip_address("203.0.113.9")].total == 3


def test_failed_none_is_not_an_attempt(settings: Settings) -> None:
    an = AccessAnalyzer(settings)
    probe = SshEvent("failed", ip_address("203.0.113.5"), 1, "root", True, "none")
    assert an.feed(probe, now=0.0) == []
    assert an.tracked_ips == 0


def test_external_login_is_critical_event(settings: Settings) -> None:
    an = AccessAnalyzer(settings)
    ok = SshEvent("accepted", ip_address("198.51.100.4"), 22, "kuro", True, "publickey")
    (fire,) = an.feed(ok, now=0.0)
    assert fire.key == "accesos:login_externo:kuro@198.51.100.4"
    assert fire.level == "critical"
    assert fire.ttl is not None and fire.ttl.total_seconds() == 30 * 60
    assert fire.message == (
        "Login SSH aceptado desde IP externa 198.51.100.4 (usuario kuro, publickey)"
    )


@pytest.mark.parametrize("ip", ["192.168.1.20", "10.0.0.2", "100.87.200.61", "127.0.0.1"])
def test_trusted_login_is_silent(settings: Settings, ip: str) -> None:
    an = AccessAnalyzer(settings)
    assert an.feed(SshEvent("accepted", ip_address(ip), 22, "kuro", True, "publickey"), 0) == []


def test_external_attempts_with_valid_user_are_aggregated(settings: Settings) -> None:
    an = AccessAnalyzer(settings)
    actions = []
    for i in range(50):  # una botnet: 50 IPs, un intento cada una
        actions += an.feed(failed(f"198.51.100.{i}", "root", port=i), now=float(i))
    external = [f for f in fires(actions) if f.key == "accesos:intento_externo"]
    assert len(external) == 2  # la primera y una actualización a los 30 s
    assert external[0].level == "warning"
    assert "1 en 5 min desde 1 IP(s) (último: root desde 198.51.100.0)" in external[0].message
    assert "31 en 5 min desde 31 IP(s)" in external[1].message
    # Ninguna IP pasa el umbral de fuerza bruta.
    assert not any(k.startswith("accesos:fuerza_bruta") for k in keys(actions))
    (res,) = an.tick(now=49.0 + 5 * 60 + 1)
    assert res.key == "accesos:intento_externo"


def test_invalid_users_from_outside_are_not_external_attempts(settings: Settings) -> None:
    an = AccessAnalyzer(settings)
    actions = an.feed(failed("198.51.100.1", "oracle", valid=False), now=0.0)
    assert actions == []


def test_memory_is_bounded(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.heads.accesos.MAX_TRACKED_IPS", 100)
    monkeypatch.setattr("app.heads.accesos.MAX_OPEN_CONNS", 50)
    an = AccessAnalyzer(settings)
    for i in range(1000):
        an.feed(failed(f"10.9.{i // 256}.{i % 256}", valid=False, port=i), now=float(i) / 100)
    assert an.tracked_ips == 100
    assert len(an._counted_conns) == 50
    for i in range(3000):
        an.feed(failed(f"198.51.{i // 250}.{i % 250}", "root", port=i), now=10 + i / 100)
    assert len(an._external) == 1000


def test_eviction_keeps_the_active_attacker(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.heads.accesos.MAX_TRACKED_IPS", 10)
    an = AccessAnalyzer(settings)
    actions = []
    for i in range(200):
        # El atacante falla sin parar mientras un escaneo pasa por cientos de IPs.
        actions += an.feed(failed("203.0.113.5", valid=False, port=i), now=i / 10)
        an.feed(failed(f"198.51.100.{i % 250}", valid=False, port=i), now=i / 10)
    assert "accesos:fuerza_bruta:203.0.113.5" in keys(actions)


def test_count_saturates_but_keeps_alerting(settings: Settings) -> None:
    an = AccessAnalyzer(settings)
    for i in range(500):
        an.feed(failed("203.0.113.5", valid=False, port=i), now=i / 100)
    (fire,) = fires(an.feed(failed("203.0.113.5", valid=False, port=999), now=60.0))
    assert "200+ fallos en 5 min" in fire.message


# --- Tail ---------------------------------------------------------------------------


def write(path: Path, text: str, mode: str = "a") -> None:
    with path.open(mode) as fh:
        fh.write(text)


def test_tailer_starts_at_end_and_follows(tmp_path: Path) -> None:
    log = tmp_path / "auth.log"
    write(log, "viejo 1\nviejo 2\n", "w")
    t = LogTailer(str(log))
    assert t.read() == []
    write(log, "nuevo 1\nnue")
    assert t.read() == ["nuevo 1"]
    assert t.read() == []
    write(log, "vo 2\n")
    assert t.read() == ["nuevo 2"]
    t.close()


def test_tailer_handles_rotation(tmp_path: Path) -> None:
    log = tmp_path / "auth.log"
    write(log, "", "w")
    t = LogTailer(str(log))
    t.read()
    write(log, "antes de rotar\n")
    os.rename(log, tmp_path / "auth.log.1")  # logrotate: renombra y crea uno nuevo
    write(tmp_path / "auth.log.1", "rezagada en el viejo\n")
    assert t.read() == ["antes de rotar", "rezagada en el viejo"]
    assert t.read() == []  # aún no existe el nuevo: seguimos con el viejo
    write(log, "primera del nuevo\n", "w")
    assert t.read() == ["primera del nuevo"]
    write(log, "segunda\n")
    assert t.read() == ["segunda"]
    t.close()


def test_tailer_handles_copytruncate(tmp_path: Path) -> None:
    log = tmp_path / "auth.log"
    write(log, "x" * 100 + "\n", "w")
    t = LogTailer(str(log))
    t.read()
    write(log, "", "w")  # truncado en el sitio
    write(log, "tras truncar\n")
    assert t.read() == ["tras truncar"]
    t.close()


def test_tailer_waits_for_missing_file_and_reads_it_whole(tmp_path: Path) -> None:
    log = tmp_path / "auth.log"
    t = LogTailer(str(log))
    assert t.read() == []
    assert not t.is_open
    assert t.error == "No such file or directory"
    write(log, "linea 1\nlinea 2\n", "w")
    assert t.read() == ["linea 1", "linea 2"]
    assert t.is_open and t.error is None
    t.close()


def test_tailer_survives_invalid_utf8(tmp_path: Path) -> None:
    log = tmp_path / "auth.log"
    log.write_bytes(b"")
    t = LogTailer(str(log))
    t.read()
    with log.open("ab") as fh:
        fh.write(b"usuario \xff\xfe raro\n")
    (line,) = t.read()
    assert "raro" in line
    t.close()


# --- Cabeza completa (hilo + bucle de asyncio) ---------------------------------------


@pytest.mark.anyio
async def test_head_end_to_end(
    tmp_path: Path, settings: Settings, notifier: FakeNotifier, clock: FakeClock
) -> None:
    log = tmp_path / "auth.log"
    write(log, NEW + "Failed password for root from 203.0.113.5 port 1 ssh2\n" * 50, "w")
    alerter = Alerter(settings, AlertStore(), notifier, clock=clock)
    head = AccesosHead(settings, alerter, tailer=LogTailer(str(log)))
    head.POLL_S = 0.02
    task = asyncio.create_task(head.run())
    try:
        await _until(head.healthy)
        lines = [
            NEW + f"Failed password for invalid user u{i} from 203.0.113.5 port {i} ssh2\n"
            for i in range(11)
        ]
        lines.append(NEW + "Accepted publickey for kuro from 198.51.100.4 port 9 ssh2\n")
        lines.append(NEW + "Accepted publickey for kuro from 192.168.1.20 port 9 ssh2\n")
        write(log, "".join(lines))
        await _until(lambda: head.lines_seen == 13 and len(notifier.sent) >= 2)
        active = {a.key: a for a in alerter.store.list()}
        # El histórico previo (50 fallos) no cuenta: se empieza por el final.
        assert "11 fallos" in active["accesos:fuerza_bruta:203.0.113.5"].message
        assert "accesos:login_externo:kuro@198.51.100.4" in active
        assert len(active) == 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not head.healthy()


@pytest.mark.anyio
async def test_head_warns_when_log_is_missing(
    tmp_path: Path, settings: Settings, notifier: FakeNotifier, clock: FakeClock
) -> None:
    log = tmp_path / "auth.log"
    alerter = Alerter(settings, AlertStore(), notifier, clock=clock)
    head = AccesosHead(settings, alerter, tailer=LogTailer(str(log)))
    head.POLL_S = 0.02
    task = asyncio.create_task(head.run())
    try:
        await _until(lambda: alerter.is_active("accesos:log"))
        assert not head.healthy()
        assert "No such file or directory (¿está instalado rsyslog?)" in notifier.sent[0]
        write(log, "", "w")
        await _until(lambda: not alerter.is_active("accesos:log"))
        assert head.healthy()
        assert "🟢 [RESUELTO]" in notifier.sent[-1]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def _until(condition, timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("la condición no se cumplió a tiempo")
