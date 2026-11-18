"""Sealing secrets at rest.

Secrets (the BRAIN password, Google API keys) are sealed with AES-256-GCM and stored as
opaque blobs in SQLite. The key lives in a single file created ``0600`` under the data
directory.

Threat model: this protects against casual disclosure — a stray backup, an accidental
`git add`, someone reading the database file — but **not** against an attacker who already
has your user account, since the key is readable by it. That is the deliberate trade for a
tool that must resume polling unattended after a restart without prompting for a passphrase.
"""

import contextlib
import os
import secrets
import sys
import time
from typing import TYPE_CHECKING, Any

import structlog
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger(__name__)

KEY_BYTES = 32  # AES-256
NONCE_BYTES = 12  # GCM standard
#: Windows' ERROR_SHARING_VIOLATION: another program has the key open, as an antivirus scan
#: does for a moment. Retried a few times; every other refusal is final.
SHARING_VIOLATION = 32
READ_ATTEMPTS = 5
READ_RETRY_SECONDS = 0.25


class SealError(RuntimeError):
    """Raised when a secret cannot be sealed or opened."""


def load_or_create_key(path: Path) -> bytes:
    """Return the master key, creating it 0600 on first use.

    Only a key that is not there is created. One that is there but cannot be read fails the
    start instead: ``Path.exists()`` answers False for a file Windows will not let this process
    see, and taking that for a missing key sent the race below round forever, since the link
    refuses to replace the real key.

    The key is written and fsynced to a temporary file, then hard-linked into place. The
    link never overwrites, so ``path`` only ever appears complete, and a process that loses
    a first-start race reads the winner's key instead of keeping its own.
    """
    try:
        key = _read_key(path)
    except FileNotFoundError:
        pass
    else:
        if len(key) != KEY_BYTES:
            raise SealError(
                f"Key at {path} is {len(key)} bytes, expected {KEY_BYTES}. "
                "Refusing to guess. Move it aside to generate a fresh one — "
                "stored secrets will need to be re-entered."
            )
        _tighten_permissions(path)
        return key

    key = secrets.token_bytes(KEY_BYTES)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            # Lost a first-start race: the winner's key is there now, and reading it fails
            # closed, so this cannot come round again.
            return load_or_create_key(path)
    except PermissionError as exc:
        raise SealError(refused(path.parent, exc)) from exc
    finally:
        # A folder that refused the file may refuse this too; the reason above is the news.
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
    # Best effort: Windows cannot open a directory (NTFS journals the link anyway), and
    # some mounts refuse fsync on one. The key is already in place either way.
    if os.name != "nt":
        with contextlib.suppress(OSError):
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    return key


def _read_key(path: Path) -> bytes:
    """The key's bytes. ``FileNotFoundError`` passes through; a sharing violation is retried
    briefly; any other refusal becomes a :class:`SealError` that names the file."""
    for attempt in range(1, READ_ATTEMPTS + 1):
        try:
            return path.read_bytes()
        except PermissionError as exc:
            busy = getattr(exc, "winerror", None) == SHARING_VIOLATION
            if not busy or attempt == READ_ATTEMPTS:
                raise SealError(refused(path, exc)) from exc
            time.sleep(READ_RETRY_SECONDS)
    raise AssertionError("unreachable: the last attempt returns or raises")


def refused(target: Path, exc: PermissionError) -> str:
    """Why ``target`` could not be used, worded for the person starting the app. What decides
    access on Windows is logged beside it, since the error code alone cannot tell a lock from
    a refusal."""
    code = getattr(exc, "winerror", None)
    access = _access(target)
    log.warning("sealing.refused", path=str(target), winerror=code, errno=exc.errno, **access)
    if code == SHARING_VIOLATION:
        return (
            f"Another program has {target} open, such as a second Alpha Harness or an antivirus "
            "scan. Quit Alpha Harness from its tray icon, wait a minute, then start it again."
        )
    because = f" (Windows error {code})" if code is not None else ""
    return (
        f"Alpha Harness is not allowed to use {target}{because}. Check that this account owns "
        "the folder and that no security software blocks Alpha Harness, then start it again. "
        "launcher.log has the details."
    )


