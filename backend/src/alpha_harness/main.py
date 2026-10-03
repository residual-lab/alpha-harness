"""FastAPI application.

Run with::

    uvx alpha-harness                                          # built UI included
    uv run uvicorn alpha_harness.main:app --reload --port 8000  # development, UI from Vite

The frontend talks to this over HTTP + WebSocket only.
Credentials never leave the backend.
"""

import asyncio
import contextlib
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

import structlog
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import updates
from .api import (
    alphas,
    auth,
    catalog,
    chat,
    competitions,
    ga,
    lab_tasks,
    llm,
    portfolio,
    power_pool_lab,
    preferences,
    prompts,
    quarter,
    region_agnostic_lab,
    search_lab,
    sims,
    super_lab,
    tasks,
    template_basic_lab,
    template_lab,
    today,
    tools,
    update,
    vault,
    ws,
)
from .api.auth import Session
from .api.deps import install_exception_handlers
from .api.update import stop_server
from .schemas import Out
from .state import AppState

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from starlette.responses import Response
    from starlette.types import Scope

log = structlog.get_logger(__name__)

DESCRIPTION = """
Local research studio for the WorldQuant BRAIN platform.

Runs entirely on your machine. BRAIN credentials are encrypted at rest and never
reach the browser.
"""


def configure_logging() -> None:
    """Console-rendered structured logs, for a local tool a human is watching."""
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)

    # uvicorn's access log duplicates what we already record and is noisy in a terminal
    # that is also showing simulation telemetry.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
            structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def create_app() -> FastAPI:
    configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        try:
            state = AppState()
            app.state.harness = state
            await state.startup()
        except Exception as exc:
            # uvicorn reports this and then exits through a traceback of its own, whose last
            # lines are all the launcher shows; ``__main__`` says this instead.
            app.state.startup_failure = exc
            raise

        async def install(release: updates.Release) -> None:
            updates.request(release.version, wheel_url=release.wheel_url)
            await stop_server(getattr(app.state, "server", None))

        watcher = asyncio.create_task(updates.watch(state.engine.idle, install), name="updates")
        try:
            yield
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            await state.shutdown()

    app = FastAPI(
        title="Alpha Harness",
        description=DESCRIPTION,
        # One source of truth: the installed distribution's own metadata, which the release
        # tag sets. A hardcoded string here drifts from what the updater compares against.
        version=updates.current(),
        lifespan=lifespan,
    )

    # Set by the first request a page of ours makes, so a start straight after an update can
    # tell whether the page that asked for it came back (see ``__main__``).
    page_seen = asyncio.Event()
    app.state.page_seen = page_seen

    # DNS rebinding: a page whose hostname resolves to 127.0.0.1 is "same-origin" to the
    # browser, so the header checks below pass. Its Host header still names that hostname.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])

    @app.middleware("http")
    async def require_client_header(request: Request, call_next: Any) -> Any:
        """Refuse requests another page started, and writes that lack the UI's custom header.

        CORS only hides the response of a "simple" cross-site request; it does not stop it
        running, and some GETs spend BRAIN quota, so Sec-Fetch-Site guards every method
        (same-origin from the Vite proxy, none from a typed URL). The custom header covers
        writes from clients that send no Sec-Fetch-Site.
        """
        if request.headers.get("sec-fetch-site", "none") not in {"same-origin", "none"}:
            return JSONResponse(
                {"detail": {"code": "forbidden", "message": "Requests must come from the app."}},
                status_code=403,
            )
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and (
            request.headers.get("x-harness-client") != "1"
        ):
            return JSONResponse(
                {"detail": {"code": "forbidden", "message": "Writes must come from the app."}},
                status_code=403,
            )
        page_seen.set()
        return await call_next(request)

    install_exception_handlers(app)

    app.include_router(auth.router)
    app.include_router(catalog.router)
    app.include_router(sims.router)
    app.include_router(alphas.router)
    app.include_router(template_lab.router)
    app.include_router(template_basic_lab.router)
    app.include_router(ga.router)
    app.include_router(llm.router)
    app.include_router(prompts.router)
    app.include_router(vault.router)
    app.include_router(portfolio.router)
    app.include_router(quarter.router)
    app.include_router(tasks.router)
    app.include_router(today.router)
    app.include_router(tools.router)
    app.include_router(search_lab.router)
    app.include_router(lab_tasks.router)
    app.include_router(power_pool_lab.router)
    app.include_router(region_agnostic_lab.router)
    app.include_router(super_lab.router)
    app.include_router(chat.router)
    app.include_router(competitions.router)
    app.include_router(update.router)
    app.include_router(preferences.router)
    app.include_router(ws.router)

    @app.get("/api/health", tags=["meta"])
    async def health() -> Health:
        """Liveness plus a summary of local state, for the UI's status bar."""
        state: AppState = app.state.harness
        return Health(
            ok=True,
            version=app.version,
            data_dir=str(state.settings.data_dir),
            database=await state.db.healthcheck(),
            session=Session.model_validate(state.auth.session.to_dict()),
            active_simulations=len(await state.tracker.active()),
            websocket_clients=state.hub.client_count,
        )

    @app.post("/api/quit", tags=["meta"], status_code=202)
    async def quit_app(background: BackgroundTasks) -> Quitting:
        """Close the app for good. The launcher's notification-area Quit calls this.

        Not a courtesy: killing the process outright leaves DuckDB's single-writer lock held
        and the next start finds its own catalog busy. This unwinds uvicorn's lifespan, which
        closes both stores, and the launcher exits when the process does.
        """
        background.add_task(stop_server, getattr(app.state, "server", None))
        return Quitting(stopping=True)

    # Last, so every API and socket route matches first.
    app.mount("/", SinglePageApp(directory=WEB, html=True, check_dir=False), name="web")
    return app


class Quitting(Out):
    stopping: bool


class Health(Out):
    ok: bool
    version: str
    data_dir: str
    database: bool
    session: Session
    active_simulations: int
    websocket_clients: int


#: The built frontend (``lefthook run build``); absent in development, where Vite serves it.
WEB = Path(__file__).parent / "web"


class SinglePageApp(StaticFiles):
    """Static files, with the UI's own routes (``/matrix``, ...) answered by ``index.html``."""

    @override
    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # A missing file (``.js``) or API path stays a 404; only UI routes fall back.
            if exc.status_code != 404 or "." in path.rsplit("/", 1)[-1] or path.startswith("api"):
                raise
            response = await super().get_response("index.html", scope)
        # The page is asked for again every time; the scripts it names carry a hash of their
        # contents, so they never need to be. Without a rule a browser kept the page for a
        # tenth of its file's age, and after an update showed the old app for hours.
        if path.startswith("assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


app = create_app()
