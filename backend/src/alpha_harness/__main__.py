"""``alpha-harness``: the backend and its built UI in one process, opened in the browser."""

import asyncio
import contextlib
import errno
import faulthandler
import os
import socket
import sys
from typing import Any

import uvicorn

from . import diagnostics, handover, updates
from .window import open_window

HOST = "127.0.0.1"
PORT = 8000
#: Tried when PORT is taken. A fixed second choice keeps the page's origin, and with it the
#: drafts and layouts the browser keeps per origin, the same from one start to the next.
FALLBACK_PORT = 18000
#: A port another program holds: POSIX's number, and Windows' own (WSAEADDRINUSE). Access
#: denied is Windows' answer for a port it reserved, for Hyper-V or WSL say (WSAEACCES).
PORT_TAKEN = frozenset({errno.EADDRINUSE, errno.EACCES, 10048, 10013})
#: How long a start straight after an update waits for the page that asked for it to return.
#: It asks every 1.5 s (RECONNECT_MS in the UI's update.tsx).
PAGE_RETURN_SECONDS = 10.0


async def _serve() -> str | None:
    """Serve until the app is closed. Returns why it could not start, if it could not."""
    listening = _listen()
    port: int = listening.getsockname()[1]
    from .main import app

    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=port))
    # The one handle that can stop this process gracefully. Reached by the update route, which
    # has to close the app so the launcher can replace it while nothing holds the files open.
    app.state.server = server

    async def serve() -> None:
        # uvicorn ends a failed startup with ``sys.exit`` inside this task, which reaches the
        # log as a traceback of uvicorn's own frames; the reason is returned below instead.
        try:
            await server.serve(sockets=[listening])
        except SystemExit:
            if getattr(app.state, "startup_failure", None) is None:
                raise

    serving = asyncio.create_task(serve())
    # Kept referenced for the life of the server, so it is never collected mid-watch.
    watching = asyncio.create_task(_orphaned(server)) if _launched() else None
    # Startup reconciles in-flight simulations first; open the page once it can answer.
    # uvicorn exposes readiness only as the ``started`` flag, never an event.
    while not server.started and not serving.done():  # noqa: ASYNC110
        await asyncio.sleep(0.1)
    if server.started:
        updates.served(port)
        # Taken on every start, so a desktop window's update never leaves it for a later one.
        reloads = updates.page_reloads()
        # Straight after an update the page that asked for it reloads onto this one by itself.
        if not os.environ.get(updates.SHELL_VARIABLE) and not (
            reloads and await _page_returns(app)
        ):
            open_window(f"http://{HOST}:{port}")
    await serving
    if watching is not None:
        watching.cancel()
    failure = getattr(app.state, "startup_failure", None)
    if failure is None:
        return None
    return f"Alpha Harness could not start: {str(failure) or type(failure).__name__}"


def _listen() -> socket.socket:
    """The app's socket, bound before anything else starts.

    On :data:`PORT`, else :data:`FALLBACK_PORT`, else any port the system has free: the
    launcher learns which from ``served.json``. Bound here rather than by uvicorn, which
    reports a port in use as a traceback, and only after the catalog has been opened.
    """
    for port in (PORT, FALLBACK_PORT):
        try:
            return _bind(port)
        except OSError as exc:
            if exc.errno not in PORT_TAKEN:
                raise
            # In the launcher's log: why the app is not where it usually is.
            sys.stderr.write(f"Port {port} is unavailable ({exc.strerror}); trying another.\n")
    return _bind(0)


def _bind(port: int) -> socket.socket:
    # IPPROTO_TCP named, or asyncio leaves Nagle on for every connection accepted: measured,
    # each request after a connection's first then waited 40 ms on the delayed ACK.
    listening = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    # What asyncio sets itself on POSIX, so the restart after an update is not refused over
    # the old server's connections still in TIME_WAIT. On Windows the same option would let
    # this socket share a port another program is listening on.
    if sys.platform != "win32":
        listening.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listening.bind((HOST, port))
    except OSError:
        listening.close()
        raise
    return listening


async def _page_returns(app: Any) -> bool:
    """Whether a page of ours reached this server by itself within a few seconds.

    Waited for rather than assumed. The marker only says a page was open when the update
    began; after a reboot, or with the tab closed since, nothing comes back, and a start that
    opened nothing then would read as a double-click that did nothing.
    """
    try:
        await asyncio.wait_for(app.state.page_seen.wait(), PAGE_RETURN_SECONDS)
    except TimeoutError:
        return False
    return True


def _launched() -> bool:
    """Started by the launcher on macOS or Linux, where nothing else ends the app with it.

    Windows puts the app in a job object the kernel kills with the launcher. Elsewhere a
    launcher that is killed would leave the app running with nothing to quit it by, holding
    port 8000 and DuckDB's lock against every later start.
    """
    return bool(os.environ.get(updates.HOME_VARIABLE)) and sys.platform != "win32"


async def _orphaned(server: uvicorn.Server) -> None:
    """Close the app, gracefully, once the launcher that started it is gone.

    A process whose parent dies is handed to init, or to a subreaper: either way, a different
    parent. Compared against the id the launcher gave, since a launcher that died before this
    first ran would otherwise be read as the parent it was handed to, and never missed.
    """
    given = os.environ.get(updates.PID_VARIABLE, "")
    launcher = int(given) if given.isdigit() else os.getppid()
    while not server.should_exit:
        if os.getppid() != launcher:
            _log_to_file()
            server.should_exit = True
            return
        await asyncio.sleep(2)


def _log_to_file() -> None:
    """Point stdout and stderr at the launcher's log. They were pipes to the launcher, now gone,
    and a write nobody reads fails, which would cut the shutdown that closes DuckDB short."""
    home = updates.launcher_home()
    if home is None:
        return
    with contextlib.suppress(OSError):
        log = os.open(home / diagnostics.LOG_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        os.dup2(log, 1)
        os.dup2(log, 2)
        os.close(log)


def main() -> None:
    # A native crash, an access violation inside DuckDB say, otherwise ends with no trace at
    # all: this writes its stack to stderr, which the launcher keeps in its log.
    faulthandler.enable()
    if handover.started_by_old_launcher():
        listening = _listen()
        handover.run(listening, f"http://{HOST}:{listening.getsockname()[1]}")
        return
    failure = asyncio.run(_serve())
    if failure is not None:
        # A message, not a code: Python writes it last in the launcher's log, which is the
        # part of the log the launcher's message box shows.
        raise SystemExit(failure)


if __name__ == "__main__":
    main()
