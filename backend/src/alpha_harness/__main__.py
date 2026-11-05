"""``alpha-harness``: the backend and its built UI in one process, opened in the browser."""

import asyncio
import errno
import os
import socket
import sys
from typing import Any

import uvicorn

from . import updates
from .window import open_window

HOST = "127.0.0.1"
PORT = 8000
#: A port another program holds: POSIX's number, and Windows' own (WSAEADDRINUSE).
PORT_TAKEN = frozenset({errno.EADDRINUSE, 10048})
#: How long a start straight after an update waits for the page that asked for it to return.
#: It asks every 1.5 s (RECONNECT_MS in the UI's update.tsx).
PAGE_RETURN_SECONDS = 10.0


async def _serve() -> str | None:
    """Serve until the app is closed. Returns why it could not start, if it could not."""
    listening = _listen()
    from .main import app

    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT))
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
        updates.served()
        # Taken on every start, so a desktop window's update never leaves it for a later one.
        reloads = updates.page_reloads()
        # Straight after an update the page that asked for it reloads onto this one by itself.
        if not os.environ.get(updates.SHELL_VARIABLE) and not (
            reloads and await _page_returns(app)
        ):
            open_window(f"http://{HOST}:{PORT}")
    await serving
    if watching is not None:
        watching.cancel()
    failure = getattr(app.state, "startup_failure", None)
    if failure is None:
        return None
    return f"Alpha Harness could not start: {str(failure) or type(failure).__name__}"


def _listen() -> socket.socket:
    """The app's socket, bound before anything else starts, or a plain reason it cannot be.

    Bound here rather than by uvicorn, which reports a port in use as a traceback ending in
    ``SystemExit: 3`` — the last lines the launcher shows — and only after the catalog has
    been opened for a server that was never going to serve.
    """
    listening = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # What asyncio sets itself on POSIX, so the restart after an update is not refused over
    # the old server's connections still in TIME_WAIT. On Windows the same option would let
    # this socket share a port another program is listening on.
    if sys.platform != "win32":
        listening.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listening.bind((HOST, PORT))
    except OSError as exc:
        listening.close()
        if exc.errno not in PORT_TAKEN:
            raise
        # A message, not a code: Python writes it to stderr — the launcher's log — and exits 1.
        raise SystemExit(
            f"Alpha Harness could not start: another program is using port {PORT}. "
            "Close it, or restart the computer, then open Alpha Harness again."
        ) from None
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
            server.should_exit = True
            return
        await asyncio.sleep(2)


def main() -> None:
    failure = asyncio.run(_serve())
    if failure is not None:
        # A message, not a code: Python writes it last in the launcher's log, which is the
        # part of the log the launcher's message box shows.
        raise SystemExit(failure)


if __name__ == "__main__":
    main()
