"""Started by the retired PyInstaller launcher: hand over to the desktop app, never run beside it.

Both share one folder: its venv slots, ``harness.db`` and the catalog. Run from the old
launcher, this build would migrate the databases under the desktop app and ask the old launcher
to install updates into the slots the desktop app runs. So nothing is opened here. The desktop
app is started if it is installed; otherwise one page asks for it, then closes itself.
"""

import asyncio
import html
import os
import socket
import time
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING

import structlog
import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from . import updates
from .window import detached, open_window

if TYPE_CHECKING:
    from starlette.requests import Request

log = structlog.get_logger(__name__)

#: Written by the desktop app on every start, naming its own executable.
DESKTOP_FILE = "desktop-app.txt"
#: An abandoned page must not hold the old launcher, and its lock on the folder, for good.
IDLE_SECONDS = 600.0
DOWNLOAD_URL = f"https://github.com/{updates.REPOSITORY}/releases/latest/download/"


def started_by_old_launcher() -> bool:
    """A launcher started this app, and it is not the desktop app."""
    return updates.launcher_home() is not None and not os.environ.get(updates.SHELL_VARIABLE)


def desktop_app() -> Path | None:
    """The desktop app's executable, if it has run on this machine and is still there."""
    home = updates.launcher_home()
    if home is None:
        return None
    try:
        path = Path((home / DESKTOP_FILE).read_text(encoding="utf-8").strip())
    except OSError:
        return None
    return path if path.is_file() else None


def run(listening: socket.socket, url: str) -> None:
    """Start the desktop app, or serve the page that asks for it until closed or left idle."""
    app = desktop_app()
    if app is not None:
        try:
            # Exiting makes the old launcher exit too, which frees the folder for the new one.
            detached([str(app)])
        except OSError as exc:
            log.warning("handover.launch_failed", app=str(app), error=str(exc))
        else:
            log.warning("handover.desktop_app", app=str(app))
            listening.close()
            return
    log.warning("handover.page", installed=app is not None)
    asyncio.run(_offer(listening, url, installed=app is not None))


async def _offer(listening: socket.socket, url: str, *, installed: bool) -> None:
    last_seen = time.monotonic()

    async def page(_request: Request) -> HTMLResponse:
        nonlocal last_seen
        last_seen = time.monotonic()
        return HTMLResponse(_page(installed=installed))

    async def close(request: Request) -> Response:
        # The header a page elsewhere cannot send without asking first, as the app itself checks.
        if request.headers.get("x-harness-client") != "1":
            return Response(status_code=403)
        server.should_exit = True
        return Response(status_code=204)

    routes = [
        # The old launcher's tray Quit asks here, as it asked the app.
        Route("/api/quit", close, methods=["POST"]),
        Route("/{path:path}", page),
    ]
    server = uvicorn.Server(
        uvicorn.Config(Starlette(routes=routes), log_level="warning", lifespan="off")
    )

    async def watch() -> None:
        while not server.started:  # noqa: ASYNC110 - uvicorn has a flag, not an event
            await asyncio.sleep(0.1)
        if url:
            open_window(url)
        while not server.should_exit:
            await asyncio.sleep(5)
            if time.monotonic() - last_seen > IDLE_SECONDS:
                server.should_exit = True

    watching = asyncio.create_task(watch())
    try:
        await server.serve(sockets=[listening])
    finally:
        watching.cancel()


