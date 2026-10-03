"""The assistant: keys and models. The prompts it sends live in :mod:`.prompts`.

Keys are the only secret here, and leave only as a masked hint.
"""

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..llm.keys import serialise
from ..llm.providers import LLMProviders, catalogue
from ..llm.registry import LLMModels, ModelInfo
from ..schemas import Out
from .deps import State

router = APIRouter(prefix="/api/llm", tags=["assistant"])


class LLMKeyUsage(Out):
    key_id: int
    model: str
    #: The model's own day, YYYY-MM-DD in its reset time zone.
    day: str
    requests: int
    tokens: int
    last_request_at: str | None


class LLMKey(Out):
    id: int
    label: str
    provider: str
    hint: str
    enabled: bool
    #: The user's own daily ceiling, or null to use the model's.
    daily_limit: int | None
    last_ok_at: str | None
    last_error: str | None
    created_at: str | None
    usage: list[LLMKeyUsage]


class LLMBudget(Out):
    model: str
    #: How requests name this model: its provider and id together.
    ref: str
    provider: str
    #: Requests today across every enabled key, each held to its model limit or its cap.
    allowed_today: int
    remaining_today: int
    reset_timezone: str
    #: Until midnight in ``reset_timezone``, when this model's allowance comes back.
    reset_in_seconds: int


class LLMKeyStatus(Out):
    keys: list[LLMKey]
    enabled: int
    budget: list[LLMBudget]
    #: The soonest any set-up model's day turns over, or null with none set up.
    reset_in_seconds: int | None


class KeyWorks(Out):
    key_id: int
    ok: Literal[True]
    #: How many models the key can reach.
    models: int


class KeyFailed(Out):
    key_id: int
    ok: Literal[False]
    error: str


def _check(result: dict[str, Any]) -> KeyWorks | KeyFailed:
    if result["ok"]:
        return KeyWorks.model_validate(result)
    return KeyFailed.model_validate(result)


# --- setup ----------------------------------------------------------------


@router.get("/models")
async def models(state: State) -> LLMModels:
    """The models set up, each with the limits its user gave it."""
    return state.llm.registry.roster()


class SetModel(BaseModel):
    provider: str
    model: str = Field(min_length=1, max_length=200, description="The provider's model id")
    requests_per_minute: int = Field(ge=1)
    requests_per_day: int = Field(ge=1)
    reset_timezone: str = Field(
        min_length=1, max_length=64, description="IANA time zone whose midnight starts a new day"
    )
    max_prompt_tokens: int | None = Field(
        default=None,
        ge=1,
        description="Most tokens one Power Pool prompt may use; null for default",
    )


@router.put("/models")
async def set_model(body: SetModel, state: State) -> ModelInfo:
    """Set a model up with its limits, or change the limits of one already set up."""
    return await state.llm.registry.set(
        body.provider,
        body.model,
        body.requests_per_minute,
        body.requests_per_day,
        body.reset_timezone,
        body.max_prompt_tokens,
    )


@router.delete("/models", status_code=204)
async def remove_model(provider: str, model: str, state: State) -> None:
    """Query parameters, not a path: model ids carry slashes."""
    await state.llm.registry.remove(provider, model)


class OfferedModels(Out):
    #: Model ids the provider lists for this account, sorted. Empty when it would not say.
    models: list[str]
    #: Why the list is empty, when it is. The id can still be typed.
    error: str | None


@router.get("/providers/{provider}/models")
async def offered_models(provider: str, state: State) -> OfferedModels:
    """What a provider's Key can reach, asked live. Costs no generation request."""
    return OfferedModels.model_validate(await state.llm.offered(provider))


@router.get("/keys")
async def list_keys(state: State) -> LLMKeyStatus:
    """Keys, today's usage, and how much budget is left across all of them."""
    return LLMKeyStatus.model_validate(await state.llm.keys.status(state.llm.registry))


@router.get("/providers")
async def providers() -> LLMProviders:
    """Every assistant that can answer, and how to get a free key for it.

    All of them are free and need no card — the assistant is optional here, so asking for
    payment details would turn a convenience into a purchase decision.
    """
    return catalogue()


class AddKey(BaseModel):
    key: str = Field(description="An assistant API key. Sealed at rest; never returned.")
    label: str | None = Field(default=None, description="Which account this key belongs to")
    provider: str = Field(default="google", description="Whose key this is")
    daily_limit: int | None = Field(
        default=None,
        ge=1,
        description="Daily request ceiling for this key. Required for a paid provider.",
    )


@router.post("/keys", status_code=201)
async def add_key(body: AddKey, state: State) -> LLMKey:
    """Store a key.

    Quota is per account, so adding a key from a second account genuinely doubles the
    daily budget — which is why the same key cannot be added twice.
    """
    row = await state.llm.keys.add(
        body.key, body.label, provider=body.provider, daily_limit=body.daily_limit
    )
    return LLMKey.model_validate(serialise(row))


@router.post("/keys/{key_id}/check")
async def check_key(key_id: int, state: State) -> KeyWorks | KeyFailed:
    """Confirm a key works. Costs nothing against the generation quota."""
    return _check(await state.llm.check_key(key_id))


@router.post("/keys/check")
async def check_all_keys(state: State) -> list[KeyWorks | KeyFailed]:
    return [_check(await state.llm.check_key(k.id)) for k in await state.llm.keys.list_keys()]


class KeyToggle(BaseModel):
    enabled: bool
    daily_limit: int | None = Field(
        default=None, ge=1, description="Move this key's daily cap. Left alone when omitted."
    )
    #: Explicit rather than a zero sentinel, because omitted already means "leave it".
    clear_daily_limit: bool = Field(
        default=False, description="Remove this key's daily cap, going back to the model's."
    )


@router.put("/keys/{key_id}")
async def toggle_key(key_id: int, body: KeyToggle, state: State) -> LLMKey:
    row = await state.llm.keys.set_enabled(
        key_id, body.enabled, cap=body.daily_limit, clear=body.clear_daily_limit
    )
    return LLMKey.model_validate(serialise(row))


@router.delete("/keys/{key_id}", status_code=204)
async def remove_key(key_id: int, state: State) -> None:
    await state.llm.keys.remove(key_id)
