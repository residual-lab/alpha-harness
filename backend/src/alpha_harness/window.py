"""Showing the app: in a window of its own where the default browser can draw one, else a tab.

A Chromium browser started with ``--app`` opens a page as a standalone window — its own
taskbar or Dock icon, no tabs, no address bar — which is the whole difference between a web
page and a desktop app to the person using it. Only the *default* browser is ever used, and
in its ordinary profile: a different browser, or a private profile, would start the app with
none of the drafts and settings the reader's browser already keeps for it. Anything else —
Firefox, Safari, a browser that cannot be found — gets a tab, exactly as before.
"""

import os
import plistlib
import re
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

#: The browsers that take ``--app``, as each system names its default.
WINDOWS_EXES = ("chrome.exe", "msedge.exe", "brave.exe", "chromium.exe", "vivaldi.exe")
MAC_BUNDLES = (
    "com.google.chrome",
    "com.microsoft.edgemac",
    "com.brave.browser",
    "org.chromium.chromium",
    "com.vivaldi.vivaldi",
)
#: A Linux default is a ``.desktop`` id; each word it can contain, and the commands it names.
LINUX_COMMANDS = {
    "chrome": ("google-chrome-stable", "google-chrome"),
    "chromium": ("chromium", "chromium-browser"),
    "edge": ("microsoft-edge-stable", "microsoft-edge"),
    "brave": ("brave-browser", "brave"),
    "vivaldi": ("vivaldi-stable", "vivaldi"),
}
#: Where Windows records the browser a person chose for each scheme.
WINDOWS_ASSOCIATIONS = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations"
#: The executable a shell ``open`` command runs, quoted or not: an unquoted path may itself
#: hold spaces, so it runs to ``.exe`` rather than to the first space.
WINDOWS_OPENER = re.compile(r'^\s*(?:"([^"]+)"|(.+?\.exe)\b)', re.IGNORECASE)
#: Lets a process leave the job the Windows launcher runs the app in. A browser started
#: inside it would be killed when Alpha Harness quits, taking the reader's other windows too.
BREAKAWAY = 0x01000000


def open_window(url: str) -> None:
    """Show ``url`` as an app window in the default browser, or in a tab if that cannot be."""
    command = _app_command(url)
    if command is not None:
        try:
            _detached(command)
            return
        except OSError:
            # An older Windows launcher's job refuses the breakaway; a tab is what it
            # always opened, so that is no worse than before.
            pass
    webbrowser.open(url)


def _detached(command: list[str]) -> None:
    """Start the browser so that nothing about this process's end can end it."""
    if sys.platform == "win32":
        subprocess.Popen(  # noqa: S603 - the default browser's own executable
            command,
            creationflags=BREAKAWAY | subprocess.DETACHED_PROCESS,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    subprocess.Popen(  # noqa: S603 - the default browser's own executable
        command,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _app_command(url: str) -> list[str] | None:
    """The command that opens ``url`` as an app window, when the default browser has one."""
    try:
        if sys.platform == "win32":
            return _windows(url)
        if sys.platform == "darwin":
            return _mac(url)
        return _linux(url)
    except OSError, ValueError, subprocess.SubprocessError, plistlib.InvalidFileException:
        return None


def _windows(url: str) -> list[str] | None:
    # Narrowed in the positive branch, which is what tells a type checker winreg exists.
    if sys.platform == "win32":
        import winreg

        # https first; a machine with no choice recorded for it may still have one for http.
        for scheme in ("https", "http"):
            choice = rf"{WINDOWS_ASSOCIATIONS}\{scheme}\UserChoice"
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, choice) as key:
                    prog_id = str(winreg.QueryValueEx(key, "ProgId")[0])
                # What the shell runs, e.g. `"C:\...\chrome.exe" --single-argument %1`.
                opener = winreg.QueryValue(
                    winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command"
                )
            except OSError:
                continue
            if match := WINDOWS_OPENER.match(opener):
                # Some browsers register `%ProgramFiles%\...`, which only the shell expands.
                exe = Path(os.path.expandvars(match.group(1) or match.group(2)))
                if exe.name.lower() in WINDOWS_EXES and exe.exists():
                    return [str(exe), f"--app={url}"]
            return None
    return None


def _mac(url: str) -> list[str] | None:
    preferences = (
        Path.home()
        / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
    )
    # No record at all means nothing was ever chosen: Safari, which has no app windows.
    with preferences.open("rb") as handle:
        handlers = plistlib.load(handle).get("LSHandlers", [])
    for handler in handlers:
        if handler.get("LSHandlerURLScheme") not in ("https", "http"):
            continue
        role = handler.get("LSHandlerRoleAll") or handler.get("LSHandlerRoleViewer") or ""
        bundle = str(role).lower()
        if not bundle:
            continue
        if bundle not in MAC_BUNDLES:
            return None
        # -n: a new copy that hands the window to the running browser, in its profile.
        return ["open", "-n", "-b", bundle, "--args", f"--app={url}"]
    return None


def _linux(url: str) -> list[str] | None:
    desktop = ""
    # xdg-settings is empty on some desktops; the https handler says the same thing.
    for asked in (
        ["xdg-settings", "get", "default-web-browser"],
        ["xdg-mime", "query", "default", "x-scheme-handler/https"],
    ):
        try:
            done = subprocess.run(  # noqa: S603 - a desktop's own tools, fixed arguments
                asked, capture_output=True, text=True, timeout=3, check=False
            )
        except OSError, subprocess.SubprocessError:
            continue
        if done.returncode == 0 and (desktop := done.stdout.strip().lower()):
            break
    for word, commands in LINUX_COMMANDS.items():
        if word in desktop:
            for name in commands:
                if found := shutil.which(name):
                    return [found, f"--app={url}"]
    return None