PAGE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alpha Harness</title>
<style>
:root { color-scheme: light dark; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
body {
  margin: 0;
  min-height: 100vh;
  display: grid;
  place-items: center;
  background: Canvas;
  color: CanvasText;
}
main { max-width: 34rem; padding: 24px; display: flex; flex-direction: column; gap: 16px; }
h1 { margin: 0; font-size: 20px; }
p { margin: 0; line-height: 1.5; }
.muted { opacity: 0.7; font-size: 14px; }
.row { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
a.button, button {
  font: inherit;
  padding: 8px 14px;
  border-radius: 6px;
  border: 1px solid color-mix(in srgb, CanvasText 25%, transparent);
  background: transparent;
  color: inherit;
  text-decoration: none;
  cursor: pointer;
}
a.primary { background: #3e63dd; border-color: #3e63dd; color: #fff; }
</style>
</head>
<body>
<main>
<h1>Alpha Harness now runs as a desktop app</h1>
<p>This launcher is retired, so nothing runs from it any more. $lead</p>
<div class="row">
<a class="button primary" href="$download">Download the desktop app</a>
<a href="$releases" target="_blank" rel="noopener noreferrer">All downloads</a>
<button type="button" id="close">Close</button>
</div>
<p class="muted" id="note">This page closes itself after ten minutes.</p>
</main>
<script>
document.getElementById("close").addEventListener("click", async () => {
  const headers = { "X-Harness-Client": "1" };
  await fetch("/api/quit", { method: "POST", headers }).catch(() => {});
  document.getElementById("note").textContent = "Closed. You can close this tab.";
});
</script>
</body>
</html>""")


def _page(*, installed: bool) -> str:
    lead = (
        "The desktop app is installed but did not open from here. Open Alpha Harness from "
        "your apps, then close this page."
        if installed
        else "Install the desktop app, then open Alpha Harness from your apps. Your Alphas, "
        "tasks and settings are kept: it uses the same folder."
    )
    return PAGE.substitute(
        lead=lead,
        download=html.escape(DOWNLOAD_URL + updates.LAUNCHER_FILE),
        releases=html.escape(updates.RELEASES_PAGE),
    )


if __name__ == "__main__":
    import sys
    import tempfile
    import threading

    import httpx2

    def serve(installed: bool) -> tuple[str, threading.Thread]:
        listening = socket.socket()
        listening.bind(("127.0.0.1", 0))
        port = listening.getsockname()[1]
        thread = threading.Thread(
            target=lambda: asyncio.run(_offer(listening, "", installed=installed)), daemon=True
        )
        thread.start()
        return f"http://127.0.0.1:{port}", thread

    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as folder:
        os.environ[updates.HOME_VARIABLE] = folder
        os.environ.pop(updates.SHELL_VARIABLE, None)
        checks["the old launcher is told apart"] = started_by_old_launcher()
        os.environ[updates.SHELL_VARIABLE] = "tauri"
        checks["...from the desktop app"] = not started_by_old_launcher()
        checks["no record, no desktop app"] = desktop_app() is None
        exe = Path(folder) / "alpha-harness.exe"
        (Path(folder) / DESKTOP_FILE).write_text(f"{exe}\n", encoding="utf-8")
        checks["a recorded path that is gone is ignored"] = desktop_app() is None
        exe.write_bytes(b"")
        checks["a recorded path that exists is found"] = desktop_app() == exe

    base, thread = serve(installed=False)
    for _ in range(50):
        try:
            answer = httpx2.get(f"{base}/matrix")
            break
        except httpx2.TransportError:
            time.sleep(0.1)
    else:
        raise SystemExit("handover self-check: the page never served")
    checks["any path shows the page"] = "Download the desktop app" in answer.text
    checks["a close without the header is refused"] = (
        httpx2.post(f"{base}/api/quit").status_code == 403 and thread.is_alive()
    )
    checks["a close with it ends the server"] = (
        httpx2.post(f"{base}/api/quit", headers={"X-Harness-Client": "1"}).status_code == 204
    )
    thread.join(timeout=5)
    checks["...and the server is gone"] = not thread.is_alive()

    # The socket this page and the app serve on: a taken port moves them, never stops them.
    from . import __main__ as entry

    taken = [socket.socket() for _ in range(2)]
    for held in taken:
        held.bind(("127.0.0.1", 0))
        held.listen()
    entry.PORT, entry.FALLBACK_PORT = (held.getsockname()[1] for held in taken)
    with entry._listen() as moved:  # pyright: ignore[reportPrivateUsage]
        checks["both ports taken, any free one"] = moved.getsockname()[1] not in {
            entry.PORT,
            entry.FALLBACK_PORT,
        }
    taken[1].close()
    with entry._listen() as fallback:  # pyright: ignore[reportPrivateUsage]
        checks["the usual port taken, the fallback"] = (
            fallback.getsockname()[1] == entry.FALLBACK_PORT
        )
    taken[0].close()
    if failed := [name for name, ok in checks.items() if not ok]:
        raise SystemExit(f"handover self-check failed: {'; '.join(failed)}")
    sys.stdout.write(f"{len(checks)} cases as expected\n")
