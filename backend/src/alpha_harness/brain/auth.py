"""Sign-in and session lifecycle.

Sessions persist on the platform, so the expensive part — solving the ALTCHA
proof-of-work — should happen rarely. The flow is always:

    restore the cookie jar -> GET /authentication -> still valid? done.
                                                  -> expired? solve captcha, sign in.

The captcha is required on sign-in, and repeated failures lock the account, so sign-in
is never attempted speculatively.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from ..schemas import camel_dict
from .altcha import solve
from .errors import (
    INQUIRY_INCOMPLETE_DETAIL,
    PERSONA_PATH,
    BrainError,
    BrainVerificationRequired,
    persona_url,
    sign_in_message,
)
from .schemas import AuthState

if TYPE_CHECKING:
    from .endpoints import BrainEndpoints

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class SessionInfo:
    """What the UI needs to render the login tab."""

    authenticated: bool
    user_id: str | None = None
    full_name: str | None = None
    permissions: list[str] = field(default_factory=list)
    expires_at: float | None = None
    verification_url: str | None = None
    #: The Persona inquiry of a pending verification; closed by :meth:`Authenticator.verify`.
    inquiry: str | None = None
    detail: str | None = None
    #: BRAIN refused the login itself (wrong password, throttled, locked), so trying the same
    #: one again can only spend the lockout budget. A rejected captcha is not a refusal.
    refused: bool = False

    @property
    def can_multi_simulate(self) -> bool:
        return "MULTI_SIMULATION" in self.permissions

    @property
    def expires_in_seconds(self) -> int | None:
        if self.expires_at is None:
            return None
        return max(0, round(self.expires_at - time.time()))

    def to_dict(self) -> dict[str, Any]:
        # The countdown, not the moment: the page's clock need not agree with this one.
        return camel_dict(self, exclude=("refused", "expires_at")) | {
            "canMultiSimulate": self.can_multi_simulate,
            "expiresInSeconds": self.expires_in_seconds,
        }

    @classmethod
    def anonymous(cls, detail: str | None = None, *, refused: bool = False) -> SessionInfo:
        return cls(authenticated=False, detail=detail, refused=refused)

    @classmethod
    def from_state(cls, state: AuthState) -> SessionInfo:
        expires_at = None
        if state.token and state.token.expiry:
            expires_at = time.time() + float(state.token.expiry)
        return cls(
            authenticated=state.user_id is not None,
            user_id=state.user_id,
            permissions=list(state.permissions),
            expires_at=expires_at,
        )


class Authenticator:
    """Establishes and validates a BRAIN session on one :class:`BrainEndpoints`."""

    def __init__(self, endpoints: BrainEndpoints) -> None:
        self.endpoints = endpoints

    async def restore(self, cookies: list[dict[str, Any]] | None) -> SessionInfo | None:
        """Try to reuse a cached cookie jar.

        Returns ``None`` when there is nothing to restore or the session has lapsed, so
        the caller can fall through to a full sign-in.
        """
        if not cookies:
            return None

        self.endpoints.client.load_cookies(cookies)
        try:
            state = await self.endpoints.get_auth()
        except BrainVerificationRequired as exc:
            # The session exists but needs a browser check. Keep the cookies: clearing them
            # here would force a full sign-in after the user had already verified.
            return _needs_verification(exc)
        if state is None or state.user_id is None:
            log.info("brain.session.expired")
            self.endpoints.client.clear_cookies()
            return None

        log.info("brain.session.restored", user_id=state.user_id)
        return SessionInfo.from_state(state)

    async def login(self, email: str, password: str) -> SessionInfo:
        """Full sign-in: solve the proof-of-work, then exchange Basic auth for a cookie."""
        started = time.monotonic()
        solution = await asyncio.to_thread(solve, await self.endpoints.get_captcha())
        log.info(
            "brain.captcha.solved",
            number=solution.number,
            took_ms=solution.took_ms,
        )

        try:
            state = await self.endpoints.authenticate(email, password, captcha=solution.encode())
        except BrainVerificationRequired as exc:
            # Not a credential failure. The user must finish this in a browser.
            log.warning("brain.auth.verification_required", inquiry=exc.inquiry)
            return _needs_verification(exc)
        except BrainError as exc:
            # 400/401/403/429 carry ``detail`` (or ``captcha``); show the platform's words.
            body = exc.body if isinstance(exc.body, dict) else {}
            detail = body.get("detail")
            if exc.status not in (400, 401, 403, 429) or not (detail or body.get("captcha")):
                raise
            log.warning("brain.auth.failed", status=exc.status, detail=detail)
            message = (
                "BRAIN rejected the sign-in captcha. Please try again."
                if not detail
                else sign_in_message(detail if isinstance(detail, str) else None)
            )
            return SessionInfo.anonymous(detail=message, refused=bool(detail))

        if state.user_id is None:
            return SessionInfo.anonymous(detail="BRAIN accepted the request but returned no user.")

        log.info(
            "brain.auth.ok",
            user_id=state.user_id,
            permissions=state.permissions,
            elapsed_s=round(time.monotonic() - started, 2),
        )
        return SessionInfo.from_state(state)

    async def verify(self, inquiry: str) -> SessionInfo | None:
        """Close a completed identity verification: ``POST /authentication/persona``.

        A POST back to the 401's ``Location``: the inquiry as a query parameter, no body and
        no captcha. ``None`` means the platform did not accept it and the caller should fall
        back to a full sign-in.
        """
        try:
            r = await self.endpoints.client.request(
                "POST",
                PERSONA_PATH,
                params={"inquiry": inquiry},
            )
        except BrainVerificationRequired as exc:
            log.info("brain.auth.verification_pending", inquiry=exc.inquiry)
            return _needs_verification(exc)
        except BrainError as exc:
            if exc.detail == INQUIRY_INCOMPLETE_DETAIL:
                # A full sign-in would mint a replacement inquiry and strand the one the
                # person is part-way through in their browser.
                log.info("brain.auth.verification_unfinished", inquiry=inquiry)
                return _verification_pending(
                    inquiry,
                    persona_url(self.endpoints.client.base_url, inquiry),
                    detail=(
                        "BRAIN has not recorded this identity check as finished. Complete it "
                        "at the verification link, then sign in here again."
                    ),
                )
            # Without the detail, the fall-through to a full sign-in reads as an
            # unexplained sign-in loop.
            log.warning(
                "brain.auth.verification_lost",
                status=exc.status,
                inquiry=inquiry,
                detail=exc.detail,
                body=exc.body,
            )
            return None
        state = AuthState.model_validate(r.body or {})
        if state.user_id is None:
            return None
        log.info("brain.auth.verified", user_id=state.user_id)
        return SessionInfo.from_state(state)

    async def status(self) -> SessionInfo:
        """Current session state, without attempting to sign in."""
        try:
            state = await self.endpoints.get_auth()
        except BrainVerificationRequired as exc:
            return _needs_verification(exc)
        if state is None or state.user_id is None:
            return SessionInfo.anonymous()
        return SessionInfo.from_state(state)


def _verification_pending(inquiry: str, url: str, detail: str | None = None) -> SessionInfo:
    """An unauthenticated session that is waiting on one specific Persona inquiry."""
    return SessionInfo(
        authenticated=False,
        verification_url=url,
        inquiry=inquiry,
        detail=detail
        or (
            "BRAIN requires identity verification. Complete it on the BRAIN website, "
            "then sign in here again."
        ),
    )


def _needs_verification(exc: BrainVerificationRequired) -> SessionInfo:
    return _verification_pending(exc.inquiry, exc.url)
