"""Async HTTP client for the BRAIN API.

Two behaviours in here are the reason this file exists, and both break naive clients:

1. **A response carrying ``Retry-After`` means "not ready yet".** The header's presence —
   not the status code — is the signal.
   :meth:`BrainClient.poll` re-issues the request until it is gone; recordsets,
   correlations and checks are read that way.
2. **``POST /simulations`` answers in headers.** The id is in ``Location``; the caller
   reads it from :class:`BrainResponse`.

Versioning lives in the ``Accept`` header (``application/json;version=N``), not the path.
"""

import asyncio
import math
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx2
import structlog

from .errors import (
    DAILY_LIMIT_DETAIL,
    BrainAuthError,
    BrainBusy,
    BrainDailyLimitReached,
    BrainError,
    BrainForbidden,
    BrainNotFound,
    BrainPollTimeout,
    BrainRateLimited,
    BrainServerError,
    BrainServiceUnavailable,
    BrainTransportError,
    BrainValidationError,
    BrainVerificationRequired,
)

log = structlog.get_logger(__name__)

Method = Literal["GET", "POST", "PATCH", "DELETE", "OPTIONS"]

DEFAULT_VERSION = "2.0"

#: Seconds a request may take. ``read_timeout`` stretches the read for the few slow endpoints.
TIMEOUT = 30.0
#: Retry backoff when the server names no wait: doubling from the first, capped at the second.
BASE_BACKOFF = 2.0
MAX_BACKOFF = 60.0
#: Attempts for retryable failures (429, 503, transport errors).
ATTEMPTS = 6
#: Ceiling on one poll loop, so a stuck server-side job cannot hang a request forever.
#: Simulations are polled by the background tracker, not here.
POLL_TIMEOUT = 300.0

#: Shortest wait between polls of a pending job, whatever ``Retry-After`` says.
MIN_POLL_DELAY = 0.25

#: A reset this large is a wall-clock instant, not a countdown.
EPOCH_SECONDS = 1e9
#: Longest gap a response is allowed to talk us into. Not a rate limit — a bound on how
#: badly a misread header may stall a sync.
MAX_MEASURED_GAP = 60.0
#: Longest a paced caller sleeps before looking again, so a shorter gap measured meanwhile
#: takes effect within a second.
PACE_RECHECK = 1.0


def _header_num(headers: httpx2.Headers, name: str) -> float | None:
    raw = headers.get(name)
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class RateLimit:
    """Daily simulation quota from the ``x-ratelimit-*`` headers of ``POST /simulations``.

    Undocumented, but sent on every send: limit, remaining, and reset in seconds.
    """

    limit: int | None
    remaining: int | None
    reset_seconds: float | None
    observed_at: float

    @classmethod
    def from_headers(cls, headers: httpx2.Headers) -> RateLimit | None:
        limit, remaining, reset = (
            _header_num(headers, f"x-ratelimit-{name}") for name in ("limit", "remaining", "reset")
        )
        if limit is None and remaining is None and reset is None:
            return None
        return cls(
            limit=None if limit is None else int(limit),
            remaining=None if remaining is None else int(remaining),
            reset_seconds=reset,
            observed_at=time.time(),
        )


@dataclass(slots=True)
class BrainResponse:
    """A completed BRAIN response with the bits callers actually need."""

    status: int
    body: Any
    retry_after: float | None
    rate_limit: RateLimit | None
    location: str | None

    @property
    def pending(self) -> bool:
        """True while the server is still working on an asynchronous job."""
        return self.retry_after is not None


def _parse_retry_after(headers: httpx2.Headers) -> float | None:
    """Seconds to wait, or ``None`` when the header is absent and the result is ready.

    Exactly the documented parser: numeric seconds, or an HTTP-date; zero, negative, a past
    date or anything unreadable means poll now.
    """
    raw = headers.get("retry-after")
    if raw is None:
        return None
    raw = raw.strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(raw) - datetime.now(UTC)).total_seconds()
        except TypeError, ValueError:
            return 0.0
    if not math.isfinite(seconds):
        return 0.0
    return max(seconds, 0.0)


def _busy(bucket: str) -> BrainBusy:
    return BrainBusy(f"BRAIN was not asked: its /{bucket} rate limit is in use by a download")


