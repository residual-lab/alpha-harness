"""FastAPI dependencies and the exception -> HTTP mapping.

Every BRAIN failure becomes an HTTP response in exactly one place, so business logic can
raise typed exceptions and never touch ``HTTPException``.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ..account import NoCredentialError
from ..brain.errors import (
    BrainAuthError,
    BrainDailyLimitReached,
    BrainError,
    BrainForbidden,
    BrainNotFound,
    BrainPollTimeout,
    BrainRateLimited,
    BrainServiceUnavailable,
    BrainTransportError,
    BrainValidationError,
    BrainVerificationRequired,
)
from ..labs.objectives import StudyError, StudyNotFoundError
from ..llm.keys import BudgetExhaustedError, LLMError, NoKeysError
from ..state import AppState


def refuse(status: int, code: str, message: str) -> HTTPException:
    """An HTTP error in the ``{code, message}`` shape every screen reads."""
    return HTTPException(status, detail={"code": code, "message": message})


def get_state(request: Request) -> AppState:
    return request.app.state.harness


State = Annotated[AppState, Depends(get_state)]


def _problem(
    status: int,
    code: str,
    message: str,
    **extra: Any,
) -> JSONResponse:
    """A consistent error envelope.

    ``code`` is stable and machine-readable; ``message`` is written for a human who may
    not know what a 429 is.
    """
    return JSONResponse(
        status_code=status,
        content={"detail": {"code": code, "message": message, **extra}},
    )


#: ``exception -> (status, code, message, retryable)``. Anything carrying a payload of its
#: own — which fields were rejected, how long to wait — stays an explicit handler below.
#:
#: ``message`` of ``None`` means the exception already phrases itself for a person.
#: ``retryable`` of ``None`` omits the key, which is not the same as ``False``: the client
#: then falls back to "retryable if 5xx", right for a poll timeout and wrong for a daily limit.
SIMPLE: dict[type[Exception], tuple[int, str, str | None, bool | None]] = {
    BrainAuthError: (
        401,
        "not_authenticated",
        "Not signed in to BRAIN, or the session expired. Sign in again.",
        None,
    ),
    BrainForbidden: (
        403,
        "forbidden",
        "Your BRAIN account is not permitted to do this. It may need a permission your "
        "tier does not have, or the settings combination is not available.",
        None,
    ),
    BrainNotFound: (404, "not_found", "BRAIN has no such resource.", None),
    # Deliberately distinct from an ordinary 429: retrying cannot help today.
    BrainDailyLimitReached: (
        429,
        "daily_limit_reached",
        "You have used your daily simulation quota. It resets at midnight US Eastern "
        "time — retrying before then will not work.",
        False,
    ),
    BrainServiceUnavailable: (
        503,
        "platform_unavailable",
        "The BRAIN simulation service is temporarily unavailable.",
        True,
    ),
    BrainTransportError: (
        502,
        "network_error",
        "Could not reach BRAIN. Check your connection.",
        True,
    ),
    BrainPollTimeout: (
        504,
        "poll_timeout",
        "BRAIN is still working on this. It may finish later — try again shortly.",
        None,
    ),
    NoCredentialError: (400, "no_credential", None, None),
    StudyNotFoundError: (404, "study_not_found", None, None),
    # Understood and refused: the study as configured cannot produce anything.
    StudyError: (422, "study_invalid", None, None),
    NoKeysError: (400, "no_api_key", None, None),
    LLMError: (400, "llm_error", None, None),
}


def _simple_handler(
    status: int, code: str, message: str | None, retryable: bool | None
) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
    async def handler(_r: Request, exc: Exception) -> JSONResponse:
        extra: dict[str, Any] = {}
        if retryable is not None:
            extra["retryable"] = retryable
        if message is not None and isinstance(exc, BrainError):
            extra["detail"] = exc.message
        return _problem(status, code, message or str(exc), **extra)

    return handler


def install_exception_handlers(app: FastAPI) -> None:
    # Starlette picks a handler by walking ``type(exc).__mro__``, so a specific handler
    # still wins over its base — BrainVerificationRequired over BrainAuthError,
    # BrainDailyLimitReached over BrainRateLimited — whatever order they register in.
    for exc_type, (status, code, message, retryable) in SIMPLE.items():
        app.add_exception_handler(exc_type, _simple_handler(status, code, message, retryable))

    @app.exception_handler(BrainVerificationRequired)
    async def _verification(_r: Request, exc: BrainVerificationRequired) -> JSONResponse:
        return _problem(
            409,
            "verification_required",
            "BRAIN needs to verify your identity before this session can be used. "
            "Open the link, complete the check, then sign in again.",
            verificationUrl=exc.url,
            inquiry=exc.inquiry,
        )

    @app.exception_handler(BrainValidationError)
    async def _validation(_r: Request, exc: BrainValidationError) -> JSONResponse:
        return _problem(
            422,
            "rejected_by_platform",
            "BRAIN rejected this request.",
            fields=exc.fields,
        )

    @app.exception_handler(BrainRateLimited)
    async def _rate_limited(_r: Request, exc: BrainRateLimited) -> JSONResponse:
        return _problem(
            429,
            "rate_limited",
            "BRAIN is throttling requests. Wait a moment and try again.",
            retryable=True,
            retryAfter=exc.retry_after,
        )

    @app.exception_handler(BudgetExhaustedError)
    async def _budget(_r: Request, exc: BudgetExhaustedError) -> JSONResponse:
        # A daily exhaustion is not retryable in any useful sense; a per-minute one is, so
        # the client is told which and for how long.
        return _problem(
            429,
            "llm_budget_exhausted",
            str(exc),
            retryable=not exc.daily,
            retryAfter=round(exc.retry_after),
            model=exc.model,
            keys=[s.to_dict() for s in exc.states],
        )

    # Starlette answers anything unhandled with a bare "Internal Server Error", which leaves
    # a bug report with nothing to go on. Naming the error is enough to find it; the
    # traceback still reaches the log, since Starlette re-raises after this responds.
    @app.exception_handler(Exception)
    async def _unexpected(_r: Request, exc: Exception) -> JSONResponse:
        return _problem(
            500,
            "internal_error",
            "Alpha Harness hit an unexpected error.",
            detail=f"{type(exc).__name__}: {exc}"[:500],
        )

    @app.exception_handler(BrainError)
    async def _generic(_r: Request, exc: BrainError) -> JSONResponse:
        return _problem(
            502,
            "platform_error",
            "BRAIN returned an unexpected response.",
            detail=exc.message,
            platformStatus=exc.status,
        )
