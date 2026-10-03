"""Alpha Harness on the desktop: keep a Python and a venv, run the app, apply updates.

Frozen with PyInstaller into ``AlphaHarness.exe``, ``AlphaHarness.app`` or the Linux
``AlphaHarness``. It carries ``uv`` and nothing else — the app's own dependencies are a third
of a gigabyte, and a user who updates weekly should not re-download ``pyarrow`` every week. So
the heavy half is installed once into a per-user folder (:func:`home`) and an update is just
this project's wheel, a few megabytes.

The launcher is the *parent* of the app, which is what makes updating safe. Windows keeps a
running process's extension modules open, so an install that replaces ``duckdb`` under a live
app fails half-way. Here the app writes the version it wants and exits; the install happens
with nothing running; then the app starts again.

Deliberately standard library only: it is frozen separately from the app and must keep
working when the venv it manages does not.
"""

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

# The one module the launcher shares with the app: standard library only, and bundled from
# backend/src by the release build (`--paths backend/src`). From source, put that on PYTHONPATH.
from alpha_harness.window import open_window

#: Written in by the release workflow; the version a fresh machine installs.
BUILD_VERSION = "0.0.0"
#: Written in alongside it: the repository that built this exe, which must match the wheel's.
REPOSITORY = "residual-lab/alpha-harness"
DOWNLOAD = f"https://github.com/{REPOSITORY}/releases/download"

HOME_VARIABLE = "ALPHA_HARNESS_HOME"
#: The exe's own version, handed to the app. The in-app update replaces the wheel and never
#: this program, so without it the app cannot tell that the exe around it is years older.
LAUNCHER_VARIABLE = "ALPHA_HARNESS_LAUNCHER"
#: This process's id, handed to the app so it can tell when the launcher is gone — even if it
#: went before the app first looked, when the app's parent is already someone else.
PID_VARIABLE = "ALPHA_HARNESS_LAUNCHER_PID"
REQUEST_FILE = "update-request.json"
ERROR_FILE = "update-error.json"
#: The exe version that last installed itself over an older install, so it tries once only.
OUTGROWN_FILE = "launcher-upgrade.txt"
ACTIVE_FILE = "active-slot.txt"
LOCK_FILE = "running.lock"
LOG_FILE = "launcher.log"
#: Where the app serves; kept in step with ``alpha_harness.__main__``.
APP_PORT = 8000

WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"
#: Set on the background copy macOS and Linux run the supervisor in (see :func:`detach`).
DETACHED_VARIABLE = "ALPHA_HARNESS_DETACHED"
#: The claim's file descriptor, handed to that copy so the claim passes on with no gap.
LOCK_FD_VARIABLE = "ALPHA_HARNESS_LOCK_FD"
UV_NAME = "uv.exe" if WINDOWS else "uv"
#: The mark, bundled for the Linux applications menu; Windows and macOS read the exe's own.
ICON_NAME = "mark.svg"

#: Two environments, one live and one spare. An update builds the spare and flips the
#: pointer, so a failed install cannot touch what is currently working and a version that
#: will not start can be stepped back out of.
SLOTS = ("a", "b")

#: Long enough for a cold ``uv`` to fetch CPython and ~330 MB of wheels on a slow line.
INSTALL_TIMEOUT = 1800
#: An app that exits faster than this, right after an update, never came up at all. A real
#: session outlives it even if the user closes the browser immediately.
BOOT_SECONDS = 20.0


def home() -> Path:
    """Where everything the launcher owns lives. Per-user, so updating needs no elevation."""
    override = os.environ.get(HOME_VARIABLE)
    if override:
        return Path(override)
    if WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    elif MACOS:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "AlphaHarness"


def say(root: Path, message: str) -> None:
    """Append to the log. The exe is windowed, so this file is the only account of a run."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with (root / LOG_FILE).open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def recent(root: Path, lines: int = 8) -> str:
    """The tail of the log, for a message box. A traceback's last lines carry the reason."""
    try:
        return "\n".join(
            (root / LOG_FILE).read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        )
    except OSError as exc:
        return f"(the log could not be read: {exc})"


def fail(root: Path, message: str) -> None:
    """Report a failure the user can act on. Windowed processes have nowhere else to speak."""
    say(root, f"FATAL {message}")
    text = f"{message}\n\nDetails: {root / LOG_FILE}"
    # Looked up rather than imported: ``ctypes.windll`` exists only on Windows.
    windll = getattr(ctypes, "windll", None)
    if windll is not None:
        windll.user32.MessageBoxW(None, text, "Alpha Harness", 0x10)
        return
    # The message goes in as an argument, never pasted into the script, so no quote in it
    # can end the string early.
    if MACOS:
        dialog = [
            "osascript",
            "-e",
            "on run argv",
            "-e",
            'display dialog (item 1 of argv) with title "Alpha Harness" buttons {"OK"} '
            "default button 1 with icon stop",
            "-e",
            "end run",
            text,
        ]
    elif shutil.which("zenity"):
        # --no-markup: a path or an error with `<` or `&` in it is text, not Pango markup.
        dialog = ["zenity", "--error", "--title=Alpha Harness", "--no-markup", f"--text={text}"]
    elif shutil.which("kdialog"):
        dialog = ["kdialog", "--title", "Alpha Harness", "--error", text]
    elif shutil.which("notify-send"):
        dialog = ["notify-send", "--urgency=critical", "Alpha Harness", text]
    else:
        return
    try:
        subprocess.run(dialog, check=False, timeout=600)  # noqa: S603
    except (OSError, subprocess.SubprocessError) as exc:
        say(root, f"could not show the message: {exc}")


