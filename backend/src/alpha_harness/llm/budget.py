"""Tracking what each key has spent.

No provider publishes a remaining-quota endpoint, so the only way to rotate keys sensibly
is to count locally, against the limits the user set up. Two windows, tracked differently
because they behave differently:

* **Requests per minute** is a sliding sixty-second window, kept in memory: losing it on a
  restart costs at most one minute of over-caution.
* **Requests per day** is a calendar day and *must* survive a restart, or a spent key looks
  fresh and every rotation walks into a ``429``.

Tokens are recorded for the record, never limited on: no count made here matches the
provider's own.

**The day boundary is the model's own.** Providers do not agree on when a day starts, Google
at midnight Pacific and OpenRouter at midnight UTC, so each model carries the time zone its
user named, and a day counted on the wrong clock hands a budget back hours early or late.

Accounting is deliberately conservative — a request is refused here when it *would* exceed
a limit — because a local refusal can name another key or model, and a ``429`` cannot.
"""

import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from datetime import time as clock  # `time` is already the module, imported above
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from ..db.models import KeyUsage, utcnow

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..db.sqlite import Database
    from .registry import ModelInfo

log = structlog.get_logger(__name__)

WINDOW_SECONDS = 60.0
#: The usage row that counts a key's requests to every model, for its own daily cap.
ALL_MODELS = "*"


def quota_day(tz: tzinfo, moment: datetime | None = None) -> str:
    """The day a moment falls in on ``tz``'s clock, as ``YYYY-MM-DD``."""
    return (moment or utcnow()).astimezone(tz).strftime("%Y-%m-%d")


def model_day(model: ModelInfo) -> str:
    """Today, as the model's provider counts it."""
    return quota_day(ZoneInfo(model.reset_timezone))


def seconds_until_reset(tz: tzinfo, moment: datetime | None = None) -> float:
    """How long until a daily budget comes back at midnight on ``tz``'s clock.

    Built from the next local *date* rather than by adding 24 hours, so the two days a year
    that are 23 or 25 hours long do not shift the answer.
    """
    now = (moment or utcnow()).astimezone(tz)
    midnight = datetime.combine(now.date() + timedelta(days=1), clock.min, tzinfo=tz)
    # In UTC: two times sharing a zone subtract as wall clocks, which ignores the change.
    return max(0.0, (midnight.astimezone(UTC) - now.astimezone(UTC)).total_seconds())


def model_reset(model: ModelInfo) -> float:
    """Seconds until the model's provider starts a new day."""
    return seconds_until_reset(ZoneInfo(model.reset_timezone))


@dataclass(slots=True)
class Window:
    """A sliding sixty-second record of requests."""

    events: deque[float] = field(default_factory=deque)

    def trim(self, now: float) -> None:
        cutoff = now - WINDOW_SECONDS
        while self.events and self.events[0] < cutoff:
            self.events.popleft()

    def add(self, now: float) -> None:
        self.events.append(now)
        self.trim(now)

    def requests(self, now: float) -> int:
        self.trim(now)
        return len(self.events)

    def next_free(self, now: float) -> float:
        """Seconds until the oldest request ages out of the window."""
        self.trim(now)
        if not self.events:
            return 0.0
        return max(0.0, WINDOW_SECONDS - (now - self.events[0]))


@dataclass(slots=True)
class Headroom:
    """What is left on one (key, model) pair, and what is blocking it."""

    key_id: int
    model: str
    requests_today: int
    requests_per_day: int
    requests_this_minute: int
    requests_per_minute: int
    blocked_by: str | None = None
    #: Seconds until the block clears. Large for a daily limit, small for a per-minute one.
    retry_after: float = 0.0

    @property
    def available(self) -> bool:
        return self.blocked_by is None

    @property
    def daily_remaining(self) -> int:
        return max(0, self.requests_per_day - self.requests_today)

    def to_dict(self) -> dict[str, Any]:
        return {
            "keyId": self.key_id,
            "model": self.model,
            "available": self.available,
            "blockedBy": self.blocked_by,
            "retryAfter": round(self.retry_after, 1),
            "requestsToday": self.requests_today,
            "requestsPerDay": self.requests_per_day,
            "dailyRemaining": self.daily_remaining,
            "requestsThisMinute": self.requests_this_minute,
            "requestsPerMinute": self.requests_per_minute,
        }


