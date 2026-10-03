"""Talking to the assistant.

Where diversity comes from: dropdowns give everyone who picks the same options the same
answer, a sentence in someone's own words does not. What comes back is a list of real data
fields, checked against the catalogue, ready to hand to a lab that runs them.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ..catalog.queries import Tuple4
from ..llm.chat import DEFAULT_REASONING, Reasoning, reasoning_options
from ..llm.registry import LLMModels
from ..schemas import Out
from .deps import State, refuse
from .prompts import chosen

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ReasoningOption(Out):
    value: Reasoning
    level: Literal["MINIMAL", "LOW", "MEDIUM", "HIGH"]
    label: str
    description: str


class ChatOptions(Out):
    models: LLMModels
    reasoning: list[ReasoningOption]
    default_reasoning: Reasoning
    note: str


class ChatThreadSummary(Out):
    id: int
    title: str
    #: ``USA/D1/TOP3000``.
    scope: str
    updated_at: str | None


class ThreadScope(Out):
    instrument_type: str
    region: str
    delay: int
    universe: str


class ChatMessageOut(Out):
    id: int
    role: Literal["user", "assistant"]
    text: str
    #: ``picks``, ``model``, ``reasoning`` and ``tokens`` on an assistant reply.
    meta: dict[str, Any]
    created_at: str | None


class ChatThreadOut(Out):
    id: int
    title: str
    scope: ThreadScope
    messages: list[ChatMessageOut]


class LLMUsage(Out):
    prompt_tokens: int
    output_tokens: int
    thinking_tokens: int
    total_tokens: int


class ChatReply(Out):
    thread_id: int
    reply: str
    #: ``{field, why}`` and whatever else the model said about the field.
    picks: list[dict[str, Any]]
    datasets: list[str]
    #: Field ids the model named that do not exist in the catalogue.
    dropped: list[str]
    catalog_note: str | None
    usage: LLMUsage
    model: str
    reasoning: Reasoning


@router.get("/options")
async def options(state: State) -> ChatOptions:
    """The two choices a conversation offers: which model, and how hard to think."""
    return ChatOptions.model_validate(
        {
            "models": state.llm.registry.roster(),
            "reasoning": reasoning_options(),
            "defaultReasoning": DEFAULT_REASONING,
            "note": (
                "Thinking harder is not free — those tokens come out of the same daily "
                "budget as the answer. Normal is the right choice almost always."
            ),
        }
    )


@router.get("/threads")
async def threads(
    state: State, limit: Annotated[int, Query(ge=1, le=200)] = 30
) -> list[ChatThreadSummary]:
    return [
        ChatThreadSummary.model_validate(
            {
                "id": t.id,
                "title": t.title,
                "scope": f"{t.region}/D{t.delay}/{t.universe}",
                "updatedAt": t.updated_at.isoformat() if t.updated_at else None,
            }
        )
        for t in await state.chat.threads(limit)
    ]


@router.get("/threads/{thread_id}")
async def thread(thread_id: int, state: State) -> ChatThreadOut:
    found = await state.chat.thread(thread_id)
    if found is None:
        raise refuse(404, "no_such_thread", "No such conversation.")
    row, messages = found
    return ChatThreadOut.model_validate(
        {
            "id": row.id,
            "title": row.title,
            "scope": {
                "instrumentType": row.instrument_type,
                "region": row.region,
                "delay": row.delay,
                "universe": row.universe,
            },
            "messages": [
                {
                    "id": m.id,
                    "role": m.role,
                    "text": m.text,
                    "meta": m.meta,
                    "createdAt": m.created_at.isoformat() if m.created_at else None,
                }
                for m in messages
            ],
        }
    )


@router.delete("/threads/{thread_id}", status_code=204)
async def delete_thread(thread_id: int, state: State) -> None:
    await state.chat.delete(thread_id)


class Say(BaseModel):
    """One message. Everything except the text has a sensible default."""

    text: str = Field(description="Your idea, in your own words")
    scope: Tuple4
    thread_id: int | None = Field(default=None, description="Omit to start a new conversation")
    model: str | None = None
    reasoning: Reasoning = DEFAULT_REASONING
    dataset_ids: list[str] = Field(
        default_factory=list, description="Narrow it to particular datasets"
    )
    prompt_id: int | None = Field(
        default=None, description="A saved Assistant prompt from LLM Prompts; omit for the built-in"
    )


@router.post("")
async def say(body: Say, state: State) -> ChatReply:
    """Send a message and get back a reply plus the fields it chose.

    Anything the assistant names that is not really in the catalogue is dropped before
    you see it and reported in ``dropped`` — an invented field costs a simulation to
    discover, and the consultant has a limited number each day.
    """
    _, system = await chosen(state, "assistant", body.prompt_id)
    try:
        answered = await state.chat.say(
            body.thread_id,
            body.text,
            scope=body.scope,
            model=body.model,
            reasoning=body.reasoning,
            dataset_ids=body.dataset_ids,
            system=system,
        )
    except ValueError as exc:
        # A market with nothing downloaded or an unknown conversation: each phrases itself,
        # so say it rather than raising a bare 500.
        raise refuse(400, "cannot_answer", str(exc)) from exc
    return ChatReply.model_validate(answered)
