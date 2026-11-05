"""Is a newer release out, and asking the launcher to install it.

The app never upgrades itself in place. On Windows a running process keeps its extension
modules open, so an install that touches ``duckdb`` or ``numpy`` fails part-way and leaves a
broken tree behind. The launcher supervises the app instead, on Windows, macOS and Linux alike:
Update writes the wanted version to a file, the app exits, the launcher installs that version
with ``uv`` and starts the app again.

The file is what matters, not the restart. If the app cannot close itself the request still
sits there, and the next start applies it — so a failed shutdown costs the user one manual
restart rather than the update.

Run without the launcher — a development checkout, or the ``uvx --from`` route in CLAUDE.md —
there is nothing to hand the request to. Updates then report themselves unavailable and say
why, rather than failing at the last step.
"""

import asyncio
import contextlib
import json
import os
import platform
import random
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as installed_version
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2
import structlog
from packaging.version import InvalidVersion, Version

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = structlog.get_logger(__name__)

PACKAGE = "alpha-harness"
REPOSITORY_VARIABLE = "ALPHA_HARNESS_REPOSITORY"
REPOSITORY = os.environ.get(REPOSITORY_VARIABLE) or "residual-lab/alpha-harness"
_OWNER, _, _NAME = REPOSITORY.partition("/")
#: GitHub's latest release, as the release workflow copies it to GitHub Pages. Never the API:
#: that allows sixty anonymous calls an hour per address, and a college hostel puts hundreds
#: of people behind one. Pages is a CDN with no such limit.
LATEST_URL = f"https://{_OWNER.lower()}.github.io/{_NAME}/latest.json"
#: Where a person goes to fetch a release by hand, when the app cannot do it for them.
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases/latest"

#: Set by the launcher to the directory it owns: ``uv.exe``, the Python it fetched, the venv.
HOME_VARIABLE = "ALPHA_HARNESS_HOME"
#: Set by the launcher to its own version. Absent when the app was started another way, and
#: on any exe built before this variable existed.
LAUNCHER_VARIABLE = "ALPHA_HARNESS_LAUNCHER"
#: The launcher's process id, from a launcher new enough to say so.
PID_VARIABLE = "ALPHA_HARNESS_LAUNCHER_PID"
#: Set by the desktop app, which shows the app in a window of its own. The PyInstaller
#: launchers before it never set it.
SHELL_VARIABLE = "ALPHA_HARNESS_SHELL"

#: The oldest desktop app this release works fully with.
#:
#: Raised by hand, and only when a desktop app change actually matters — it is rebuilt every
#: release, so comparing it to the wheel would nag after every update for nothing. Its first
#: release is all any needs so far.
LAUNCHER_NEEDED = "2026.10.21rc1"


def _launcher_asset() -> str:
    """The release download for this machine, named as the release names it."""
    if sys.platform == "win32":
        return "AlphaHarness-windows-x64-setup.exe"
    arm = platform.machine().lower() in ("arm64", "aarch64")
    if sys.platform == "darwin":
        return f"AlphaHarness-macOS-{'arm64' if arm else 'x86_64'}.dmg"
    return "AlphaHarness-linux-amd64.deb"


#: The launcher download to fetch for this machine, for the notice that asks for a newer one.
LAUNCHER_FILE = _launcher_asset()
#: Read by the launcher once the app has exited, then deleted.
REQUEST_FILE = "update-request.json"
#: Left by an app handing itself over to an update: its page reloads itself once the new one
#: answers, so the new one must not open a second window beside it.
RELOADING_FILE = "page-reloads.json"
#: Longer than any install takes. Past it the page has surely given up, and a start opens one.
RELOADING_SECONDS = 1800
#: Written by the launcher when an install failed, so the app can say why rather than offer
#: the same update again with no explanation.
ERROR_FILE = "update-error.json"
#: Written once the app serves, naming its version: the launcher rolls back a new version
#: that exits with an error before it ever did.
SERVED_FILE = "served.json"
#: The last answer, kept across restarts so that starting the app is no reason to ask again.
CHECK_FILE = "update-check.json"