class Ledger:
    """Per-key, per-model usage accounting."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._windows: dict[tuple[int, str], Window] = {}
        #: Daily counts, read through from the database on first use and kept in step
        #: with it afterwards, so the hot path does not hit SQLite per check.
        self._daily: dict[tuple[int, str, str], int] = {}
        #: Requests sent and not yet settled, per (key, model). They count against the day
        #: until :meth:`record` makes it permanent, or callers at once could overrun a cap.
        self._in_flight: Counter[tuple[int, str]] = Counter()

    def forget(self, key_id: int) -> None:
        """Drop a removed key's counts: SQLite hands its id to the next key added."""
        self._windows = {k: v for k, v in self._windows.items() if k[0] != key_id}
        self._daily = {k: v for k, v in self._daily.items() if k[0] != key_id}

    def _window(self, key_id: int, model: str) -> Window:
        return self._windows.setdefault((key_id, model), Window())

    async def _count(self, key_id: int, model_id: str, day: str) -> int:
        """Requests a key has sent a model (or :data:`ALL_MODELS`) on ``day``."""
        cached = self._daily.get((key_id, model_id, day))
        if cached is not None:
            return cached
        async with self.db.session() as session:
            row = await session.scalar(
                select(KeyUsage).where(
                    KeyUsage.api_key_id == key_id,
                    KeyUsage.model == model_id,
                    KeyUsage.day == day,
                )
            )
            count = int(row.requests) if row else 0
        self._daily[(key_id, model_id, day)] = count
        return count

    async def headroom(self, key_id: int, model: ModelInfo, *, cap: int | None = None) -> Headroom:
        """What is left, and the first limit that would stop the next request.

        ``cap`` is the key's own daily limit. A paid key must have one, because it is the
        only thing between its owner and a bill, so it counts every model the key serves.
        Where both are set, the one closer to running out stops.
        """
        now = time.monotonic()
        window = self._window(key_id, model.id)
        day = model_day(model)
        today = await self._count(key_id, model.id, day) + self._in_flight[(key_id, model.id)]
        daily = model.rpd
        if cap is not None:
            spent = await self._count(key_id, ALL_MODELS, day) + sum(
                n for (key, _), n in self._in_flight.items() if key == key_id
            )
            if cap - spent < daily - today:
                today, daily = spent, cap

        state = Headroom(
            key_id=key_id,
            model=model.id,
            requests_today=today,
            requests_per_day=daily,
            requests_this_minute=window.requests(now),
            requests_per_minute=model.rpm,
        )

        # Daily first: it is the one that cannot be waited out in any useful sense.
        if today >= daily:
            state.blocked_by = "requests_per_day"
            state.retry_after = model_reset(model)
        elif state.requests_this_minute >= model.rpm:
            state.blocked_by = "requests_per_minute"
            state.retry_after = window.next_free(now)
        return state

    def sent(self, key_id: int, model: ModelInfo) -> None:
        """Count a request against the minute as it leaves, not when it returns: one still
        in flight already counts at the provider. :meth:`KeyStore.choose` calls it under the
        lock it reads the budget with. The day counts it too, until :meth:`settled`."""
        self._window(key_id, model.id).add(time.monotonic())
        self._in_flight[(key_id, model.id)] += 1

    def settled(self, key_id: int, model: ModelInfo) -> None:
        """The request :meth:`sent` counted has been answered and recorded, or has failed."""
        self._in_flight[(key_id, model.id)] = max(0, self._in_flight[(key_id, model.id)] - 1)

    async def record(self, key_id: int, model: ModelInfo, tokens: int) -> None:
        """Count an answered request against the day, with its tokens.

        Called after the response so the token count is the provider's own, thinking
        tokens included. The minute already counted it in :meth:`sent`.
        """
        day = model_day(model)
        spent, now_at = max(0, tokens), utcnow()

        def bump(model_id: str) -> Any:
            # One atomic statement: calls finishing together otherwise read-then-write the
            # same count and lose increments, and the day's first two collide inserting its row.
            return (
                insert(KeyUsage)
                .values(
                    api_key_id=key_id,
                    model=model_id,
                    day=day,
                    requests=1,
                    tokens=spent,
                    last_request_at=now_at,
                )
                .on_conflict_do_update(
                    index_elements=["api_key_id", "model", "day"],
                    set_={
                        "requests": KeyUsage.requests + 1,
                        "tokens": KeyUsage.tokens + spent,
                        "last_request_at": now_at,
                    },
                )
                .returning(KeyUsage.requests)
            )

        async with self.db.session() as session:
            counted = {
                model_id: (await session.execute(bump(model_id))).scalar_one()
                for model_id in (model.id, ALL_MODELS)
            }
        for model_id, requests in counted.items():
            self._daily[(key_id, model_id, day)] = requests

        # Yesterday's counts are not just stale, they are wrong to serve — drop them. Only
        # this key's for this model: another provider serving the same id keeps its own clock.
        for cached in [
            k
            for k in self._daily
            if k[0] == key_id and k[1] in (model.id, ALL_MODELS) and k[2] != day
        ]:
            del self._daily[cached]

    async def penalise(self, key_id: int, model: ModelInfo, *, daily: bool) -> None:
        """Believe the provider over our own arithmetic.

        A ``429`` means the local count was wrong, so the count is moved to the model's
        ceiling and rotation immediately treats this pair as spent rather than retrying into
        it. Only this model's row: the key's all-models row counts what was actually sent.
        """
        ceiling = model.rpd
        if daily:
            day = model_day(model)
            async with self.db.session() as session:
                row = await session.scalar(
                    select(KeyUsage).where(
                        KeyUsage.api_key_id == key_id,
                        KeyUsage.model == model.id,
                        KeyUsage.day == day,
                    )
                )
                if row is None:
                    # Explicit zeros: column defaults are only applied at flush, and this
                    # row is read back before then.
                    row = KeyUsage(api_key_id=key_id, model=model.id, day=day, requests=0, tokens=0)
                    session.add(row)
                row.requests = max(row.requests or 0, ceiling)
                await session.commit()
                self._daily[(key_id, model.id, day)] = row.requests
            log.warning("llm.budget.daily_exhausted", key_id=key_id, model=model.id)
        else:
            now = time.monotonic()
            window = self._window(key_id, model.id)
            while window.requests(now) < model.rpm:
                window.add(now)
            log.info("llm.budget.minute_exhausted", key_id=key_id, model=model.id)

    async def usage(
        self, providers: Mapping[int, str], models: Mapping[tuple[str, str], ModelInfo]
    ) -> list[dict[str, Any]]:
        """Today's spend per key and set-up model, each today counted on its model's clock.

        ``providers`` maps each key to its provider, and ``models`` is keyed by (provider,
        model id): a usage row names only its key and model id, and ids repeat across
        providers.
        """
        if not providers or not models:
            return []
        today = {pair: model_day(model) for pair, model in models.items()}
        async with self.db.session() as session:
            rows = list(
                (
                    await session.scalars(
                        select(KeyUsage).where(
                            KeyUsage.api_key_id.in_(providers.keys()),
                            KeyUsage.day.in_(set(today.values())),
                        )
                    )
                ).all()
            )
        rows = [r for r in rows if today.get((providers[r.api_key_id], r.model)) == r.day]
        return [
            {
                "keyId": r.api_key_id,
                "model": r.model,
                "day": r.day,
                "requests": r.requests,
                "tokens": r.tokens,
                "lastRequestAt": r.last_request_at.isoformat() if r.last_request_at else None,
            }
            for r in rows
        ]
