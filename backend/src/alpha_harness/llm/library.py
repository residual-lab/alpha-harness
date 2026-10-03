"""The prompt library: the built-ins plus the prompts the user saved beside them.

A saved prompt is read afresh every time it is used, so an edit reaches the next LLM call
of a task that is already running.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..db.models import SavedPrompt
from .prompts import PROMPTS, Prompt

if TYPE_CHECKING:
    from ..db.sqlite import Database

BUILT_IN = {p.slug: p for p in PROMPTS}


class PromptNotFoundError(ValueError):
    """A saved prompt that is gone, or that belongs to another job."""


def built_in(kind: str) -> Prompt:
    return BUILT_IN[kind]


async def saved(db: Database, prompt_id: int, kind: str) -> SavedPrompt:
    async with db.session() as session:
        row = await session.get(SavedPrompt, prompt_id)
    if row is None:
        raise PromptNotFoundError(f"Prompt {prompt_id} was deleted. Choose another.")
    if row.kind != kind:
        raise PromptNotFoundError(
            f"“{row.name}” is for {BUILT_IN[row.kind].label}, not {BUILT_IN[kind].label}."
        )
    return row


async def resolve(db: Database, kind: str, prompt_id: int | None) -> tuple[str, str]:
    """``(name, body)`` of the prompt to send: the built-in when ``prompt_id`` is None."""
    if prompt_id is None:
        p = built_in(kind)
        return p.label, p.body
    row = await saved(db, prompt_id, kind)
    return row.name, row.body
