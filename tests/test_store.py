from __future__ import annotations

from datetime import timedelta

from app.store import AlertStore
from tests.conftest import FakeClock


def test_add_is_idempotent_per_key(clock: FakeClock) -> None:
    store = AlertStore()
    a, new = store.add("recursos:ram", "recursos", "critical", "RAM 1.0 GB", clock())
    assert new
    clock.advance(seconds=30)
    b, new = store.add("recursos:ram", "recursos", "critical", "RAM 0.8 GB", clock())
    assert not new
    assert b is a
    assert b.message == "RAM 0.8 GB"
    # El timestamp es el del primer disparo.
    assert b.timestamp == clock.now - timedelta(seconds=30)
    assert len(store.list()) == 1


def test_resolve_moves_to_history(clock: FakeClock) -> None:
    store = AlertStore()
    store.add("k", "recursos", "warning", "m", clock())
    clock.advance(minutes=5)
    resolved = store.resolve("k", clock())
    assert resolved is not None
    assert not resolved.active
    assert resolved.resolved_at == clock.now
    assert store.get("k") is None
    assert store.resolve("k", clock()) is None
    assert [a.key for a in store.list()] == ["k"]


def test_history_keeps_last_50_but_never_drops_active(clock: FakeClock) -> None:
    store = AlertStore()
    store.add("recursos:ram", "recursos", "critical", "RAM", clock())
    for i in range(80):
        clock.advance(seconds=1)
        store.add(f"accesos:x:{i}", "accesos", "warning", str(i), clock())
        store.resolve(f"accesos:x:{i}", clock())
    alerts = store.list()
    assert alerts[0].key == "recursos:ram"
    assert alerts[0].active
    resolved = [a for a in alerts if not a.active]
    assert len(resolved) == 50
    # Las resueltas, de la más reciente a la más antigua.
    assert resolved[0].message == "79"
    assert resolved[-1].message == "30"


def test_list_orders_active_newest_first(clock: FakeClock) -> None:
    store = AlertStore()
    store.add("a", "recursos", "warning", "a", clock())
    clock.advance(seconds=1)
    store.add("b", "recursos", "warning", "b", clock())
    assert [a.key for a in store.list()] == ["b", "a"]


def test_expired_keys(clock: FakeClock) -> None:
    store = AlertStore()
    store.add("ev", "accesos", "critical", "login", clock(), clock.now + timedelta(minutes=30))
    store.add("cond", "recursos", "critical", "ram", clock())
    assert store.expired_keys(clock.now) == []
    assert store.expired_keys(clock.now + timedelta(minutes=30)) == ["ev"]


def test_api_dump_hides_internal_fields(clock: FakeClock) -> None:
    store = AlertStore()
    alert, _ = store.add("k", "accesos", "critical", "m", clock())
    alert.notified = True
    data = alert.model_dump(mode="json")
    assert set(data) == {
        "id",
        "key",
        "level",
        "head",
        "message",
        "timestamp",
        "active",
        "resolved_at",
    }
    assert data["timestamp"] == "2026-09-30T03:14:22+02:00"