#: Hours between scheduled looks at GitHub, from Settings; 0 looks only when asked. A failed
#: look waits as long as a good one, so an offline laptop does not retry all day; "Check now"
#: is there for the impatient.
check_hours = 1
#: Install a newer release by itself once nothing is simulating, from Settings.
auto_install = True
#: However often "Check now" is pressed, GitHub is asked at most this often.
FORCE_GAP_SECONDS = 60.0
#: The first look waits this long after startup, picked at random, so copies started together
#: (a lab's machines switched on at nine) do not all ask in the same second.
STARTUP_DELAY_SECONDS = (5.0, 30.0)
#: How often the watcher wakes to see whether a look or an install is due.
WATCH_SECONDS = 60.0
CHECK_TIMEOUT = 10.0
#: Release notes are shown in a dialog, not a document.
NOTES_LIMIT = 4000


@dataclass(frozen=True, slots=True)
class Release:
    version: str
    notes: str
    url: str
    published_at: str | None
    #: The wheel's own download URL, read from the release rather than built from the version.
    #: A constructed name breaks the moment a build tags it anything but ``py3-none-any``.
    wheel_url: str | None = None


#: Serialises the look at GitHub, so concurrent callers make one call between them.
_checking = asyncio.Lock()

#: ``(monotonic time, wall-clock ISO time, release, error)`` of the last look at GitHub.
_cache: tuple[float, str, Release | None, str | None] | None = None
#: Before this, a look nobody asked for is put off; see :data:`STARTUP_DELAY_SECONDS`.
_not_before = time.monotonic() + random.uniform(*STARTUP_DELAY_SECONDS)
#: Whether :data:`CHECK_FILE` was read yet, which happens once, on the first look.
_restored = False


def current() -> str:
    """The running version, from the installed distribution's own metadata.

    Releases are dated (``2026.9.21``). The repository itself stays at ``0.0.0.dev0``, which
    the release workflow stamps over — so any version below a real date is a checkout.
    """
    try:
        return installed_version(PACKAGE)
    except PackageNotFoundError:
        # A source checkout with nothing installed has no distribution to read.
        return "0.0.0.dev0"


def is_release() -> bool:
    """Whether this is a published build rather than a development tree."""
    parsed = _parse(current())
    return parsed is not None and not parsed.is_devrelease


def launcher_version() -> str | None:
    """Which launcher started this app, when one did and it says so."""
    return os.environ.get(LAUNCHER_VARIABLE) or None


def launcher_outdated() -> bool:
    """Whether the exe around this app is older than this release needs.

    An exe that does not name itself predates :data:`LAUNCHER_VARIABLE`, so it is older than
    anything that could have set it. An app started without a launcher has none to update.
    """
    if launcher_home() is None:
        return False
    # A PyInstaller launcher, which the desktop app replaces whatever its version.
    if not os.environ.get(SHELL_VARIABLE):
        return True
    running = launcher_version()
    return running is None or is_newer(LAUNCHER_NEEDED, running)


def launcher_home() -> Path | None:
    """The directory the launcher owns, or ``None`` when the app was started some other way."""
    raw = os.environ.get(HOME_VARIABLE)
    return Path(raw) if raw else None


def _parse(raw: str) -> Version | None:
    try:
        return Version(raw.removeprefix("v"))
    except InvalidVersion:
        return None


def is_newer(candidate: str, running: str) -> bool:
    """Whether ``candidate`` is a release to move to. Anything unreadable is not."""
    new, now = _parse(candidate), _parse(running)
    return new is not None and now is not None and new > now


