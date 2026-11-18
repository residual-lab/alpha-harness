"""What a person sends when they ask for help: the log folder, and a scrubbed export of it."""

import asyncio

from fastapi import APIRouter

from .. import diagnostics, updates
from ..schemas import Out
from .deps import refuse

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])


class Exported(Out):
    path: str


@router.post("/export")
async def export() -> Exported:
    """Save the version, the system and the end of the log, scrubbed, as a zip in Downloads."""
    return Exported(path=str(await asyncio.to_thread(diagnostics.export)))


@router.post("/open-logs", status_code=204)
async def open_logs() -> None:
    """Show the folder the launcher keeps its log in."""
    folder = updates.launcher_home()
    if folder is None:
        raise refuse(404, "no_log_folder", "Run this way, the log is the terminal it runs in.")
    await asyncio.to_thread(diagnostics.open_folder, folder)
