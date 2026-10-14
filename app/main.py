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
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __doc__ as description
from .alerter import Alerter, Notifier, TelegramNotifier
from .api import router
from .config import Settings, load_settings
from .heads.accesos import AccesosHead
from .heads.contenedores import ContenedoresHead
from .heads.recursos import RecursosHead
from .heads.smart import SmartHead
from .heads.tareas import TareasHead
from .store import AlertStore

log = logging.getLogger("cerbero")

# Cada cuánto se cierran los eventos caducados (logins externos).
HOUSEKEEPING_S = 30

# Logos: con fondo (icono; Dis lo usa en la tarjeta del contenedor, porque cuando
# un servicio no está en su catálogo prueba el ``/favicon.svg`` de su web) y sin
# fondo (cabecera de la web).
ASSETS = Path(__file__).resolve().parent.parent / "assets"
FAVICON = ASSETS / "cerbero.svg"
LOGO = ASSETS / "cerbero-logo.svg"
# La web propia: HTML, CSS y JS sin compilar.
STATIC = Path(__file__).resolve().parent / "static"

# Solo para la web, no para /api/docs (Swagger carga su JS desde un CDN). Todo
# viene de Cerbero y nada se ejecuta en línea.
WEB_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # Que una actualización de Cerbero se vea al recargar (revalida con ETag).
    "Cache-Control": "no-cache",
}
WEB_PATHS = ("/", "/favicon.svg", "/logo.svg")


@dataclass
class Cerbero:
    settings: Settings
    alerter: Alerter
    recursos: RecursosHead
    contenedores: ContenedoresHead
    accesos: AccesosHead
    smart: SmartHead
    tareas: TareasHead

    @property
    def heads(self) -> list[RecursosHead | ContenedoresHead | AccesosHead | SmartHead | TareasHead]:
        return [self.recursos, self.contenedores, self.accesos, self.smart, self.tareas]


Heads = tuple[RecursosHead, ContenedoresHead, AccesosHead, SmartHead, TareasHead]
HeadsFactory = Callable[[Settings, Alerter], Heads]


def default_heads(settings: Settings, alerter: Alerter) -> Heads:
    return (
        RecursosHead(settings, alerter),
        ContenedoresHead(settings, alerter),
        AccesosHead(settings, alerter),
        SmartHead(settings, alerter),
        TareasHead(settings, alerter),
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
                    alerter.announce(
                        "Cerbero en guardia: recursos, contenedores, accesos, discos y tareas"
                    ),
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
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def web_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        path = request.url.path
        if path in WEB_PATHS or path.startswith("/static/"):
            response.headers.update(WEB_HEADERS)
        return response

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        # La web de detalle de Cerbero; el botón «Abrir» de Dis lleva aquí.
        return FileResponse(STATIC / "index.html", media_type="text/html")

    def svg(path: Path) -> Response:
        if not path.is_file():
            return Response(status_code=404)
        return FileResponse(path, media_type="image/svg+xml")

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon() -> Response:
        return svg(FAVICON)

    @app.get("/logo.svg", include_in_schema=False)
    def logo() -> Response:
        return svg(LOGO)

    return app