async def latest(*, force: bool = False) -> tuple[Release | None, str | None]:
    """The newest published release and why it could not be read, each possibly ``None``.

    The last answer, good or failed, holds for :data:`check_hours`; with checks off, GitHub
    is asked only when forced.
    """
    global _restored
    # One caller at a time. Several open tabs poll this at once, and without the gate each
    # one that arrives while the cache is cold makes its own call. Whoever waits reads the
    # answer the first one stored.
    async with _checking:
        if not _restored:
            _restored = True
            _restore()
        if _cache is not None:
            age = time.monotonic() - _cache[0]
            fresh = check_hours == 0 or age < check_hours * 3600
            if (force and age < FORCE_GAP_SECONDS) or (not force and fresh):
                return _cache[2], _cache[3]
        elif not force and check_hours == 0:
            return None, None
        if not force and time.monotonic() < _not_before:
            return (_cache[2], _cache[3]) if _cache is not None else (None, None)

        return await _ask()


def checked_at() -> str | None:
    """When GitHub was last asked, as an ISO time, or ``None`` if it has not been yet."""
    return _cache[1] if _cache is not None else None


async def watch(
    idle: Callable[[], Awaitable[bool]], install: Callable[[Release], Awaitable[None]]
) -> None:
    """Look for a release on the Settings schedule, and install one by itself when allowed.

    An install closes the app, so it waits for a moment nothing is simulating: a restart
    mid-run loses nothing, but it would stall every core for the minute it takes. A version
    whose install already failed is left to the user, or it would be retried forever.
    """
    await asyncio.sleep(max(0.0, _not_before - time.monotonic()))
    while True:
        try:
            release, _ = await latest()
            if (
                release is not None
                and auto_install
                and is_release()
                and is_newer(release.version, current())
                and launcher_home() is not None
                and pending() is None
                and _launcher_json(ERROR_FILE).get("version") != release.version
                and await idle()
            ):
                log.warning("update.auto_install", version=release.version)
                await install(release)
                return
        # One failed look or install must not end the watching.
        except Exception:
            log.warning("update.watch_failed", exc_info=True)
        await asyncio.sleep(WATCH_SECONDS)


