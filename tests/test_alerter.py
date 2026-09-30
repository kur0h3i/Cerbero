from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from app.alerter import Alerter, TelegramNotifier, format_message
from app.config import Settings
from app.store import AlertStore
from tests.conftest import FakeClock, FakeNotifier

pytestmark = pytest.mark.anyio


def make(settings: Settings, notifier: FakeNotifier, clock: FakeClock) -> Alerter:
    return Alerter(settings, AlertStore(), notifier, clock=clock)


def test_format_matches_the_agreed_layout(clock: FakeClock) -> None:
    text = format_message(
        "🔴 [CRÍTICO]", "recursos", "RAM libre 0.8 GB (umbral 1.5 GB)", "server-kuro", clock()
    )
    assert text == (
        "<b>🔴 [CRÍTICO] Cabeza: Recursos</b>\n"
        "Mensaje: RAM libre 0.8 GB (umbral 1.5 GB)\n"
        "Servidor: server-kuro · 2026-09-30 03:14:22"
    )


def test_format_escapes_html(clock: FakeClock) -> None:
    text = format_message("🟡 [AVISO]", "accesos", "usuario <script>", "srv", clock())
    assert "&lt;script&gt;" in text


async def test_fire_sends_once_then_respects_cooldown(
    settings: Settings, notifier: FakeNotifier, clock: FakeClock
) -> None:
    alerter = make(settings, notifier, clock)
    await alerter.fire("recursos:ram", "recursos", "critical", "RAM 0.8 GB")
    assert len(notifier.sent) == 1
    assert "🔴 [CRÍTICO] Cabeza: Recursos" in notifier.sent[0]
    for _ in range(10):
        clock.advance(minutes=2)
        await alerter.fire("recursos:ram", "recursos", "critical", "RAM 0.8 GB")
    assert len(notifier.sent) == 1


async def test_reminder_after_cooldown_if_problem_persists(
    settings: Settings, notifier: FakeNotifier, clock: FakeClock
) -> None:
    alerter = make(settings, notifier, clock)
    await alerter.fire("recursos:ram", "recursos", "critical", "RAM 0.8 GB")
    clock.advance(minutes=30)
    await alerter.fire("recursos:ram", "recursos", "critical", "RAM 0.7 GB")
    assert len(notifier.sent) == 2
    assert "Activa desde: 2026-09-30 03:14:22" in notifier.sent[1]
    assert "RAM 0.7 GB" in notifier.sent[1]


async def test_escalation_skips_cooldown(
    settings: Settings, notifier: FakeNotifier, clock: FakeClock
) -> None:
    alerter = make(settings, notifier, clock)
    await alerter.fire("recursos:disco:/", "recursos", "warning", "88 %")
    clock.advance(minutes=1)
    await alerter.fire("recursos:disco:/", "recursos", "critical", "96 %")
    assert len(notifier.sent) == 2
    assert "🔴 [CRÍTICO]" in notifier.sent[1]
    # Bajar de nivel no es motivo para reenviar.
    clock.advance(minutes=1)
    await alerter.fire("recursos:disco:/", "recursos", "warning", "90 %")
    assert len(notifier.sent) == 2


async def test_resolution_message_only_if_notified(
    settings: Settings, notifier: FakeNotifier, clock: FakeClock
) -> None:
    alerter = make(settings, notifier, clock)
    await alerter.fire("recursos:ram", "recursos", "critical", "RAM 0.8 GB")
    clock.advance(minutes=30)
    alert = await alerter.resolve("recursos:ram", "RAM libre recuperada (1.9 GB)")
    assert alert is not None and not alert.active
    assert notifier.sent[-1] == (
        "<b>🟢 [RESUELTO] Cabeza: Recursos</b>\n"
        "Mensaje: RAM libre recuperada (1.9 GB)\n"
        "Servidor: server-kuro · 2026-09-30 03:44:22"
    )
    # Resolver algo que no está activo no hace nada.
    assert await alerter.resolve("recursos:ram", "x") is None
    assert len(notifier.sent) == 2


async def test_flapping_condition_does_not_spam(
    settings: Settings, notifier: FakeNotifier, clock: FakeClock
) -> None:
    alerter = make(settings, notifier, clock)
    for _ in range(5):
        await alerter.fire("recursos:ram", "recursos", "critical", "RAM baja")
        clock.advance(minutes=1)
        await alerter.resolve("recursos:ram", "RAM recuperada")
        clock.advance(minutes=1)
    # Primera alerta y su resolución; las siguientes caen dentro del cooldown y,
    # como no se notificaron, tampoco se notifica su resolución.
    assert len(notifier.sent) == 2
    # Pero todas quedan registradas en el almacén (para Dis).
    assert len(alerter.store.list()) == 5


async def test_failed_send_is_retried_next_time(settings: Settings, clock: FakeClock) -> None:
    notifier = FakeNotifier(ok=False)
    alerter = make(settings, notifier, clock)
    alert = await alerter.fire("recursos:ram", "recursos", "critical", "RAM")
    assert not alert.notified
    notifier.ok = True
    clock.advance(seconds=30)
    await alerter.fire("recursos:ram", "recursos", "critical", "RAM")
    assert alert.notified
    assert len(notifier.sent) == 1


async def test_ttl_events_expire_silently(
    settings: Settings, notifier: FakeNotifier, clock: FakeClock
) -> None:
    alerter = make(settings, notifier, clock)
    await alerter.fire(
        "accesos:login:kuro@8.8.8.8", "accesos", "critical", "login", ttl=timedelta(minutes=30)
    )
    clock.advance(minutes=29)
    alerter.expire()
    assert alerter.is_active("accesos:login:kuro@8.8.8.8")
    clock.advance(minutes=1)
    alerter.expire()
    assert not alerter.is_active("accesos:login:kuro@8.8.8.8")
    assert len(notifier.sent) == 1  # sin 🟢 [RESUELTO]


async def test_announce(settings: Settings, notifier: FakeNotifier, clock: FakeClock) -> None:
    alerter = make(settings, notifier, clock)
    await alerter.announce("Cerbero en guardia")
    assert notifier.sent == [
        "<b>🟢 [INFO]</b>\nMensaje: Cerbero en guardia\nServidor: server-kuro · 2026-09-30 03:14:22"
    ]


# --- TelegramNotifier ----------------------------------------------------------


def telegram(handler) -> TelegramNotifier:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelegramNotifier("123:SECRETO", "42", client=client)


async def test_telegram_posts_to_bot_api() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    assert await telegram(handler).send("hola")
    assert seen[0].url.path == "/bot123:SECRETO/sendMessage"
    body = seen[0].read().decode()
    assert '"chat_id":"42"' in body
    assert '"parse_mode":"HTML"' in body


async def test_telegram_disabled_without_credentials() -> None:
    notifier = TelegramNotifier(None, None)
    assert not notifier.enabled
    assert not await notifier.send("hola")
    await notifier.aclose()


async def test_telegram_retries_once_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0}})
        return httpx.Response(200, json={"ok": True})

    assert await telegram(handler).send("hola")
    assert calls == 2


async def test_telegram_error_never_logs_the_token(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"fallo conectando a {request.url}")

    assert not await telegram(handler).send("hola")
    assert "SECRETO" not in caplog.text
    assert "***" in caplog.text


async def test_telegram_rate_limit() -> None:
    notifier = telegram(lambda request: httpx.Response(200, json={"ok": True}))
    results = [await notifier.send(str(i)) for i in range(TelegramNotifier.MAX_PER_MINUTE + 5)]
    assert results.count(True) == TelegramNotifier.MAX_PER_MINUTE