def _access(target: Path) -> dict[str, Any]:
    """On Windows: who owns ``target`` and its folder and who may use them, as SDDL, and whether
    this process runs elevated. Best effort; whatever cannot be read is left out."""
    found: dict[str, Any] = {}
    if sys.platform == "win32":
        import ctypes

        with contextlib.suppress(Exception):
            found["elevated"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
        advapi32, kernel32 = ctypes.WinDLL("advapi32"), ctypes.WinDLL("kernel32")
        owner_and_dacl = 0x1 | 0x4  # OWNER_ and DACL_SECURITY_INFORMATION
        for name, place in (("sddl", target), ("folder_sddl", target.parent)):
            with contextlib.suppress(Exception):
                descriptor, text = ctypes.c_void_p(), ctypes.c_wchar_p()
                status = advapi32.GetNamedSecurityInfoW(  # 1: SE_FILE_OBJECT
                    str(place), 1, owner_and_dacl, None, None, None, None, ctypes.byref(descriptor)
                )
                if status:
                    found[name] = f"unreadable (error {status})"
                    continue
                try:
                    if advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                        descriptor, 1, owner_and_dacl, ctypes.byref(text), None
                    ):
                        found[name] = text.value
                finally:
                    kernel32.LocalFree(descriptor)
                    if text.value is not None:
                        kernel32.LocalFree(text)
    return found


def _tighten_permissions(path: Path) -> None:
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        # Tighten rather than merely complain; the user cannot act on a log line.
        path.chmod(0o600)


class Sealer:
    """Seals and opens secrets with a single AES-GCM key."""

    def __init__(self, key_path: Path) -> None:
        self._aead = AESGCM(load_or_create_key(key_path))

    def seal(self, plaintext: str, *, context: str) -> bytes:
        """Encrypt ``plaintext``.

        ``context`` is bound as additional authenticated data, so a blob sealed for one
        purpose cannot be silently substituted into another.
        """
        nonce = secrets.token_bytes(NONCE_BYTES)
        ct = self._aead.encrypt(nonce, plaintext.encode("utf-8"), context.encode("utf-8"))
        return nonce + ct

    def open(self, blob: bytes, *, context: str) -> str:
        """Decrypt a blob produced by :meth:`seal`."""
        if len(blob) <= NONCE_BYTES:
            raise SealError("Ciphertext is too short to contain a nonce")
        nonce, ct = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
        try:
            return self._aead.decrypt(nonce, ct, context.encode("utf-8")).decode("utf-8")
        except InvalidTag as exc:
            raise SealError(
                "Could not decrypt secret: wrong key, wrong context, or corrupted data"
            ) from exc


if __name__ == "__main__":
    import pathlib
    import tempfile

    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as folder:
        root = pathlib.Path(folder)
        made = load_or_create_key(root / "key")
        checks["made once, then read back"] = len(made) == KEY_BYTES and made == load_or_create_key(
            root / "key"
        )
        (root / "short").write_bytes(b"x")
        try:
            load_or_create_key(root / "short")
            checks["a wrong-sized key is refused"] = False
        except SealError:
            checks["a wrong-sized key is refused"] = True

        real_read = pathlib.Path.read_bytes
        attempts = {"n": 0}

        def tried() -> int:
            return attempts["n"]

        def busy_then(limit: int) -> Any:
            def read(self: pathlib.Path) -> bytes:
                attempts["n"] += 1
                if attempts["n"] <= limit:
                    exc = PermissionError(13, "in use")
                    exc.winerror = SHARING_VIOLATION  # pyright: ignore[reportAttributeAccessIssue]
                    raise exc
                return real_read(self)

            return read

        pathlib.Path.read_bytes = busy_then(2)
        checks["a brief lock is waited out"] = load_or_create_key(root / "key") == made
        attempts["n"] = 0
        pathlib.Path.read_bytes = busy_then(READ_ATTEMPTS)
        try:
            load_or_create_key(root / "key")
            checks["a lasting lock fails closed"] = False
        except SealError:
            checks["a lasting lock fails closed"] = tried() == READ_ATTEMPTS
        pathlib.Path.read_bytes = real_read

        if os.name == "posix" and os.geteuid() != 0:  # root reads whatever the mode says
            (root / "key").chmod(0o000)
            before = sorted(p.name for p in root.iterdir())
            try:
                load_or_create_key(root / "key")
                checks["an unreadable key fails closed"] = False
            except SealError:
                after = sorted(p.name for p in root.iterdir())
                checks["an unreadable key fails closed"] = before == after
            (root / "key").chmod(0o600)
            shut = root / "shut"
            shut.mkdir()
            shut.chmod(0o500)
            try:
                load_or_create_key(shut / "key")
                checks["a folder refusing new files fails closed"] = False
            except SealError as exc:
                checks["a folder refusing new files fails closed"] = str(shut) in str(exc)
            shut.chmod(0o700)
    if failed := [name for name, ok in checks.items() if not ok]:
        raise SystemExit(f"sealing self-check failed: {'; '.join(failed)}")
    sys.stdout.write(f"{len(checks)} cases as expected\n")
