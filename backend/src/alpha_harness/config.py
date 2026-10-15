"""Where the app keeps its files, and the addresses it answers to.

**No BRAIN credentials here.** They are typed into the sign-in screen and sealed in the
local vault, and nothing reads them from the environment: seeding them from a file would
let a checked-out repository sign in as its owner.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# Not a setting: the sign-in screen sends the password here, so no file may redirect it.
BRAIN_API_BASE = "https://api.worldquantbrain.com"

#: An address the app is also served at through a reverse proxy, ``https://harness.example.com``.
#: The app has no login of its own, so whatever answers at that address must ask for one.
PUBLIC_ORIGIN_VARIABLE = "ALPHA_HARNESS_ORIGIN"
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _public_address() -> tuple[frozenset[str], frozenset[str]]:
    """The ``Origin`` and ``Host`` a browser sends for the public address; none when unset."""
    raw = os.environ.get(PUBLIC_ORIGIN_VARIABLE, "").strip()
    if not raw:
        return frozenset(), frozenset()
    # Refused at startup, since a near miss would otherwise answer every request with a bare 400.
    malformed = ValueError(
        f"{PUBLIC_ORIGIN_VARIABLE} must be an address like https://harness.example.com, not {raw!r}"
    )
    try:
        url = urlsplit(raw)
        port = url.port
    except ValueError:
        raise malformed from None
    host = url.hostname
    if (
        url.scheme not in _DEFAULT_PORTS
        or not host
        or url.username is not None
        or url.path.strip("/")
        or url.query
        or url.fragment
    ):
        raise malformed
    # Browsers leave a scheme's own port out of Origin, and the socket compares it exactly.
    netloc = host if port in {None, _DEFAULT_PORTS[url.scheme]} else f"{host}:{port}"
    return frozenset({f"{url.scheme}://{netloc}"}), frozenset({host})


_PUBLIC_ORIGINS, _PUBLIC_HOSTS = _public_address()

#: The pages that may open the telemetry socket: the Vite dev server, the built UI the
#: backend serves itself, and the public address when one is set.
ALLOWED_ORIGINS = (
    frozenset(
        {
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        }
    )
    | _PUBLIC_ORIGINS
)
#: The names a request may address the app by.
ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1"}) | _PUBLIC_HOSTS


#: Home-relative. Must stay on native ext4 — see CLAUDE.md.
DATA_DIR = Path.home().joinpath(".alpha-harness").resolve()


@dataclass(frozen=True, slots=True)
class Settings:
    """Where local state lives."""

    data_dir: Path = DATA_DIR

    @property
    def sqlite_path(self) -> Path:
        """Operational state: credentials, sessions, simulation records, templates."""
        return self.data_dir / "harness.db"

    @property
    def duckdb_path(self) -> Path:
        """Analytical store: the data-field catalog."""
        return self.data_dir / "catalog.duckdb"

    @property
    def key_path(self) -> Path:
        """AES-GCM master key. Created 0600 on first run."""
        return self.data_dir / "key"

    def ensure_data_dir(self) -> None:
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
