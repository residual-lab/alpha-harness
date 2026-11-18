"""The models the user has set up, and the limits they gave each one.

Nothing here is built in. Which models a provider offers and what its free tier allows
change every few weeks, and a table transcribed into the code is wrong by the time anyone
reads it. So the user names the model, picked from what their key lists or typed, and the
requests per minute and per day their provider shows them.

Requests per day is the limit that ends a session: it does not come back until the
provider's day resets, at midnight in the time zone the user names for the model, because
providers do not agree on one. Tokens per minute is not tracked at all, because no count
made here would match the provider's.
"""

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from pydantic import computed_field
from pydantic.dataclasses import dataclass
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert

from ..db.models import LLMModel
from ..schemas import WIRE, Out
from . import providers
from .keys import LLMError

if TYPE_CHECKING:
    from ..db.sqlite import Database

log = structlog.get_logger(__name__)

#: Tokens one Power Pool prompt may spend on fields when its model names no ceiling.
DEFAULT_PROMPT_TOKENS = 40_000


def model_ref(provider: str, model_id: str) -> str:
    """How a request names one set-up model: ids repeat across providers, so both.

    Provider ids never hold a colon; model ids can (``…:free``), so the first one splits.
    """
    return f"{provider}:{model_id}"


@dataclass(frozen=True, slots=True, config=WIRE)
class ModelInfo:
    """One model and the limits its user gave it. Each key gets these limits in full."""

    id: str
    #: Whose key answers for this model. A key only ever serves its own provider.
    provider: str
    rpm: int
    rpd: int
    #: The IANA time zone whose midnight starts this model's new day at its provider.
    reset_timezone: str
    #: The most tokens one Power Pool prompt may spend, or None for the default.
    max_prompt_tokens: int | None = None

    @computed_field
    @property
    def ref(self) -> str:
        return model_ref(self.provider, self.id)

    @computed_field
    @property
    def retired(self) -> str | None:
        """Why this model can no longer answer, or None. Kept so the user can remove it."""
        return providers.get(self.provider).retired or None

    @property
    def prompt_tokens(self) -> int:
        return self.max_prompt_tokens or DEFAULT_PROMPT_TOKENS


class LLMModels(Out):
    models: list[ModelInfo]
    #: What a model without its own prompt ceiling gets.
    default_prompt_tokens: int
    note: str


def _info(row: LLMModel) -> ModelInfo:
    return ModelInfo(
        id=row.model,
        provider=row.provider,
        rpm=row.requests_per_minute,
        rpd=row.requests_per_day,
        reset_timezone=row.reset_timezone,
        max_prompt_tokens=row.max_prompt_tokens,
    )


class ModelRegistry:
    """The set-up models, held in memory and written through to the database."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._models: dict[str, ModelInfo] = {}

    async def load(self) -> None:
        async with self.db.session() as session:
            rows = (await session.scalars(select(LLMModel))).all()
        self._models = {info.ref: info for info in map(_info, rows)}

    # -- reading ---------------------------------------------------------

    def get(self, ref: str) -> ModelInfo | None:
        """A model by its :func:`model_ref`. A bare id, as tasks from before refs still
        hold, is found when only one provider has it."""
        if found := self._models.get(ref):
            return found
        named = [m for m in self._models.values() if m.id == ref]
        return named[0] if len(named) == 1 else None

    def all(self) -> list[ModelInfo]:
        """Every model, richest daily budget first, because that is what runs out."""
        return sorted(self._models.values(), key=lambda m: (-m.rpd, -m.rpm, m.ref))

    def roster(self) -> LLMModels:
        return LLMModels(
            models=self.all(),
            default_prompt_tokens=DEFAULT_PROMPT_TOKENS,
            note=(
                "Requests per day is the limit that ends a session: it does not come back "
                "until the provider's day resets. Limits apply to each Key, so a second "
                "account's Key doubles them."
            ),
        )

    # -- editing ---------------------------------------------------------

    async def set(
        self,
        provider: str,
        model_id: str,
        rpm: int,
        rpd: int,
        reset_timezone: str,
        max_prompt_tokens: int | None = None,
    ) -> ModelInfo:
        """Set a model up, or change the limits of one already set up."""
        model_id = model_id.strip()
        try:
            ZoneInfo(reset_timezone)
        except ZoneInfoNotFoundError, ValueError:
            raise LLMError(f"{reset_timezone!r} is not a time zone this machine knows.") from None
        if provider not in providers.PROVIDERS:
            gone = providers.get(provider)
            raise LLMError(f"{gone.label} cannot take a model. {gone.retired}")
        if not model_id:
            raise LLMError("Name the model: pick one from the list or type its ID.")
        if rpm < 1 or rpd < 1:
            raise LLMError("Both limits have to be at least one request.")
        if max_prompt_tokens is not None and max_prompt_tokens < 1:
            raise LLMError("Max prompt tokens has to be at least one, or left empty.")
        limits = {
            "requests_per_minute": rpm,
            "requests_per_day": rpd,
            "reset_timezone": reset_timezone,
            "max_prompt_tokens": max_prompt_tokens,
        }
        # One statement, so two saves of the same model at once cannot both insert it.
        statement = (
            insert(LLMModel)
            .values(provider=provider, model=model_id, **limits)
            .on_conflict_do_update(index_elements=["provider", "model"], set_=limits)
        )
        async with self.db.session() as session:
            await session.execute(statement)
            await session.commit()
        info = ModelInfo(
            id=model_id,
            provider=provider,
            rpm=rpm,
            rpd=rpd,
            reset_timezone=reset_timezone,
            max_prompt_tokens=max_prompt_tokens,
        )
        self._models[info.ref] = info
        log.info("llm.model.set", model=info.ref, rpm=rpm, rpd=rpd, tz=reset_timezone)
        return info

    async def remove(self, provider: str, model_id: str) -> None:
        ref = model_ref(provider, model_id)
        if ref not in self._models:
            raise LLMError(f"{model_id} is not set up for {providers.get(provider).label}.")
        async with self.db.session() as session:
            await session.execute(
                delete(LLMModel).where(LLMModel.provider == provider, LLMModel.model == model_id)
            )
            await session.commit()
        del self._models[ref]
        log.info("llm.model.removed", model=ref)
