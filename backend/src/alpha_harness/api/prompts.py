"""LLM Prompts: the built-in system prompts, and the user's own saved beside them.

Built-ins are read-only; copying one is how it is changed. A saved prompt is edited in
place and every task that uses it reads the new text on its next LLM call.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db.models import SavedPrompt
from ..llm.library import BUILT_IN, PromptNotFoundError, resolve
from ..llm.prompts import PROMPTS
from ..llm.text import estimate_tokens
from ..schemas import Out
from .deps import State, refuse

router = APIRouter(prefix="/api/prompts", tags=["prompts"])

MAX_BODY = 60_000


class PromptKind(Out):
    """One job a prompt can do: a built-in's slug, and where it is chosen."""

    slug: str
    label: str
    purpose: str


class LibraryPrompt(Out):
    #: Null for a built-in, whose ``kind`` is its slug.
    id: int | None
    kind: str
    name: str
    body: str
    built_in: bool
    based_on: str | None
    characters: int
    words: int
    estimated_tokens: int
    updated_at: datetime | None


class PromptLibrary(Out):
    kinds: list[PromptKind]
    prompts: list[LibraryPrompt]


def _out(
    prompt_id: int | None,
    kind: str,
    name: str,
    body: str,
    based_on: str | None,
    at: datetime | None,
) -> LibraryPrompt:
    return LibraryPrompt(
        id=prompt_id,
        kind=kind,
        name=name,
        body=body,
        built_in=prompt_id is None,
        based_on=based_on,
        characters=len(body),
        words=len(body.split()),
        estimated_tokens=estimate_tokens(body),
        updated_at=at,
    )


def _row(r: SavedPrompt) -> LibraryPrompt:
    return _out(r.id, r.kind, r.name, r.body, r.based_on, r.updated_at)


@router.get("")
async def library(state: State) -> PromptLibrary:
    """Every prompt: each built-in first, then the saved ones, newest edit first."""
    async with state.db.session() as session:
        rows = (
            await session.scalars(select(SavedPrompt).order_by(SavedPrompt.updated_at.desc()))
        ).all()
    return PromptLibrary(
        kinds=[PromptKind(slug=p.slug, label=p.label, purpose=p.purpose) for p in PROMPTS],
        prompts=[
            *(_out(None, p.slug, p.label, p.body, None, None) for p in PROMPTS),
            *(_row(r) for r in rows if r.kind in BUILT_IN),
        ],
    )


class NewPrompt(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str
    body: str = Field(default="", max_length=MAX_BODY)
    based_on: str | None = Field(default=None, max_length=160)


@router.post("", status_code=201)
async def create(body: NewPrompt, state: State) -> LibraryPrompt:
    """A new prompt, blank or a copy: a copy sends the text it starts from."""
    if body.kind not in BUILT_IN:
        raise refuse(422, "unknown_prompt_kind", f"There is no {body.kind!r} prompt.")
    row = SavedPrompt(
        name=body.name.strip(), kind=body.kind, body=body.body, based_on=body.based_on
    )
    async with state.db.session() as session:
        session.add(row)
        await session.flush()
        await session.refresh(row)
    return _row(row)


class EditPrompt(BaseModel):
    """Only what is sent changes."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    body: str | None = Field(default=None, max_length=MAX_BODY)


@router.put("/{prompt_id}")
async def edit(prompt_id: int, body: EditPrompt, state: State) -> LibraryPrompt:
    async with state.db.session() as session:
        row = await session.get(SavedPrompt, prompt_id)
        if row is None:
            raise refuse(404, "prompt_not_found", f"Prompt {prompt_id} was deleted.")
        if body.name is not None:
            row.name = body.name.strip()
        if body.body is not None:
            row.body = body.body
        await session.flush()
        await session.refresh(row)
    return _row(row)


@router.delete("/{prompt_id}", status_code=204)
async def remove(prompt_id: int, state: State) -> None:
    """A task already using it keeps the text it had when it was added."""
    async with state.db.session() as session:
        row = await session.get(SavedPrompt, prompt_id)
        if row is not None:
            await session.delete(row)


async def chosen(state: State, kind: str, prompt_id: int | None) -> tuple[str, str]:
    """``(name, body)`` for a request that names a prompt, refused plainly if it is gone."""
    try:
        return await resolve(state.db, kind, prompt_id)
    except PromptNotFoundError as exc:
        raise refuse(422, "prompt_not_found", str(exc)) from exc