class BrainClient:
    """Cookie-authenticated async client.

    The instance owns a cookie jar; :meth:`export_cookies` / :meth:`load_cookies` persist
    it so a restart does not cost another proof-of-work solve.
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        #: Seconds between sends per endpoint, learned from its own rate-limit headers, and
        #: the next free slot in each. Measured: BRAIN meters each endpoint separately, and
        #: asking faster than it allows buys 429s rather than throughput.
        self._gap: dict[str, float] = {}
        self._last_send: dict[str, float] = {}
        #: Set once an endpoint's first response has reported its window, so the callers
        #: behind it pace on measurements rather than on an assumption.
        self._measured: dict[str, asyncio.Event] = {}
        self._pace_lock = asyncio.Lock()
        #: Monotonic time per endpoint before which nothing is sent to it: set by a ``429`` so
        #: its callers back off together instead of each retrying into a server that said
        #: stop. Per endpoint, so a throttled catalog crawl never holds up a simulation poll.
        self._resume_at: dict[str, float] = {}
        self._client = httpx2.AsyncClient(
            base_url=self.base_url,
            timeout=httpx2.Timeout(TIMEOUT, connect=10.0),
            follow_redirects=False,
            headers={"User-Agent": "alpha-harness/0.1 (local research studio)"},
        )

    # -- lifecycle -------------------------------------------------------

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- cookie jar ------------------------------------------------------

    def export_cookies(self) -> list[dict[str, Any]]:
        """Serialise the jar for storage."""
        return [
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "expires": c.expires,
                "secure": c.secure,
            }
            for c in self._client.cookies.jar
        ]

    def load_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """Restore a jar produced by :meth:`export_cookies`."""
        for c in cookies:
            if not c.get("name") or c.get("value") is None:
                continue
            self._client.cookies.set(
                c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/")
            )

    def clear_cookies(self) -> None:
        self._client.cookies.clear()

    # -- core request ----------------------------------------------------

    @staticmethod
    def _bucket(path: str) -> str:
        """The window a path is metered under: ``/simulations/{id}`` shares ``simulations``.

        Measured: an Alpha and its recordsets share one window, but its correlations have
        their own, much tighter one, and its check reports none.
        """
        parts = path.strip("/").split("/")
        if parts[0] == "alphas" and len(parts) > 2 and parts[2] in ("correlations", "check"):
            return f"alphas/{parts[2]}"
        return parts[0]

    def _pause(self, bucket: str, seconds: float) -> None:
        self._resume_at[bucket] = max(self._resume_at.get(bucket, 0.0), time.monotonic() + seconds)

    async def _reserve(self, bucket: str, wait: float | None = None) -> None:
        """Hold the caller until this endpoint's next free slot, or raise :class:`BrainBusy`
        when that is more than ``wait`` seconds off.

        Nothing about the rate is written here. The first caller to reach an unmeasured
        endpoint goes straight through and every other waits for what its headers report,
        rather than guessing a number that would be wrong for someone else's account.
        """
        async with self._pace_lock:
            measured = self._measured.get(bucket)
            if measured is None:
                self._measured[bucket] = asyncio.Event()
                self._last_send[bucket] = time.monotonic()
                return
        if not measured.is_set():
            try:
                async with asyncio.timeout(wait):
                    await measured.wait()
            except TimeoutError:
                raise _busy(bucket) from None

        # The next send is due a gap after the last, re-read on every wake rather than booked
        # ahead: a queue booked while a window ran dry would keep its minute-wide spacing long
        # after the window reset and a fresh response measured a gap of a fraction of a second.
        while True:
            async with self._pace_lock:
                now = time.monotonic()
                due = self._last_send.get(bucket, 0.0) + self._gap.get(bucket, 0.0)
                if now >= due:
                    self._last_send[bucket] = now
                    return
            if wait is not None and due - now > wait:
                raise _busy(bucket)
            await asyncio.sleep(min(due - now, PACE_RECHECK))

    def _measure(self, bucket: str, headers: httpx2.Headers) -> None:
        """Read this endpoint's window off the response: how many, and how long until reset."""
        limit = _header_num(headers, "ratelimit-limit")
        left = _header_num(headers, "ratelimit-remaining")
        reset = _header_num(headers, "ratelimit-reset")
        if reset is not None and reset > EPOCH_SECONDS:
            # Some gateways report the moment the window rolls over rather than how long
            # until it does. Taken literally that is a sleep measured in decades.
            reset = max(0.0, reset - time.time())
        if left is None:
            left = limit
        if limit and limit > 0 and left is not None and reset and reset > 0:
            # What is left of the window over what is left of its time, pacing starts rather
            # than finishes. ``reset / limit`` speeds up as the window runs down, whatever was
            # spent: BRAIN's hour of 2,000 then runs dry with a third of it to go. Capped, not
            # dropped: past the cap the window is all but spent, and unpaced only draws the 429
            # sooner. ``max``: a one-slot window reports none left after every request.
            self._gap[bucket] = min(reset / max(left, 1.0), MAX_MEASURED_GAP)
        else:
            # Not metered this way, or a window we cannot make sense of. Left unpaced and
            # left to the Retry-After gate, which is safer than obeying a nonsense number.
            self._gap[bucket] = 0.0

    async def request(
        self,
        method: Method,
        path: str,
        *,
        version: str = DEFAULT_VERSION,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        auth: tuple[str, str] | None = None,
        raise_for_status: bool = True,
        raw: bool = False,
        read_timeout: float | None = None,
        patience: float | None = None,
    ) -> BrainResponse:
        """Issue one request and translate failures into typed exceptions.

        ``raw`` leaves a successful body as undecoded ``bytes``, for payloads whose caller
        has a faster decoder than the standard library's. ``read_timeout`` overrides the client's
        for one call, since a few endpoints legitimately take longer than the rest.
        ``patience`` caps how long it queues for the rate limit before :class:`BrainBusy`,
        for a caller with something else to show; once sent, it is never cut short.

        ``params`` values that are ``None`` are dropped.
        """
        clean_params = {k: v for k, v in params.items() if v is not None} if params else None

        bucket = self._bucket(path)
        # Re-checked after each sleep: another 429 may have pushed the pause further out.
        # Jittered so every waiter does not re-send in the same tick and draw another 429.
        while (wait := self._resume_at.get(bucket, 0.0) - time.monotonic()) > 0:
            if patience is not None and wait > patience:
                raise _busy(bucket)
            await asyncio.sleep(wait + random.uniform(0.05, 0.25))

        await self._reserve(bucket, patience)

        try:
            response = await self._client.request(
                method,
                path.lstrip("/"),
                params=clean_params,
                json=json_body,
                headers={"Accept": f"application/json;version={version}"},
                auth=auth or httpx2.USE_CLIENT_DEFAULT,
                # Only the read is stretched: passing read_timeout as the default would
                # make the pool wait that long for a free connection too.
                timeout=httpx2.Timeout(TIMEOUT, connect=10.0, read=read_timeout)
                if read_timeout is not None
                else httpx2.USE_CLIENT_DEFAULT,
            )
            # Before the finally below releases anyone waiting on this endpoint.
            self._measure(bucket, response.headers)
        # Not connected, or no pooled connection free: the request never left.
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout) as exc:
            raise BrainTransportError(
                f"{method} {path} could not connect: {exc}", maybe_delivered=False
            ) from exc
        except httpx2.TimeoutException as exc:
            raise BrainTransportError(f"{method} {path} timed out", maybe_delivered=True) from exc
        except httpx2.HTTPError as exc:
            raise BrainTransportError(
                f"{method} {path} failed: {exc}", maybe_delivered=True
            ) from exc
        finally:
            # Even a failed probe must release its endpoint, or every caller behind it waits
            # on a measurement that is never coming.
            self._measured[bucket].set()

        result = BrainResponse(
            status=response.status_code,
            # A refusal is always decoded, so error handling still reads its body.
            body=response.content if raw and response.status_code < 400 else _decode(response),
            retry_after=_parse_retry_after(response.headers),
            rate_limit=RateLimit.from_headers(response.headers),
            location=response.headers.get("location"),
        )
        if result.status == 429 and result.retry_after:
            # The server named its own wait: nobody sends to this endpoint before it is over.
            # Capped: every request waits on this, polls included, and a day-long Retry-After
            # would stop finished alphas being read.
            self._pause(bucket, min(result.retry_after, MAX_MEASURED_GAP))

        if raise_for_status and response.status_code >= 400:
            raise self.to_error(method, path, result)
        return result

    def _persona_challenge(self, r: BrainResponse) -> tuple[str | None, str | None]:
        """The Persona inquiry in a 401, and the URL to open to complete it.

        A biometric step-up arrives as ``WWW-Authenticate: persona`` with the inquiry in a
        relative ``Location``; some responses put it in the body instead
        (``02-authentication.md``).
        """
        location = r.location
        if location:
            inquiry = parse_qs(urlsplit(location).query).get("inquiry", [""])[0]
            if inquiry:
                return inquiry, urljoin(self.base_url, location)

        inquiry = r.body.get("inquiry") if isinstance(r.body, dict) else None
        if isinstance(inquiry, str) and inquiry:
            return inquiry, None
        return None, None

    def to_error(self, method: str, path: str, r: BrainResponse) -> BrainError:
        """Map a failed response to a typed error."""
        where = f"{method} {path}"
        body = r.body
        detail = body.get("detail") if isinstance(body, dict) else None

        if r.status == 401:
            inquiry, inquiry_url = self._persona_challenge(r)
            if inquiry:
                # Biometric step-up, not a credential failure (02-authentication.md).
                return BrainVerificationRequired(
                    "BRAIN requires identity verification before this session can be used.",
                    inquiry=inquiry,
                    url=inquiry_url,
                    body=body,
                )
            return BrainAuthError(
                f"{where}: not authenticated ({detail or 'session expired'})",
                status=401,
                body=body,
                detail=detail if isinstance(detail, str) else None,
            )

        if r.status == 403:
            return BrainForbidden(
                f"{where}: forbidden ({detail or 'account lacks permission for this request'})",
                status=403,
                body=body,
                detail=detail if isinstance(detail, str) else None,
            )

        if r.status == 404:
            return BrainNotFound(f"{where}: not found", status=404, body=body)

        if r.status == 400:
            fields = body if isinstance(body, dict) else {"detail": body}
            return BrainValidationError(
                f"{where}: rejected by the platform", fields=fields, body=body
            )

        if r.status == 429:
            if detail == DAILY_LIMIT_DETAIL:
                return BrainDailyLimitReached(
                    "Daily simulation limit reached. Please try tomorrow (EST time zone).",
                    body=body,
                )
            return BrainRateLimited(
                f"{where}: rate limited ({detail or 'too many requests'})",
                retry_after=r.retry_after,
                body=body,
            )

        if r.status == 503:
            return BrainServiceUnavailable(f"{where}: service unavailable", status=503, body=body)

        if r.status >= 500:
            return BrainServerError(f"{where}: server error {r.status}", status=r.status, body=body)

        return BrainError(f"{where}: unexpected status {r.status}", status=r.status, body=body)

    async def request_retrying(
        self,
        method: Method,
        path: str,
        **kwargs: Any,
    ) -> BrainResponse:
        """Issue a request, retrying ``429``, ``503``, other ``5xx`` and transport errors.

        Waits the server's ``Retry-After`` when it sends one, otherwise exponential backoff
        with jitter. The thresholds behind a ``429`` are server-side, so nothing here
        guesses a request rate: a ``429`` pauses every caller of that endpoint for the wait.
        Never retries the daily cap.
        """
        for attempt in range(1, ATTEMPTS):
            try:
                return await self.request(method, path, **kwargs)
            except BrainError as exc:
                if not exc.retryable:
                    raise

                delay = getattr(exc, "retry_after", None)
                if delay is None:
                    delay = BASE_BACKOFF * (2 ** (attempt - 1))
                delay = min(float(delay), MAX_BACKOFF)
                # Jitter so parallel callers do not resynchronise on the same instant.
                delay += random.uniform(0, max(delay, 1.0) * 0.25)
                if isinstance(exc, BrainRateLimited):
                    self._pause(self._bucket(path), delay)

                params = kwargs.get("params") or {}
                # Which scope is being throttled: without it a slow market and a starved one
                # look identical in the log.
                scope = "/".join(
                    str(params[k]) for k in ("region", "delay", "universe") if k in params
                )
                log.warning(
                    "brain.retrying",
                    path=path,
                    scope=scope or None,
                    attempt=attempt,
                    of=ATTEMPTS,
                    delay=round(delay, 2),
                    reason=type(exc).__name__,
                )
                await asyncio.sleep(delay)
        # The last attempt's failure is the caller's to handle.
        return await self.request(method, path, **kwargs)

    # -- the asynchronous job protocol -----------------------------------

    async def poll(self, path: str, *, patience: float | None = None) -> BrainResponse:
        """Drive an asynchronous ``GET`` job to completion.

        Re-issues the request while the response carries ``Retry-After``, waiting what the
        server asks. Returns the first response *without* that header. ``patience`` is
        :meth:`request`'s, for the first send only: a job BRAIN has taken is seen through.
        """
        deadline = time.monotonic() + POLL_TIMEOUT
        attempt = 0

        while True:
            # Retrying: a 429 or 503 between polls says nothing about the job.
            response = await self.request_retrying(
                "GET", path, patience=patience if attempt == 0 else None
            )
            attempt += 1

            if not response.pending:
                return response

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrainPollTimeout(
                    f"{path} still pending after {attempt} polls; giving up. "
                    "The job may still complete server-side."
                )

            # The documented "poll now" (zero, past or unreadable) still waits a floor:
            # otherwise a job that stays pending re-requests as fast as the round trip.
            delay = min(max(response.retry_after or 0.0, MIN_POLL_DELAY), remaining)
            log.debug("brain.poll.waiting", path=path, attempt=attempt, delay=delay)
            await asyncio.sleep(delay)


def _decode(response: httpx2.Response) -> Any:
    """Best-effort body decode. Some endpoints return empty bodies or non-JSON."""
    if not response.content:
        return None
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type:
        return response.text
    try:
        return response.json()
    except ValueError:
        return response.text
