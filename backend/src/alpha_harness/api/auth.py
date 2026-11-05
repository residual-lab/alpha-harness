"""Sign-in, session state, and cached platform metadata."""

from typing import Any

import structlog
from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..brain.auth import SessionInfo
from ..brain.settings_schema import resolve_options, validate_settings
from ..realtime import TOPIC_SESSION
from ..schemas import Out
from .deps import State

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """Omit both fields to sign in with the stored credential."""

    email: str | None = Field(default=None, description="Leave empty to use the saved login")
    password: str | None = Field(default=None, repr=False)


class Session(Out):
    authenticated: bool
    user_id: str | None
    full_name: str | None
    permissions: list[str]
    verification_url: str | None
    #: The Persona inquiry a paused sign-in is waiting on, which `/verify` closes.
    inquiry: str | None
    detail: str | None
    can_multi_simulate: bool
    expires_in_seconds: int | None


class SettingsField(Out):
    name: str
    label: str
    type: str | None
    required: bool
    read_only: bool
    #: ``{value, label}`` as BRAIN lists them; null while a parent field is unset.
    choices: list[dict[str, Any]] | None
    depends_on: list[str]
    blocked: bool
    #: Durations such as ``testPeriod`` come as ISO-8601 strings (``P6Y0M0D``).
    min: int | float | str | None
    max: int | float | str | None


class SettingsOptions(Out):
    fields: dict[str, SettingsField]
    problems: list[str]
    missing: list[str]


async def _answer(state: State, info: SessionInfo) -> Session:
    """Settle what a completed sign-in changes, then tell every open tab."""
    if info.authenticated:
        # Batch size follows MULTI_SIMULATION. Startup applies it when it restores a
        # session; a fresh sign-in has to as well.
        state.engine.configure_from_permissions(info.permissions)
        # Available regions and universes depend on the account, so refresh the schema now
        # that permissions are known. Never block sign-in on it, but never fail silently
        # either: a stale schema offers universes BRAIN has withdrawn.
        try:
            await state.metadata.refresh_metadata()
        except Exception:
            log.warning("auth.metadata_refresh_failed", exc_info=True)
    await state.hub.broadcast(TOPIC_SESSION, info.to_dict())
    return Session.model_validate(info.to_dict())


@router.post("/login")
async def login(payload: LoginRequest, state: State) -> Session:
    """Sign in to BRAIN.

    Solves the ALTCHA proof-of-work, exchanges Basic auth for a session cookie, then
    caches the cookie jar so a restart does not repeat the work.
    """
    return await _answer(state, await state.auth.login(payload.email, payload.password))


class VerifyRequest(BaseModel):
    """The Persona inquiry a sign-in was refused with."""

    inquiry: str = Field(description="The inquiry id from the 409 that asked for a check")


@router.post("/verify")
async def verify(payload: VerifyRequest, state: State) -> Session:
    """Finish a sign-in that BRAIN paused for an identity check.

    The check itself happens on BRAIN's own page, which cannot be embedded here — it
    answers ``X-Frame-Options: DENY``, and the Persona widget behind it only frames into
    WorldQuant's own domain. So the browser opens that page in a window of its own and
    asks here whether it has been accepted yet.

    Answering while the check is still open is normal, not an error: the session comes
    back unauthenticated and still carrying the inquiry, and the caller asks again. That
    is what lets the window and this poll finish in either order.
    """
    return await _answer(state, await state.auth.verify(payload.inquiry))


@router.post("/logout")
async def logout(state: State) -> Session:
    await state.auth.logout()
    return await _answer(state, state.auth.session)


class ResolveOptionsRequest(BaseModel):
    """Settings chosen so far. Partial is fine — that is the point."""

    settings: dict[str, Any] = Field(default_factory=dict)


@router.post("/settings-options")
async def settings_options(payload: ResolveOptionsRequest, state: State) -> SettingsOptions:
    """Valid options for every settings field, given what is already chosen.

    ``OPTIONS /simulations`` returns a *recursive* structure: the legal universes depend
    on the region, which depends on the instrument type. Resolving it here means the
    settings form can only ever offer combinations the platform accepts — CHN shows
    ``TOP2000U`` and nothing else.
    """
    schema = await state.metadata.cached_settings_schema()
    if schema is None:
        schema = await state.metadata.refresh_metadata()

    return SettingsOptions.model_validate(
        {
            "fields": resolve_options(schema, payload.settings),
            # Only values actually chosen are judged — a field the user has not reached yet
            # is not an error while the form is being filled in.
            "problems": validate_settings(schema, payload.settings),
            "missing": validate_settings(schema, payload.settings, require_all=True),
        }
    )
