from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings, load_settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeClock:
    """Reloj manual para probar ventanas y cooldowns sin esperar."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 30, 3, 14, 22, tzinfo=ZoneInfo("Europe/Madrid"))

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class FakeNotifier:
    """Guarda los mensajes en vez de enviarlos; ``ok=False`` simula un fallo."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        if self.ok:
            self.sent.append(text)
        return self.ok


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def settings() -> Settings:
    return load_settings({})
