"""Typed exceptions for BRAIN API failures.

Business logic should branch on these, never on raw status codes. The API layer maps
them to HTTP responses in exactly one place.
"""

from typing import Any
from urllib.parse import quote, urljoin

from .platform import PLATFORM_SIGN_IN

DAILY_LIMIT_DETAIL = "DAILY_SIMULATION_LIMIT_EXCEEDED"

#: BRAIN's 403 ``detail`` for an inquiry that is unfinished rather than dead: complete it,
#: never replace it.
INQUIRY_INCOMPLETE_DETAIL = "INQUIRY_INCOMPLETE"

#: Where an inquiry is completed and closed.
PERSONA_PATH = "/authentication/persona"


def persona_url(base_url: str, inquiry: str) -> str:
    """The browser address that completes ``inquiry`` — the 401's ``Location``, rebuilt."""
    return urljoin(base_url, f"{PERSONA_PATH}?inquiry={quote(inquiry)}")


#: Sign-in ``detail`` codes and the platform's own wording (``appendix/error-codes.md``).
#: Anything unrecognised is shown as ``DISABLED``, as the platform's client does.
AUTH_DETAIL_MESSAGES = {
    "NO_CREDENTIALS": "Please enter your email address and password.",
    "INVALID_CREDENTIALS": (
        "Incorrect email address or password. To protect your security your account will be "
        "locked if you enter incorrect information too many times."
    ),
    "NOT_VERIFIED": (
        "Your account has not been verified yet. You can resend the verification link if you "
        "missed the email. Contact support in case of problems."
    ),
    "NOT_APPROVED": (
        "Your account has not been approved. Please contact support for further assistance."
    ),
    "INACTIVE": (
        "Your account is locked, due to inactivity. Please contact support for further assistance."
    ),
    "DISABLED": (
        "Your account is locked, it was disabled by support. Please contact support for further "
        "assistance."
    ),
    "LOCKED": "Your account is locked. Please contact support for further assistance.",
    "DEACTIVATED": (
        "Your account has been deactivated. Please contact support for further assistance."
    ),
    "INQUIRY_INCOMPLETE": "Verification incomplete.",
    "RECERTIFICATION_FAILED": (
        "Your account is locked, due to recertification failure. Please contact support for "
        "further assistance."
    ),
    "CONSULTANT_COUNTRY_CHANGE": (
        "Your account is locked, due to change of country / region. Please contact support for "
        "further assistance."
    ),
    "ALPHA_SELLER": (
        "Your account is locked, it is an alpha seller account. Please contact support for "
        "further assistance."
    ),
    "THROTTLED": "Too many sign in attempts. Please try again later.",
    "AGREEMENT_EXPIRED": (
        "Your account is locked, due to agreement expiry. Please contact support for further "
        "assistance."
    ),
    "BIOMETRICS_THROTTLED": (
        "You may have exceeded the number of sign-ins allowed today. Please try again tomorrow "
        "(UTC timezone) to access your account."
    ),
}


def sign_in_message(detail: str | None) -> str:
    return AUTH_DETAIL_MESSAGES.get(detail or "", AUTH_DETAIL_MESSAGES["DISABLED"])


class BrainError(RuntimeError):
    """Base for every BRAIN API failure."""

    #: Whether retrying the identical request could plausibly succeed.
    retryable: bool = False
    #: Whether the platform may have acted on the request even though no usable answer
    #: came back. For ``POST /simulations`` this is what separates "safe to resend" from
    #: "resending may spend the quota twice": BRAIN has no idempotency key.
    maybe_delivered: bool = False

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: Any = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body
        #: The machine-readable ``detail`` code, when the body carried one.
        self.detail = detail


class BrainAuthError(BrainError):
    """401 — the session is absent, expired, or the credentials are wrong."""


class BrainVerificationRequired(BrainAuthError):
    """401 carrying an ``inquiry`` — biometric / ID verification is needed.

    Not a credential failure. The person opens ``url`` in a browser, and the inquiry is then
    closed with ``POST /authentication/persona``.
    """

    def __init__(
        self, message: str, *, inquiry: str, url: str | None = None, body: Any = None
    ) -> None:
        super().__init__(message, status=401, body=body)
        self.inquiry = inquiry
        self.url = url or PLATFORM_SIGN_IN


class BrainForbidden(BrainError):
    """403 — the account lacks permission for this resource or settings combination."""


class BrainNotFound(BrainError):
    """404."""


class BrainValidationError(BrainError):
    """400 — the request was rejected. ``fields`` carries the per-field messages.

    Simulation rejections nest under ``settings`` keyed by the offending field.
    """

    def __init__(self, message: str, *, fields: dict[str, Any], body: Any = None) -> None:
        super().__init__(message, status=400, body=body)
        self.fields = fields


class BrainRateLimited(BrainError):
    """429 — rate limited. Back off and retry; the thresholds are server-side."""

    retryable = True

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message, status=429, body=body)
        self.retry_after = retry_after


class BrainDailyLimitReached(BrainRateLimited):
    """429 with ``DAILY_SIMULATION_LIMIT_EXCEEDED``.

    Deliberately **not** retryable: the quota resets on US-Eastern midnight and nothing
    the client does before then will help. Callers must stop, not back off.
    """

    retryable = False


class BrainServiceUnavailable(BrainError):
    """503 — the simulation service is temporarily down. Retryable."""

    retryable = True


class BrainServerError(BrainError):
    """500/502/504 — retryable.

    The request reached the platform, so it may have been acted on before the error. A
    503 is kept apart: it is BRAIN refusing work ("Simulations are currently unavailable").
    """

    retryable = True
    maybe_delivered = True


class BrainTransportError(BrainError):
    """The request never completed: DNS, TLS, connection reset, timeout.

    ``maybe_delivered`` is False only when the connection was never made; a read timeout
    or a dropped connection leaves the platform free to have run the request.
    """

    retryable = True

    def __init__(self, message: str, *, maybe_delivered: bool) -> None:
        super().__init__(message)
        self.maybe_delivered = maybe_delivered


class BrainPollTimeout(BrainError):
    """An asynchronous job kept returning ``Retry-After`` past our ceiling."""
