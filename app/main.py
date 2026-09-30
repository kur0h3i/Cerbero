"""Arranque de Cerbero: la API y las tres cabezas.

Las cabezas 1 y 2 son bucles de asyncio con ``asyncio.sleep``; la 3 sigue el
log en su propio hilo. Todo arranca y se para con la aplicación (lifespan).

Uso: ``uvicorn --factory app.main:create_app``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI

from . import __doc__ as description
from .alerter import Alerter, Notifier, TelegramNotifier
from .api import router
from .config import Settings, load_settings
from .heads.accesos import AccesosHead
from .heads.contenedores import ContenedoresHead
from .heads.recursos import RecursosHead
from .store import AlertStore

log = logging.getLogger("cerbero")

# Cada cuánto se cierran los eventos caducados (logins externos).
HOUSEKEEPING_S = 30


@dataclass
class Cerbero:
    settings: Settings
    alerter: Alerter
    recursos: RecursosHead
    contenedores: ContenedoresHead
    accesos: AccesosHead

    @property
    def heads(self) -> list[RecursosHead | ContenedoresHead | AccesosHead]:
        return [self.recursos, self.contenedores, self.accesos]


HeadsFactory = Callable[[Settings, Alerter], tuple[RecursosHead, ContenedoresHead, AccesosHead]]


def default_heads(
    settings: Settings, alerter: Alerter
) -> tuple[RecursosHead, ContenedoresHead, AccesosHead]:
    return (
        RecursosHead(settings, alerter),
        ContenedoresHead(settings, alerter),
        AccesosHead(settings, alerter),
    )


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", force=True
    )
    # httpx registra cada petición con su URL, y la de Telegram lleva el token.
    for noisy in ("httpx", "httpcore", "urllib3", "docker"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def _housekeeping(alerter: Alerter) -> None:
    while True:
        await asyncio.sleep(HOUSEKEEPING_S)
        alerter.expire()


def create_app(
    settings: Settings | None = None,
    notifier: Notifier | None = None,
    heads: HeadsFactory = default_heads,
) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        telegram = None
        if notifier is None:
            telegram = TelegramNotifier(settings.telegram_token, settings.telegram_chat_id)
            if not telegram.enabled:
                log.warning("TELEGRAM_TOKEN/TELEGRAM_CHAT_ID sin definir: alertas solo en el log")
        alerter = Alerter(settings, AlertStore(), notifier or telegram)
        cerbero = Cerbero(settings, alerter, *heads(settings, alerter))
        app.state.cerbero = cerbero

        tasks = [asyncio.create_task(h.run(), name=f"cabeza-{h.name}") for h in cerbero.heads]
        tasks.append(asyncio.create_task(_housekeeping(alerter), name="limpieza"))
        if settings.notify_startup:
            tasks.append(
                asyncio.create_task(
                    alerter.announce("Cerbero en guardia: recursos, contenedores y accesos"),
                    name="aviso-arranque",
                )
            )
        log.info("Cerbero en guardia en %s", settings.server_name)
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if telegram is not None:
                await telegram.aclose()

    app = FastAPI(
        title="Cerbero",
        description=(description or "").strip(),
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.include_router(router)
    return app