#: Held open for the life of the process; the OS drops it however this one ends.
_lock: int | None = None


def claim(root: Path) -> bool:
    """Whether this process may run, or another copy already holds the lock.

    First start fetches a Python and ~330 MB of wheels behind a windowed exe that shows
    nothing, so the user double-clicks again. Two launchers then run ``uv`` over one venv,
    race for port 8000 and open DuckDB twice, which is a corrupted install rather than a
    slow one.
    """
    global _lock
    if not WINDOWS:
        return _claim_unix(root)

    import msvcrt

    try:
        # Never ``open(..., "wb")``. That asks Windows for CREATE_ALWAYS, and truncating a
        # file whose first byte another process has locked is refused with a lock violation —
        # which would surface as an unhandled PermissionError in a process that has no
        # console to print it to. O_RDWR|O_CREAT opens fine and lets the lock do the deciding.
        handle = os.open(root / LOCK_FILE, os.O_RDWR | os.O_CREAT)
    except OSError as exc:
        # A lock we cannot create is not a reason to refuse to start.
        say(root, f"could not open the lock file, running unguarded: {exc}")
        return True
    try:
        msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
    except OSError:
        os.close(handle)
        return False
    _lock = handle
    return True


def _claim_unix(root: Path) -> bool:
    """The same claim on macOS and Linux: an advisory lock the kernel drops with the process."""
    global _lock
    import fcntl

    try:
        handle = os.open(root / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        say(root, f"could not open the lock file, running unguarded: {exc}")
        return True
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(handle)
        return False
    _lock = handle
    return True


def release() -> None:
    """Let go of the claim, for a process that hands the running over to another."""
    global _lock
    if _lock is not None:
        os.close(_lock)
        _lock = None


def bundled(name: str) -> Path:
    """A file PyInstaller unpacked, or one beside this script when run from source."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base or Path(__file__).parent) / name


def ensure_uv(root: Path) -> Path:
    """Copy the bundled uv out once, so an install is not reading from a temporary folder."""
    target = root / UV_NAME
    source = bundled(UV_NAME)
    if source.exists() and (not target.exists() or source.stat().st_size != target.stat().st_size):
        shutil.copy2(source, target)
    if not WINDOWS and target.exists():
        target.chmod(0o755)
    return target


def clean_environment() -> None:
    """Take back what PyInstaller put in this process's environment for itself, in place.

    A frozen launcher points ``LD_LIBRARY_PATH`` (Linux) and ``DYLD_*`` (macOS) at its own
    bundled libraries, and anything it starts inherits them: the venv's Python would then load
    our copy of OpenSSL instead of its own. PyInstaller keeps the original beside it. Done once,
    first, so every program started from here — uv, the app, the browser, a dialog — is clean.
    """
    for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH"):
        original = os.environ.pop(f"{name}_ORIG", None)
        if original is not None:
            os.environ[name] = original
        elif getattr(sys, "frozen", False):
            os.environ.pop(name, None)
    # Not PyInstaller's, but a user's: set globally, either points the venv's Python at some
    # other standard library, and the app fails to import before it can say why.
    for name in ("PYTHONHOME", "PYTHONPATH"):
        os.environ.pop(name, None)


def other(slot: str) -> str:
    return "b" if slot == "a" else "a"


def active(root: Path) -> str:
    recorded = (
        (root / ACTIVE_FILE).read_text(encoding="utf-8").strip()
        if (root / ACTIVE_FILE).exists()
        else ""
    )
    return recorded if recorded in SLOTS else "a"


def activate(root: Path, slot: str) -> None:
    """Point at a slot. One small write, which is what makes the swap atomic enough."""
    (root / ACTIVE_FILE).write_text(slot, encoding="utf-8")
    say(root, f"active slot -> {slot}")


def venv_python(root: Path, slot: str) -> Path:
    """The console interpreter, which is what ``uv`` expects to be pointed at."""
    if WINDOWS:
        return root / f"venv-{slot}" / "Scripts" / "python.exe"
    return root / f"venv-{slot}" / "bin" / "python"


def venv_pythonw(root: Path, slot: str) -> Path:
    """The windowed interpreter, so running the app flashes no console at the user.

    Only Windows has a separate one; elsewhere a process started without a terminal has none.
    """
    if WINDOWS:
        return root / f"venv-{slot}" / "Scripts" / "pythonw.exe"
    return venv_python(root, slot)


def slot_version(root: Path, slot: str) -> str | None:
    """What is installed in a slot, or ``None`` when it holds nothing usable."""
    if not venv_python(root, slot).exists():
        return None
    recorded = read_json(root / f"slot-{slot}.json").get("version")
    return recorded if isinstance(recorded, str) else None


def read_json(path: Path) -> dict[str, object]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def requested(root: Path) -> str | None:
    wanted = read_json(root / REQUEST_FILE).get("version")
    return wanted if isinstance(wanted, str) else None


def requested_wheel(root: Path) -> str | None:
    """The wheel URL the app read off the release, rather than one built from the version."""
    url = read_json(root / REQUEST_FILE).get("wheelUrl")
    return url if isinstance(url, str) and url.startswith(DOWNLOAD) else None


def release_of(version: str | None) -> tuple[int, ...]:
    """A version's leading numbers, enough to order dated releases: ``2026.10.1`` is
    ``(2026, 10, 1)``. A pre-release reads as its release, so it is never newer than one."""
    match = re.match(r"\d+(?:\.\d+)*", version or "")
    return tuple(int(part) for part in match.group().split(".")) if match else ()


def outgrown(root: Path, running: str | None) -> bool:
    """Whether this exe is newer than the version installed and has not installed itself yet.

    An install too broken to start never reaches the in-app update, and downloading the new
    exe is what a person does next; it used to keep starting the broken version all the same.
    Once per exe version: one that fails to start is reverted below and not tried again.
    """
    if running is None or release_of(BUILD_VERSION) <= release_of(running):
        return False
    try:
        tried = (root / OUTGROWN_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        tried = ""
    return tried != BUILD_VERSION


def note_failure(root: Path, version: str, reason: str) -> None:
    """Leave the app something to say. Starting the old version again with the same Update
    button on screen and no account of why is the one outcome a user cannot make sense of."""
    (root / ERROR_FILE).write_text(
        json.dumps({"version": version, "error": reason[:400], "time": time.time()}),
        encoding="utf-8",
    )


def install(root: Path, uv: Path, slot: str, version: str, wheel: str | None = None) -> None:
    """Build ``slot`` from scratch at exactly ``version``. Raises on failure.

    Always the slot that is *not* running. Installing over a live environment is how an
    interrupted upgrade bricks it: ``uv`` has already replaced half the files when the network
    drops, and "keeping the old version" is then a claim about a tree that no longer works.
    Here a failure leaves the running slot untouched by construction.

    Rebuilding rather than upgrading in place costs no download — the wheels are in
    ``UV_CACHE_DIR`` from the last install and uv hardlinks them — and it means a slot never
    carries a package left behind by a version two releases ago.

    ``wheel`` is the URL the app read off the release. Only a first install, which has no
    release to read, falls back to the conventional name.
    """
    wheel = wheel or f"{DOWNLOAD}/v{version}/alpha_harness-{version}-py3-none-any.whl"
    constraints = f"{DOWNLOAD}/v{version}/constraints.txt"
    say(root, f"installing {version} from {wheel}")

    # uv keeps its Python builds and cache inside the same directory, so uninstalling the app
    # is deleting one folder and nothing is left behind in the user's profile.
    environment = os.environ | {
        "UV_PYTHON_INSTALL_DIR": str(root / "python"),
        "UV_CACHE_DIR": str(root / "cache"),
        "UV_NO_CONFIG": "1",
        # Never a Python found on the machine. Measured: uv took one from ~/.local/bin, and a
        # Homebrew or python.org one is moved or removed by its own upgrades, taking the venv
        # built on it down too.
        "UV_PYTHON_PREFERENCE": "only-managed",
    }

    def run(*arguments: str) -> None:
        done = subprocess.run(  # noqa: S603 - fixed argv, version comes from our own release
            [str(uv), *arguments],
            capture_output=True,
            text=True,
            # Named, never inherited. ``text=True`` otherwise decodes with the console code
            # page — CP1252 on most Windows machines — and CP1252 has undefined bytes, so one
            # multi-byte character in uv's output raises UnicodeDecodeError and fails an
            # install that actually worked.
            encoding="utf-8",
            errors="replace",
            timeout=INSTALL_TIMEOUT,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        say(root, f"uv {arguments[0]} -> {done.returncode} {done.stderr.strip()[-2000:]}")
        if done.returncode != 0:
            raise RuntimeError(f"uv {arguments[0]} failed: {done.stderr.strip()[-400:]}")

    # The marker goes first: a half-built slot must never read as a working one.
    (root / f"slot-{slot}.json").unlink(missing_ok=True)
    shutil.rmtree(root / f"venv-{slot}", ignore_errors=True)
    run("venv", "--python", "3.14", str(root / f"venv-{slot}"))
    # Constrained to the versions the release was locked against, so a transitive dependency
    # publishing a breaking version cannot break an install that worked yesterday.
    run("pip", "install", "--python", str(venv_python(root, slot)), "-c", constraints, wheel)
    (root / f"slot-{slot}.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    say(root, f"installed {version} into slot {slot}")


# --- the notification area -----------------------------------------------------------------

#: Our own callback message. ``WM_APP`` begins the range Windows reserves for an application.
_WM_APP = 0x8000
_TRAY_MESSAGE = _WM_APP + 1
_WM_DESTROY, _WM_CLOSE, _WM_COMMAND = 0x0002, 0x0010, 0x0111
_WM_LBUTTONUP, _WM_LBUTTONDBLCLK, _WM_RBUTTONUP = 0x0202, 0x0203, 0x0205
_NIM_ADD, _NIM_DELETE = 0x0, 0x2
_NIF_MESSAGE, _NIF_ICON, _NIF_TIP = 0x1, 0x2, 0x4
_MF_STRING, _MF_SEPARATOR = 0x0000, 0x0800
_TPM_RIGHTBUTTON = 0x0002
_IDI_APPLICATION = 32512
_OPEN, _QUIT = 1, 2

#: How long the app is given to close itself before it is terminated. Worth waiting for: a
#: clean exit unwinds uvicorn's lifespan, which is what releases DuckDB's single-writer lock.
QUIT_SECONDS = 15.0

#: Set when the user chooses Quit, so the launcher can tell a deliberate exit from a crash.
_quitting = threading.Event()
#: The running app, so Quit can close it. ``None`` whenever none is running.
_child: subprocess.Popen[bytes] | None = None


def open_app() -> None:
    """Show the app: its own window where the default browser can draw one, else a tab."""
    open_window(f"http://127.0.0.1:{APP_PORT}")


def close_app(root: Path) -> None:
    """Ask the app to close itself; make sure it has.

    Asked over HTTP because there is no graceful signal to send a windowed process on
    Windows: no console, so no Ctrl-Break, and terminating it skips the shutdown that closes
    DuckDB. ``POST /api/quit`` is the app's own front door, and only 127.0.0.1 can knock.
    """
    _quitting.set()
    child = _child
    # The app refuses a write that carries no ``X-Harness-Client``, which is what stops
    # another page in the browser from driving it. The launcher is a client like any other.
    ask = urllib.request.Request(
        f"http://127.0.0.1:{APP_PORT}/api/quit", data=b"", headers={"X-Harness-Client": "1"}
    )
    try:
        with urllib.request.urlopen(ask, timeout=5) as answer:  # noqa: S310 - fixed loopback URL
            answer.read()
        say(root, "asked the app to close")
    except Exception as exc:  # noqa: BLE001 - an app that will not answer is still closing
        say(root, f"the app did not take the quit request: {exc}")
    if child is None:
        return
    try:
        child.wait(timeout=QUIT_SECONDS)
    except subprocess.TimeoutExpired:
        say(root, "the app did not close in time; terminating it")
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()


class Tray:
    """The notification-area icon: click to open the app, right-click to quit it.

    Win32 through ctypes rather than a library, because the launcher is frozen separately
    from the app and stays standard library only — and an icon is one hidden window and one
    message loop. Off Windows every method does nothing: macOS and Linux have no icon, and the
    app's own Quit Alpha Harness (in the account menu) closes it instead.

    Without this the app cannot be closed at all: it is a server with no window, so closing
    the browser leaves it running, holding port 8000 and the catalog lock.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.hwnd = 0
        self._thread: threading.Thread | None = None
        #: ctypes frees a callback as soon as nothing refers to it, and Windows then calls
        #: into freed memory. These live as long as the icon does.
        self._kept: list[Any] = []
        self._ready = threading.Event()
        #: The prototyped ``user32``, so :meth:`stop` posts through declared argtypes.
        self._user32: Any = None

    def start(self) -> None:
        if sys.platform != "win32":
            return
        self._thread = threading.Thread(target=self._guarded, name="tray", daemon=True)
        self._thread.start()
        # Nothing may be posted to the window until it exists.
        self._ready.wait(timeout=5)

    def stop(self) -> None:
        # Through the library the prototypes were declared on, never ``ctypes.windll``: that
        # one is a shared cache with no argtypes, so the window handle would go out truncated
        # and WM_CLOSE would be delivered to nothing. The loop would then never end.
        if self._user32 is not None and self.hwnd:
            self._user32.PostMessageW(self.hwnd, _WM_CLOSE, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _guarded(self) -> None:
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001 - no icon is a far smaller problem than no app
            say(self.root, f"the notification icon could not be created: {exc}")
        finally:
            self._ready.set()

    def _run(self) -> None:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        lresult = ctypes.c_ssize_t
        procedure = ctypes.WINFUNCTYPE(
            lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        )
        # Every prototype, declared. This is not tidiness. An undeclared ctypes argument is
        # passed as a C `int`, so a 64-bit handle arrives sign-extended from its low half —
        # 0x000001a2b3c4d5e6 becomes 0xffffffffb3c4d5e6 — and Win32 either fails or faults on
        # it. Measured. The same applies to a return value left at the default type.
        HWND, LPCWSTR, INT = wintypes.HWND, wintypes.LPCWSTR, ctypes.c_int  # noqa: N806
        prototypes: dict[Any, list[tuple[str, Any, list[Any]]]] = {
            user32: [
                (
                    "DefWindowProcW",
                    lresult,
                    [HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM],
                ),
                ("RegisterClassW", wintypes.ATOM, [ctypes.c_void_p]),
                (
                    "CreateWindowExW",
                    HWND,
                    [
                        wintypes.DWORD,
                        LPCWSTR,
                        LPCWSTR,
                        wintypes.DWORD,
                        INT,
                        INT,
                        INT,
                        INT,
                        HWND,
                        wintypes.HMENU,
                        wintypes.HINSTANCE,
                        wintypes.LPVOID,
                    ],
                ),
                ("DestroyWindow", wintypes.BOOL, [HWND]),
                (
                    "PostMessageW",
                    wintypes.BOOL,
                    [HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM],
                ),
                ("PostQuitMessage", None, [INT]),
                ("GetMessageW", INT, [ctypes.c_void_p, HWND, wintypes.UINT, wintypes.UINT]),
                ("TranslateMessage", wintypes.BOOL, [ctypes.c_void_p]),
                ("DispatchMessageW", lresult, [ctypes.c_void_p]),
                ("GetCursorPos", wintypes.BOOL, [ctypes.c_void_p]),
                ("SetForegroundWindow", wintypes.BOOL, [HWND]),
                ("CreatePopupMenu", wintypes.HMENU, []),
                (
                    "AppendMenuW",
                    wintypes.BOOL,
                    [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, LPCWSTR],
                ),
                (
                    "TrackPopupMenu",
                    wintypes.BOOL,
                    [
                        wintypes.HMENU,
                        wintypes.UINT,
                        INT,
                        INT,
                        INT,
                        HWND,
                        ctypes.c_void_p,
                    ],
                ),
                ("DestroyMenu", wintypes.BOOL, [wintypes.HMENU]),
                ("LoadIconW", wintypes.HICON, [wintypes.HINSTANCE, LPCWSTR]),
            ],
            shell32: [
                ("ExtractIconW", wintypes.HICON, [wintypes.HINSTANCE, LPCWSTR, wintypes.UINT]),
                ("Shell_NotifyIconW", wintypes.BOOL, [wintypes.DWORD, ctypes.c_void_p]),
            ],
            kernel32: [("GetModuleHandleW", wintypes.HMODULE, [LPCWSTR])],
        }
        for library, entries in prototypes.items():
            for name, restype, argtypes in entries:
                function = getattr(library, name)
                function.restype = restype
                function.argtypes = argtypes
        # A fresh WinDLL, not ``ctypes.windll``: that one is cached process-wide, and
        # declaring argtypes on it would change calls made anywhere else in this process.
        self._user32 = user32

        class WindowClass(ctypes.Structure):
            _fields_ = (
                ("style", wintypes.UINT),
                ("lpfnWndProc", procedure),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            )

        class IconData(ctypes.Structure):
            """``NOTIFYICONDATAW``, stopping at ``dwInfoFlags``.

            That is not an arbitrary place to stop: ``cbSize`` must equal one of the sizes
            Windows knows, and this one is exactly ``NOTIFYICONDATAW_V2_SIZE`` (952 on x64).
            Dropping the fields below ``szTip`` — none of which this uses — would make it a
            size Windows has never heard of, and ``Shell_NotifyIconW`` would answer FALSE and
            show nothing, with no error anywhere.
            """

            _fields_ = (
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
            )

        icon_data: IconData | None = None

        def menu(hwnd: int) -> None:
            where = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(where))
            handle = user32.CreatePopupMenu()
            user32.AppendMenuW(handle, _MF_STRING, _OPEN, "Open Alpha Harness")
            user32.AppendMenuW(handle, _MF_SEPARATOR, 0, None)
            user32.AppendMenuW(handle, _MF_STRING, _QUIT, "Quit Alpha Harness")
            # Windows dismisses a tray menu only while its owner is the foreground window,
            # and only repaints after one more message reaches that window.
            user32.SetForegroundWindow(hwnd)
            user32.TrackPopupMenu(handle, _TPM_RIGHTBUTTON, where.x, where.y, 0, hwnd, None)
            user32.PostMessageW(hwnd, 0, 0, 0)
            user32.DestroyMenu(handle)

        def chosen(command: int) -> None:
            if command == _OPEN:
                open_app()
            elif command == _QUIT:
                # Off the message loop: closing the app takes seconds, and a frozen icon
                # during them reads as a crash.
                threading.Thread(target=self._quit, name="tray-quit", daemon=True).start()

        def handle(hwnd: int, message: int, wparam: int, lparam: int) -> int:
            if message == _TRAY_MESSAGE:
                if lparam in (_WM_LBUTTONUP, _WM_LBUTTONDBLCLK):
                    open_app()
                elif lparam == _WM_RBUTTONUP:
                    menu(hwnd)
            elif message == _WM_COMMAND:
                chosen(wparam & 0xFFFF)
            elif message == _WM_DESTROY:
                if icon_data is not None:
                    shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(icon_data))
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        instance = kernel32.GetModuleHandleW(None)
        callback = procedure(handle)
        registration = WindowClass()
        registration.lpfnWndProc = callback
        registration.hInstance = instance
        registration.lpszClassName = "AlphaHarnessTray"
        self._kept += [callback, registration]
        user32.RegisterClassW(ctypes.byref(registration))

        # Never shown: it exists only to receive the icon's callbacks.
        hwnd = user32.CreateWindowExW(
            0, "AlphaHarnessTray", "Alpha Harness", 0, 0, 0, 0, 0, None, None, instance, None
        )
        if not hwnd:
            raise OSError(f"CreateWindowExW failed: {ctypes.get_last_error()}")
        self.hwnd = hwnd

        # The exe's own icon when frozen; Windows' generic one when run from source.
        icon = shell32.ExtractIconW(instance, sys.executable, 0)
        if not icon or icon == 1:
            # MAKEINTRESOURCE: a numbered resource is its ordinal cast to a string pointer.
            icon = user32.LoadIconW(None, ctypes.cast(_IDI_APPLICATION, wintypes.LPCWSTR))

        icon_data = IconData()
        icon_data.cbSize = ctypes.sizeof(IconData)
        icon_data.hWnd = hwnd
        icon_data.uID = 1
        icon_data.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
        icon_data.uCallbackMessage = _TRAY_MESSAGE
        icon_data.hIcon = icon
        icon_data.szTip = f"Alpha Harness {BUILD_VERSION}"[:127]
        self._kept.append(icon_data)
        if not shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(icon_data)):
            raise OSError(f"Shell_NotifyIconW failed: {ctypes.get_last_error()}")
        say(self.root, "notification icon added")

        self._ready.set()
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        self.hwnd = 0

    def _quit(self) -> None:
        close_app(self.root)
        self.stop()


