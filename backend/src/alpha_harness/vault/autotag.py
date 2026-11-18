"""Tag the Alphas the labs make on BRAIN, as ACE's notebooks tag theirs, so they can be found
there by tag. Off until Settings turns it on: each tag is one more request to BRAIN.
"""

import asyncio
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from ..brain.errors import BrainError
from ..db.models import SimulationRecord
from .store import json_list

if TYPE_CHECKING:
    from ..brain.endpoints import BrainEndpoints
    from ..db.sqlite import Database
    from .store import AlphaVault

log = structlog.get_logger(__name__)

#: Alphas tagged at once. Each is one PATCH, sent as background work behind the simulations.
AT_ONCE = 2


def tags_for(name: str, task: str | None) -> list[str]:
    """The tag and the lab's own beside it: ``harness`` and ``harness_search``."""
    # A task is "<lab>-<timestamp>"; one sent by hand from the Matrix has no lab.
    lab, _, stamp = (task or "").rpartition("-")
    return [name, f"{name}_{lab.replace('-', '_')}"] if lab and stamp else [name]


class AutoTagger:
    """Adds the tags to each Alpha a simulation lands, beside the ones it already has."""

    def __init__(self, db: Database, vault: AlphaVault, endpoints: BrainEndpoints) -> None:
        self.db = db
        self.vault = vault
        self.endpoints = endpoints
        #: The tag to add, from Settings; ``None`` while auto-tagging is off.
        self.name: str | None = None
        self._gate = asyncio.Semaphore(AT_ONCE)

    async def landed(self, alpha_id: str) -> None:
        """Tag one Alpha that just landed. A failure is logged, never the simulation's."""
        name = self.name
        if not name:
            return
        async with self._gate:
            try:
                await self._tag(alpha_id, name)
            except BrainError as exc:
                log.warning("vault.auto_tag_failed", alpha_id=alpha_id, error=exc.message)

    async def _tag(self, alpha_id: str, name: str) -> None:
        async with self.db.session() as session:
            task = await session.scalar(
                select(SimulationRecord.task).where(SimulationRecord.alpha_id == alpha_id).limit(1)
            )
        wanted = tags_for(name, task)
        # Captured a moment ago, so the vault's copy is current; read from BRAIN otherwise.
        stored = (await self.vault.by_ids([alpha_id])).get(alpha_id)
        if stored is not None:
            had = json_list(stored.get("tags"))
        else:
            body = await self.endpoints.alpha_body(alpha_id)
            had = [t for t in body.get("tags") or [] if isinstance(t, str)]
        missing = [t for t in wanted if t not in had]
        if not missing:
            return
        # Saving replaces the whole list, so the tags it had go back with the new ones.
        answer = await self.endpoints.update_alpha(alpha_id, {"tags": had + missing})
        now = [t for t in answer.get("tags") or [] if isinstance(t, str)] or had + missing
        await self.vault.save_tags({alpha_id: now})
