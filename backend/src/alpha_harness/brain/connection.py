"""Whether BRAIN can be asked anything right now: one answer every background caller shares.

Before it, the simulation tracker, vault capture, Production Correlation, the settings-schema
refresh and sending each met a dead session or a dropped network on their own, every few
seconds: 29,000 polls of a dead session in two consultants' logs, and 36,000 of a network that
was not there. Now the first failure moves this state, background callers stand down, and one
probe (``AppState._watch_session``) brings it back.

Only background callers are held back. A request the person makes still goes through, so a
wrong state can never lock them out, and its answer corrects the state either way. Submission
limits (BRAIN busy with simulations started elsewhere, the daily cap) are not states here: they
stop new simulations only, and the engine keeps them (``engine.slots``).
"""

import time
from contextvars import ContextVar
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: Set by the work that runs on its own (the tracker, engine and optimizer loops, vault capture,
#: detached jobs): only its requests are held back while BRAIN cannot answer.
BACKGROUND: ContextVar[bool] = ContextVar("brain_background", default=False)


def background() -> None:
    """Mark the running task's requests as background ones. Called first thing in each loop."""
    BACKGROUND.set(True)


class Link(StrEnum):
    CONNECTED = "CONNECTED"
    #: DNS or connect failures: this computer cannot reach BRAIN.
    OFFLINE = "OFFLINE"
    #: BRAIN wants an identity check only the person can finish.
    VERIFYING = "VERIFYING"
    #: That check expired unfinished: a new one has to be started.
    VERIFYING_EXPIRED = "VERIFYING_EXPIRED"
    #: No usable session, and no check pending.
    SIGNED_OUT = "SIGNED_OUT"


#: First path segments a closed connection still lets through: signing in, and the probe.
_ALWAYS = frozenset({"authentication", "captcha"})


class Connection:
    """The shared state, moved by the client as answers come back."""

    def __init__(self) -> None:
        self.state = Link.CONNECTED
        self.since = time.time()
        self.detail: str | None = None
        #: The pending identity check, while :attr:`state` is ``VERIFYING``.
        self.verification_url: str | None = None
        self.inquiry: str | None = None

    @property
    def open(self) -> bool:
        return self.state == Link.CONNECTED

    def holds(self, path: str) -> bool:
        """Whether a request to ``path`` is held back: a background one, while not connected,
        to anything but the sign-in endpoints."""
        return not self.open and BACKGROUND.get() and path.strip("/").split("/")[0] not in _ALWAYS

    def move(
        self,
        state: Link,
        detail: str | None = None,
        *,
        url: str | None = None,
        inquiry: str | None = None,
    ) -> None:
        """Change state, logged once per change rather than once per failed request."""
        checking = state == Link.VERIFYING
        if state == self.state and (not checking or inquiry == self.inquiry):
            return
        event = log.info if state == Link.CONNECTED else log.warning
        event("brain.connection", state=str(state), was=str(self.state), detail=detail)
        self.state, self.since, self.detail = state, time.time(), detail
        self.verification_url, self.inquiry = (url, inquiry) if checking else (None, None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": str(self.state),
            "since": datetime.fromtimestamp(self.since, UTC),
            "detail": self.detail,
            "verificationUrl": self.verification_url,
            "inquiry": self.inquiry,
        }


if __name__ == "__main__":
    import sys

    def held(path: str, *, as_background: bool) -> bool:
        token = BACKGROUND.set(as_background)
        try:
            return link.holds(path)
        finally:
            BACKGROUND.reset(token)

    link = Connection()
    checks = {"open at first": link.open and not held("/alphas/x", as_background=True)}
    link.move(Link.OFFLINE, "getaddrinfo failed")
    checks["a background poll is held"] = held("/simulations/x", as_background=True)
    checks["the person's request goes through"] = not held("/alphas/x", as_background=False)
    checks["signing in goes through"] = not held("/authentication/persona", as_background=True)
    link.move(Link.VERIFYING, url="https://x", inquiry="inq_1")
    checks["the check is kept"] = link.inquiry == "inq_1" and link.verification_url == "https://x"
    link.move(Link.VERIFYING_EXPIRED)
    checks["an expired check is dropped"] = link.inquiry is None
    link.move(Link.CONNECTED)
    checks["back to open"] = link.open and not held("/simulations/x", as_background=True)
    if failed := [name for name, ok in checks.items() if not ok]:
        raise SystemExit(f"connection self-check failed: {'; '.join(failed)}")
    sys.stdout.write(f"{len(checks)} cases as expected\n")