async def _ask() -> tuple[Release | None, str | None]:
    global _cache
    release: Release | None = None
    problem: str | None = None
    try:
        async with httpx2.AsyncClient(timeout=CHECK_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(
                LATEST_URL, headers={"User-Agent": f"AlphaHarness/{current()}"}
            )
        if response.status_code == 404:
            problem = "No release has been published yet."
        elif response.status_code >= 400:
            problem = f"GitHub answered {response.status_code}."
        else:
            release = _read(response.json())
            if release is None:
                problem = "GitHub's answer did not name a version."
    except httpx2.HTTPError as exc:
        problem = f"Could not reach GitHub: {exc}"
    except ValueError:
        # A Wi-Fi sign-in page or a proxy, answering in GitHub's place with a page of its own.
        problem = "Something other than GitHub answered, such as a Wi-Fi sign-in page."

    checked = datetime.now(UTC).isoformat(timespec="seconds")
    _cache = (time.monotonic(), checked, release, problem)
    if problem is None:
        _save(checked, release)
    return release, problem


def _save(checked: str, release: Release | None) -> None:
    """Keep an answer for the next start. Only a real one: a failed look is worth retrying
    once the app is started again, which is often how someone fixes their connection."""
    home = launcher_home()
    if home is None:
        return
    body = {"at": time.time(), "checkedAt": checked, "release": release and asdict(release)}
    with contextlib.suppress(OSError):
        (home / CHECK_FILE).write_text(json.dumps(body), encoding="utf-8")


def _restore() -> None:
    """The answer a previous run kept, aged by the time since."""
    global _cache
    body = _launcher_json(CHECK_FILE)
    at, checked, raw = body.get("at"), body.get("checkedAt"), body.get("release")
    age = time.time() - at if isinstance(at, int | float) else -1.0
    if age < 0 or not isinstance(checked, str):
        return
    try:
        release = Release(**raw) if isinstance(raw, dict) else None
    except TypeError:
        return
    _cache = (time.monotonic() - age, checked, release, None)


def _read(body: Any) -> Release | None:
    # The file is what ``/releases/latest`` named, which already skips drafts and pre-releases,
    # so this is belt and braces. A release candidate reaching consultants is the one mistake
    # this file cannot take back.
    if not isinstance(body, dict) or body.get("draft") or body.get("prerelease"):
        return None
    tag = body.get("tag_name")
    if not isinstance(tag, str):
        return None
    parsed = _parse(tag)
    if parsed is None or parsed.is_prerelease:
        return None
    return Release(
        version=tag.removeprefix("v"),
        notes=str(body.get("body") or "")[:NOTES_LIMIT],
        url=str(body.get("html_url") or ""),
        published_at=body.get("published_at")
        if isinstance(body.get("published_at"), str)
        else None,
        wheel_url=_wheel_of(body.get("assets")),
    )


def _wheel_of(assets: Any) -> str | None:
    """The release's own wheel, by extension rather than by a name we guessed."""
    if not isinstance(assets, list):
        return None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name, url = asset.get("name"), asset.get("browser_download_url")
        # Named as well as suffixed: a release may carry more than one wheel one day,
        # and installing whichever came first would be a coin toss.
        if (
            isinstance(name, str)
            and name.startswith("alpha_harness-")
            and name.endswith(".whl")
            and isinstance(url, str)
        ):
            return url
    return None


def request(version: str, *, wheel_url: str | None = None) -> Path:
    """Record the version the launcher should install, and return the file it wrote.

    Raises :class:`RuntimeError` when nothing is supervising this process, because writing a
    request nobody reads would report an update that never happens.
    """
    home = launcher_home()
    if home is None:
        raise RuntimeError(
            "This copy was not started by the Alpha Harness launcher, so it cannot update "
            "itself. Install the new version the same way you installed this one."
        )
    home.mkdir(parents=True, exist_ok=True)
    # A previous failure is the user's answer to "why am I still on the old version"; once
    # they have asked again it is history.
    (home / ERROR_FILE).unlink(missing_ok=True)
    path = home / REQUEST_FILE
    path.write_text(
        json.dumps(
            {
                "version": version,
                "wheelUrl": wheel_url,
                "requestedAt": time.time(),
                "from": current(),
            }
        ),
        encoding="utf-8",
    )
    (home / RELOADING_FILE).write_text(json.dumps({"at": time.time()}), encoding="utf-8")
    log.warning("update.requested", version=version, path=str(path))
    return path


def served() -> None:
    """Tell the launcher this version came up, so a later exit is not taken for a bad update."""
    home = launcher_home()
    if home is None:
        return
    try:
        (home / SERVED_FILE).write_text(json.dumps({"version": current()}), encoding="utf-8")
    except OSError:
        log.warning("update.served_unwritten", exc_info=True)


def page_reloads() -> bool:
    """Whether a page left open by the version before this one will reload onto it.

    Taken once: only the start straight after an update is spared from opening a window.
    """
    at = _launcher_json(RELOADING_FILE).get("at")
    home = launcher_home()
    if home is not None:
        (home / RELOADING_FILE).unlink(missing_ok=True)
    return isinstance(at, int | float) and time.time() - at < RELOADING_SECONDS


def _launcher_json(name: str) -> dict[str, Any]:
    """One of the launcher's JSON files, or nothing when it is absent or unreadable."""
    home = launcher_home()
    if home is None:
        return {}
    try:
        body = json.loads((home / name).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def last_failure() -> str | None:
    """Why the launcher could not install the last requested version, if it could not.

    Without this an install that failed leaves the same Update button on screen and no
    account of why nothing changed.
    """
    body = _launcher_json(ERROR_FILE)
    version, reason = body.get("version"), body.get("error")
    if not isinstance(reason, str):
        return None
    return f"Installing {version} failed: {reason}" if version else f"The update failed: {reason}"


def pending() -> str | None:
    """The version already requested and not yet installed, if any."""
    wanted = _launcher_json(REQUEST_FILE).get("version")
    return wanted if isinstance(wanted, str) else None
