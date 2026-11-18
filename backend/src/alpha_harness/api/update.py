"""Checking for a new version and handing the install to the launcher."""

import asyncio
import signal
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Request

from .. import updates
from ..schemas import Out
from .deps import refuse

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/update", tags=["update"])

#: Only when no server handle was published — a reload-mode dev server. Long enough for the
#: response to have left, short enough that the user is not left waiting on it.
GOODBYE_SECONDS = 0.5


class UpdateStatus(Out):
    current: str
    #: False for a development checkout, where the version is a placeholder and every release
    #: reads as newer. Nothing is offered then.
    is_release: bool
    #: The newest published release, or null when GitHub could not be read.
    latest: str | None
    available: bool
    #: False for a development checkout or a manual install: there is no launcher to install
    #: it, so the UI offers the release page instead of an Update button.
    can_install: bool
    #: Already requested and waiting for the restart that applies it.
    pending: str | None
    notes: str
    url: str
    published_at: str | None
    #: Why the check could not be made, said plainly. Null when it worked.
    problem: str | None
    #: When GitHub was last asked. Null when it has not been yet, which Settings can cause.
    checked_at: str | None
    #: The version of the launcher that started this app, when one did.
    launcher: str | None
    #: The release download that replaces it on this machine, e.g. ``AlphaHarness.exe`` or
    #: ``AlphaHarness-macOS-arm64.zip``.
    launcher_file: str
    #: True when that exe is older than this release needs. An update installs the wheel and
    #: never the exe, so a launcher change reaches nobody until they download it themselves.
    launcher_outdated: bool
    #: Where to fetch a release by hand. Always set, even when GitHub could not be read.
    releases_url: str


class UpdateStarted(Out):
    version: str
    #: The app is closing itself; the launcher installs and reopens it.
    restarting: bool


@router.get("")
async def status(refresh: bool = False) -> UpdateStatus:
    """Whether a newer release is out. GitHub is asked only as often as Settings allow."""
    release, problem = await updates.latest(force=refresh)
    running = updates.current()
    # A failed install outranks a failed check: it is the answer to "I clicked Update and
    # nothing happened", which is the only question the user is actually asking.
    problem = updates.last_failure() or problem
    return UpdateStatus(
        current=running,
        is_release=updates.is_release(),
        latest=release.version if release else None,
        available=bool(
            release and updates.is_release() and updates.is_newer(release.version, running)
        ),
        can_install=updates.launcher_home() is not None,
        pending=updates.pending(),
        launcher=updates.launcher_version(),
        launcher_file=updates.LAUNCHER_FILE,
        launcher_outdated=updates.launcher_outdated(),
        releases_url=updates.RELEASES_PAGE,
        notes=release.notes if release else "",
        url=release.url if release else "",
        published_at=release.published_at if release else None,
        problem=problem,
        checked_at=updates.checked_at(),
    )


@router.post("")
async def apply(request: Request, background: BackgroundTasks) -> UpdateStarted:
    """Ask the launcher to install the newest release, then close the app so it can.

    The request file is written before anything else, so an app that then fails to close
    still gets updated the next time it is started — see :mod:`alpha_harness.updates`.
    """
    release, problem = await updates.latest()
    if release is None:
        raise refuse(503, "update_unavailable", problem or "No release could be read.")
    if not updates.is_newer(release.version, updates.current()):
        raise refuse(409, "already_current", f"Already running {updates.current()}.")
    try:
        updates.request(
            release.version, wheel_url=release.wheel_url, wheel_digest=release.wheel_digest
        )
    except RuntimeError as exc:
        raise refuse(409, "no_launcher", str(exc)) from exc

    background.add_task(stop_server, getattr(request.app.state, "server", None))
    return UpdateStarted(version=release.version, restarting=True)


async def stop_server(server: Any) -> None:
    """Close the app once this response has been flushed to the socket.

    A background task runs after the body is written, so there is no race with the browser
    and no arbitrary delay to tune. ``should_exit`` is uvicorn's own graceful path: it
    unwinds the lifespan, which is what closes DuckDB and SQLite.

    Without a server handle — ``uvicorn --reload`` in development — SIGINT reaches the same
    handler. Stopping the loop outright would skip the lifespan and leave both stores open,
    so that is deliberately not a fallback here.

    Shared with ``POST /api/quit``, which the launcher's notification-area Quit calls: an
    update and a quit differ only in whether anything starts afterwards.
    """
    if server is not None:
        server.should_exit = True
        return
    log.warning("update.no_server_handle")
    asyncio.get_running_loop().call_later(GOODBYE_SECONDS, signal.raise_signal, signal.SIGINT)
