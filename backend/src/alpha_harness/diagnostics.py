"""A file a person can send when they ask for help: what runs, and the end of the log, scrubbed.

The log names the Windows username in every path, and carries Persona inquiry ids and Alpha
ids. The export keeps where a path was without whose it was, and swaps each id for a short
hash, so one id can still be followed through the file without being readable.
"""

import getpass
import hashlib
import os
import platform
import re
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import TYPE_CHECKING

from . import updates
from .window import detached

if TYPE_CHECKING:
    from collections.abc import Callable

LOG_FILE = "launcher.log"
#: The end of the log is where a fault is; whole, one ran to 31 MB.
TAIL_BYTES = 4 * 1024 * 1024
_INQUIRY = re.compile(r"\binq_[A-Za-z0-9]+")
_ALPHA = re.compile(
    r"(?P<lead>\balpha_?id['\"]?\s*[=:]\s*['\"]?|/alphas/)(?P<id>[A-Za-z0-9]{5,12})\b",
    re.IGNORECASE,
)


def _hashed(prefix: str, value: str) -> str:
    return prefix + hashlib.sha256(value.encode()).hexdigest()[:10]


def scrubber(home: PurePath, user: str) -> Callable[[str], str]:
    """What removes ``home``, ``user``, inquiry ids and Alpha ids from a text."""
    parts = [p for p in (part.rstrip("\\/") for part in home.parts) if p]
    # Either slash and any case: one log has C:\Users\Name, c:/users/name and C:\\Users\\Name.
    folder = re.compile(
        (r"[\\/]" if home.anchor in ("/", "\\") else "")
        + r"[\\/]+".join(re.escape(p) for p in parts),
        re.IGNORECASE,
    )
    # Too short a name would match inside ordinary words.
    name = (
        re.compile(rf"(?<![A-Za-z0-9]){re.escape(user)}(?![A-Za-z0-9])", re.IGNORECASE)
        if len(user) >= 3
        else None
    )

    def scrub(text: str) -> str:
        text = folder.sub("~", text) if parts else text
        if name is not None:
            text = name.sub("<user>", text)
        text = _INQUIRY.sub(lambda m: _hashed("inq#", m.group()), text)
        return _ALPHA.sub(lambda m: m.group("lead") + _hashed("alpha#", m.group("id")), text)

    return scrub


def _user() -> str:
    try:
        return getpass.getuser()
    except OSError:
        return Path.home().name


def _tail(folder: Path) -> str:
    """The last :data:`TAIL_BYTES` of the log, reaching into the rotated one when it is short."""
    chunks: list[bytes] = []
    left = TAIL_BYTES
    for name in (LOG_FILE, f"{LOG_FILE}.1"):
        if left <= 0:
            break
        try:
            with (folder / name).open("rb") as log:
                size = log.seek(0, os.SEEK_END)
                log.seek(max(0, size - left))
                chunk = log.read()
        except OSError:
            continue
        chunks.insert(0, chunk)
        left -= len(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _about() -> str:
    shell = os.environ.get(updates.SHELL_VARIABLE)
    started = "desktop app" if shell else "old launcher" if updates.launcher_home() else "none"
    return (
        f"Alpha Harness {updates.current()}\n"
        f"Launcher: {started} {updates.launcher_version() or ''}\n"
        f"System: {platform.platform()} {platform.machine()}\n"
        f"Python: {sys.version.split()[0]}\n"
        f"Written: {datetime.now(UTC).isoformat(timespec='seconds')}\n"
    )


def export() -> Path:
    """Write the zip into Downloads, where a person looks for what they just saved."""
    downloads = Path.home() / "Downloads"
    folder = downloads if downloads.is_dir() else updates.launcher_home() or Path.home()
    target = folder / f"alpha-harness-diagnostics-{datetime.now(UTC):%Y%m%d-%H%M%S}.zip"
    scrub = scrubber(Path.home(), _user())
    logs = updates.launcher_home()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("about.txt", scrub(_about()))
        if logs is not None:
            archive.writestr(LOG_FILE, scrub(_tail(logs)))
    return target


def open_folder(folder: Path) -> None:
    """Show ``folder`` in the system's file manager."""
    if sys.platform == "win32":
        os.startfile(folder)  # noqa: S606 - a folder of ours, opened by the shell
    else:
        detached(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])


if __name__ == "__main__":
    from pathlib import PureWindowsPath

    windows = scrubber(PureWindowsPath(r"C:\Users\Wanjiru"), "Wanjiru")
    posix = scrubber(PurePath("/home/wanjiru"), "wanjiru")
    line = (
        r"opened C:\Users\Wanjiru\AppData\Local\AlphaHarness and c:/users/wanjiru/x, "
        r"'C:\\Users\\WANJIRU\\y' for wanjiru, inquiry=inq_AbC123 alpha_id=QPKp9zVr "
        r"GET /alphas/QPKp9zVr"
    )
    scrubbed = windows(line)
    checks = {
        "every form of the home folder": "Wanjiru" not in scrubbed
        and scrubbed.count("~") == 3
        and r"~\AppData\Local\AlphaHarness" in scrubbed,
        "the bare username": "for <user>," in scrubbed,
        "inquiry ids hashed": "inq_AbC123" not in scrubbed and "inq#" in scrubbed,
        "the same Alpha hashes the same": "QPKp9zVr" not in scrubbed
        and len({m.group("id") for m in _ALPHA.finditer(scrubbed)}) == 1,
        "a POSIX home": posix("/home/wanjiru/.local/share/AlphaHarness")
        == "~/.local/share/AlphaHarness",
        "a short username is left alone": scrubber(PurePath("/home/al"), "al")("also al")
        == "also al",
    }
    if failed := [name for name, ok in checks.items() if not ok]:
        raise SystemExit(f"diagnostics self-check failed: {'; '.join(failed)}\n{scrubbed}")
    sys.stdout.write(f"{len(checks)} cases as expected\n")