#: Terminate everything in the job once the last handle to it closes. Windows closes ours
#: however this process ends — including a kill that runs no code here, which is the point.
_JOB_KILL_ON_CLOSE = 0x2000
#: Lets a process the app starts with ``CREATE_BREAKAWAY_FROM_JOB`` leave the job: the
#: browser window it opens, which must outlive Alpha Harness.
_JOB_BREAKAWAY_OK = 0x0800
_JOB_EXTENDED_LIMITS = 9
_PROCESS_SET_QUOTA, _PROCESS_TERMINATE = 0x0100, 0x0001

#: The job the app runs inside, and the library used to put it there. Created once, and the
#: handle is deliberately never closed: closing it is what kills the app.
_job: int | None = None
_kernel: Any = None


def guard_children(root: Path) -> None:
    """Arrange for the app to die with this launcher, however this launcher dies.

    ``start`` waits on the app, so a launcher that is *closed* stops it. A launcher that is
    *killed* — Task Manager, ``taskkill /F``, a crash — never returns from that wait, and the
    app keeps running with no icon and no window. It then holds DuckDB's single-writer lock
    and port 8000 against every later start, which fails with nothing on screen to explain it.

    A job object moves that guarantee into the kernel, where no code of ours has to run.
    Only the app is put in it; the browser is spawned by the shell rather than by us, so
    quitting Alpha Harness never closes the page the user was reading.
    """
    global _job, _kernel
    if sys.platform != "win32":
        return

    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        """``JOBOBJECT_BASIC_LIMIT_INFORMATION``. Only ``LimitFlags`` is set, but every field
        has to be here: the call is handed a size and reads flags at a fixed offset."""

        _fields_ = (
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class IoCounters(ctypes.Structure):
        _fields_ = tuple(
            (name, ctypes.c_uint64)
            for name in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")
        )

    class ExtendedLimits(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryLimit", ctypes.c_size_t),
            ("PeakJobMemoryLimit", ctypes.c_size_t),
        )

    # A fresh WinDLL for the same reason the tray keeps one: ``ctypes.windll`` is cached
    # process-wide, and declaring argtypes on it would change calls made anywhere else.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    prototypes = (
        ("CreateJobObjectW", wintypes.HANDLE, [ctypes.c_void_p, wintypes.LPCWSTR]),
        (
            "SetInformationJobObject",
            wintypes.BOOL,
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
        ),
        ("AssignProcessToJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE]),
        ("OpenProcess", wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
        ("CloseHandle", wintypes.BOOL, [wintypes.HANDLE]),
    )
    # Undeclared, ctypes passes every argument as a C ``int`` and truncates a 64-bit handle
    # to its low half, which fails in a way that looks like the call simply not working.
    for name, restype, argtypes in prototypes:
        function = getattr(kernel32, name)
        function.restype = restype
        function.argtypes = argtypes

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        say(root, f"could not create the job object: {ctypes.get_last_error()}")
        return
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = _JOB_KILL_ON_CLOSE | _JOB_BREAKAWAY_OK
    if not kernel32.SetInformationJobObject(
        job, _JOB_EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        say(root, f"could not set job limits: {ctypes.get_last_error()}")
        kernel32.CloseHandle(job)
        return
    _job, _kernel = job, kernel32


def adopt(root: Path, pid: int) -> None:
    """Put a started app into the job, so it cannot outlive this launcher."""
    if _job is None or _kernel is None:
        return
    # The two rights ``AssignProcessToJobObject`` requires, and nothing more.
    handle = _kernel.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not handle:
        say(root, f"could not open the app to guard it: {ctypes.get_last_error()}")
        return
    try:
        if not _kernel.AssignProcessToJobObject(_job, handle):
            say(root, f"could not guard the app: {ctypes.get_last_error()}")
    finally:
        _kernel.CloseHandle(handle)


def start(root: Path, slot: str) -> int:
    """Run the app to completion, with its output in the log. Returns its exit code.

    Held in ``_child`` rather than run to completion in one call, so the notification area's
    Quit has something to close.
    """
    global _child
    environment = os.environ | {
        HOME_VARIABLE: str(root),
        LAUNCHER_VARIABLE: BUILD_VERSION,
        PID_VARIABLE: str(os.getpid()),
        "PYTHONUTF8": "1",
    }
    say(root, f"starting app from slot {slot}")
    with (root / LOG_FILE).open("a", encoding="utf-8") as handle:
        _child = subprocess.Popen(  # noqa: S603 - the venv this launcher built
            [str(venv_pythonw(root, slot)), "-m", "alpha_harness"],
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        adopt(root, _child.pid)
        try:
            code = _child.wait()
        finally:
            _child = None
    say(root, f"app exited {code}")
    return code


def detach(root: Path) -> int:
    """Hand the running to a background copy of this launcher, and return straight away.

    macOS and Linux only. macOS opens an app that is already running by bringing it forward,
    never by starting it again, so a launcher that stayed running as the app would make a
    second double-click do nothing at all; and a windowless app that lingers reads as Not
    Responding. Handed on, the app the user clicked is gone within a second. The next click
    starts it afresh, meets the supervisor's claim, and opens the browser, as on Windows.
    """
    frozen = getattr(sys, "frozen", False)
    command = [sys.executable] if frozen else [sys.executable, str(Path(__file__).resolve())]
    # PYINSTALLER_RESET_ENVIRONMENT: without it the new copy takes itself for a child of this
    # one and runs from our unpacked folder, which is deleted the moment we exit.
    # The claim goes with it: an flock belongs to the open file, not to a process, so the copy
    # holds it the moment it starts and it is never free in between. Let go and claim again
    # instead, and a second double-click in that gap starts a second supervisor.
    handed = () if _lock is None else (_lock,)
    environment = os.environ | {
        DETACHED_VARIABLE: "1",
        "PYINSTALLER_RESET_ENVIRONMENT": "1",
        **({LOCK_FD_VARIABLE: str(_lock)} if _lock is not None else {}),
    }
    try:
        subprocess.Popen(  # noqa: S603 - this very program
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            pass_fds=handed,
        )
    except OSError as exc:
        fail(root, f"Could not start Alpha Harness: {exc}")
        return 1
    finally:
        # Only this process's copy of the descriptor: the lock stays with the one handed on.
        release()
    say(root, "supervisor started in the background")
    return 0


def _desktop_quoted(path: str) -> str:
    """A path as a Desktop Entry ``Exec`` argument, so one with a space or a quote still runs.

    The spec applies its string escapes before its quoting rules, which is why one literal
    backslash takes four.
    """
    escaped = "".join("\\\\\\\\" if c == "\\" else f"\\{c}" if c in '"`$' else c for c in path)
    return '"' + escaped.replace("%", "%%") + '"'


def register_menu(root: Path) -> None:
    """Put Alpha Harness in the Linux applications menu, pointing at wherever this file is.

    A downloaded binary is otherwise found again only by hunting through Downloads. Rewritten
    whenever the launcher has moved, so the entry never points at a file that is gone.
    """
    if WINDOWS or MACOS or not getattr(sys, "frozen", False):
        return
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    entry = data / "applications" / "alpha-harness.desktop"
    icon = root / "alpha-harness.svg"
    source = bundled(ICON_NAME)
    try:
        if source.exists() and (not icon.exists() or icon.read_bytes() != source.read_bytes()):
            icon.write_bytes(source.read_bytes())
        path = str(Path(sys.executable).resolve())
        text = "\n".join(
            (
                "[Desktop Entry]",
                "Type=Application",
                "Name=Alpha Harness",
                "Comment=Alpha generation for WorldQuant BRAIN",
                f"Exec={_desktop_quoted(path)}",
                f"Icon={icon}",
                "Terminal=false",
                "Categories=Office;Finance;",
                "",
            )
        )
        if not entry.exists() or entry.read_text(encoding="utf-8") != text:
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text(text, encoding="utf-8")
            say(root, f"applications menu entry -> {path}")
    except OSError as exc:
        say(root, f"could not add the applications menu entry: {exc}")


def take_claim(root: Path) -> bool:
    """Take over the claim the copy that started this one handed on (see :func:`detach`)."""
    global _lock
    import fcntl

    handed = os.environ.pop(LOCK_FD_VARIABLE, "")
    if not handed.isdigit():
        return claim(root)
    try:
        # Already ours, so this succeeds at once; it is here to prove the descriptor is.
        fcntl.flock(int(handed), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        say(root, f"the handed-on claim was not usable, claiming afresh: {exc}")
        return claim(root)
    # Handed on as inheritable, which it must not stay: subprocess closes it in every child
    # today, but a program started any other way would keep the claim alive after us.
    os.set_inheritable(int(handed), False)
    _lock = int(handed)
    return True


def main() -> int:
    clean_environment()
    root = home()
    root.mkdir(parents=True, exist_ok=True)
    say(root, f"launcher {BUILD_VERSION}")
    handed_over = bool(os.environ.get(DETACHED_VARIABLE))
    if not (take_claim(root) if handed_over else claim(root)):
        # Double-clicking a second time is not an error, it is someone asking to see the app.
        # Unless the first copy is still doing its first install, in which case there is
        # nothing to show yet and a browser tab would just fail to connect.
        if any(slot_version(root, slot) for slot in SLOTS):
            say(root, "already running; opening the browser at it")
            open_app()
        else:
            fail(
                root,
                "Alpha Harness is still installing.\n\nThis takes a couple of minutes the "
                "first time. It opens in your browser when it is ready.",
            )
        return 0
    if not WINDOWS and not handed_over:
        register_menu(root)
        return detach(root)
    try:
        uv = ensure_uv(root)
    except OSError as exc:
        fail(root, f"Could not unpack uv: {exc}")
        return 1

    guard_children(root)
    tray = Tray(root)
    tray.start()
    try:
        return supervise(root, uv)
    finally:
        tray.stop()


def supervise(root: Path, uv: Path) -> int:
    """Install what is wanted, run it, and keep running it while updates are requested."""
    while True:
        slot = active(root)
        running = slot_version(root, slot)
        wanted = requested(root) or running or BUILD_VERSION
        if requested(root) is None and outgrown(root, running):
            (root / OUTGROWN_FILE).write_text(BUILD_VERSION, encoding="utf-8")
            say(root, f"launcher {BUILD_VERSION} is newer than {running}; installing it")
            wanted = BUILD_VERSION
        swapped = False

        if running != wanted:
            # The empty slot, so the one in use survives a failure untouched. With nothing
            # installed at all there is no "in use" and the first slot is the only choice.
            target = other(slot) if running is not None else slot
            try:
                install(root, uv, target, wanted, requested_wheel(root))
                activate(root, target)
                slot, swapped = target, True
            except Exception as exc:  # noqa: BLE001 - every failure ends the same way
                if running is None:
                    fail(root, f"Could not install Alpha Harness {wanted}.\n{exc}")
                    return 1
                say(root, f"install failed, staying on {running}: {exc}")
                note_failure(root, wanted, str(exc))
        # Cleared whether or not it was applied: left behind, a request that cannot install
        # would retry on every start for good.
        (root / REQUEST_FILE).unlink(missing_ok=True)
        if _quitting.is_set():
            # Quit chosen during an install, which cannot be interrupted part-way without
            # leaving a half-built slot. Nothing is started now.
            return 0

        began = time.monotonic()
        code = start(root, slot)
        alive = time.monotonic() - began

        if _quitting.is_set():
            say(root, "closed from the notification area")
            return 0

        # A version that dies on the way up cannot report anything itself: no server, no page,
        # no button. The launcher is the only thing left that can notice, so it does.
        if swapped and code != 0 and alive < BOOT_SECONDS and slot_version(root, other(slot)):
            previous = other(slot)
            say(root, f"{wanted} exited {code} after {alive:.1f}s; reverting to {previous}")
            activate(root, previous)
            note_failure(
                root,
                wanted,
                f"It closed straight after starting (exit code {code}). "
                f"Alpha Harness went back to {slot_version(root, previous)}.",
            )
            continue

        if requested(root) is None:
            # Nothing else can report this. The app died before it could serve a page, and
            # the launcher is about to exit too, so without a box here the whole of what the
            # user sees is a double-click that does nothing — four times over, in our case.
            if code != 0:
                fail(
                    root,
                    f"Alpha Harness stopped unexpectedly (exit code {code}).\n\n{recent(root)}",
                )
            return code
        say(root, "update requested; restarting")


if __name__ == "__main__":
    raise SystemExit(main())
