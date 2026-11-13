"""Where the app keeps its files, and the addresses it answers to.

**No BRAIN credentials here.** They are typed into the sign-in screen and sealed in the
local vault, and nothing reads them from the environment: seeding them from a file would
let a checked-out repository sign in as its owner.
"""

import os
from dataclasses import dataclass
from pathlib import Path

#: The pages besides the backend's own that may open the telemetry socket: the Vite dev server.
#: The built UI is accepted as the backend's own page on whatever port it serves (``api/ws.py``).
#: 8000 is not listed: when the app has moved off it, another program holds it.
ALLOWED_ORIGINS = frozenset({"http://localhost:5173", "http://127.0.0.1:5173"})


#: Home-relative. Must stay on native ext4 — see CLAUDE.md.
DATA_DIR = Path.home().joinpath(".alpha-harness").resolve()
#: Owner-only on POSIX. Not on Windows: Python 3.13+ turns 0o700 into an access list of its
#: own that stops the folder inheriting the profile's, and the profile's already admits only
#: this account, SYSTEM and Administrators.
PRIVATE_DIR_MODE = 0o777 if os.name == "nt" else 0o700


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
        self.data_dir.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
