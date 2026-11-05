"""The simulation record's lifecycle: the rules every row follows, whoever drives it.

Both :class:`~alpha_harness.engine.tracker.SimulationTracker` (one simulation at a time)
and :class:`~alpha_harness.engine.slots.BatchEngine` (multi-simulations) move the same
``SimulationRecord`` rows through the same statuses, so what must be identical between
them lives here: the compare-and-set that changes a status, the durable write of a
platform id, how a finished body maps to a local outcome, the write that ends a row, and
the dedup memory.

Re-running an identical alpha consumes quota for nothing — the platform counts it even
though the alpha already exists — so the full request is hashed to recognise a repeat.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

from sqlalchemy import case, update

from ..brain.errors import BrainError
from ..brain.platform import GLB_REGION, GLB_SLOTS, RA_CHILDREN, RA_SLOTS
from ..brain.schemas import FULL_MODE, SimulationRequest, SimulationStatus, SimulationType
from ..db.models import ACTIVE, DedupEntry, QuotaSnapshot, SimStatus, SimulationRecord, utcnow

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

#: What one row costs off the day's allowance. BRAIN charges a region-agnostic row per
#: region it is translated into rather than per request. GLB is *not* doubled here: its
#: rule is about concurrency, not the allowance — see :data:`SLOT_COST`.
SIMULATION_COST = case(
    (SimulationRecord.sim_type == SimulationType.REGION_AGNOSTIC, RA_CHILDREN), else_=1
)

#: What one row costs in *concurrent cores*, which is not the same as what it costs off the
#: day's allowance. Counting GLB as one would let the engine keep eight GLB simulations out
#: while BRAIN allows four, so two thirds of a round would sit refused and the cores would
#: read as fluctuating rather than full.
SLOT_COST = case(
    (SimulationRecord.sim_type == SimulationType.REGION_AGNOSTIC, RA_SLOTS),
    (SimulationRecord.region == GLB_REGION, GLB_SLOTS),
    else_=1,
)

#: Platform status -> our local lifecycle.
_STATUS_MAP = {
    SimulationStatus.COMPLETE: SimStatus.COMPLETE,
    SimulationStatus.WARNING: SimStatus.WARNING,
    SimulationStatus.ERROR: SimStatus.ERROR,
    SimulationStatus.FAIL: SimStatus.FAILED,
    SimulationStatus.TIMEOUT: SimStatus.TIMEOUT,
    SimulationStatus.CANCELLED: SimStatus.CANCELLED,
}
#: Platform statuses of a simulation that has not finished.
_RUNNING = (SimulationStatus.WAITING, SimulationStatus.SIMULATING)


# -- dedup -------------------------------------------------------------------


def _canonical_json(payload: Any) -> str:
    """Stable JSON: sorted keys, no incidental whitespace.

    Two requests that differ only in key order or float formatting must hash the same,
    or the cache silently misses and the quota is spent anyway.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def hash_payload(payload: Any) -> str:
    """SHA-256 of the canonical form."""
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def request_hash(request: SimulationRequest) -> str:
    """What an identical request shares. ``FULL`` is BRAIN's default, so naming it changes
    nothing: a Full re-run matches every Alpha simulated before modes were sent."""
    payload = request.to_wire()
    settings = payload.get("settings")
    if isinstance(settings, dict) and settings.get("simulationMode") == FULL_MODE:
        del settings["simulationMode"]
    return hash_payload(payload)


async def remember(session: Any, record: SimulationRecord, alpha_id: str) -> None:
    """Record what this payload produced, so an identical request reuses it for free."""
    if await session.get(DedupEntry, record.request_hash) is None:
        session.add(DedupEntry(request_hash=record.request_hash, alpha_id=alpha_id))


# -- writes ------------------------------------------------------------------


async def transition(
    session: Any,
    ids: int | Iterable[int],
    *,
    from_: Iterable[SimStatus],
    **values: Any,
) -> list[int]:
    """Move rows on only if they are still in one of ``from_``; returns the ids moved.

    The single way a lifecycle status changes. Every write is a compare-and-set, so a
    user's cancel or drop that lands while a send is awaiting BRAIN is never overwritten
    by the send's own next step — the loser sees an empty result and backs off.
    """
    id_list = [ids] if isinstance(ids, int) else list(ids)
    if not id_list:
        return []
    result = await session.execute(
        update(SimulationRecord)
        .where(SimulationRecord.id.in_(id_list), SimulationRecord.status.in_(list(from_)))
        .values(**values)
        .returning(SimulationRecord.id)
        .execution_options(synchronize_session=False)
    )
    return list(result.scalars().all())


async def finish(
    session: Any, record: SimulationRecord, outcome: Outcome, **values: Any
) -> str | None:
    """End an active row as ``outcome`` says. Returns its alpha id if this write ended it.

    A simulation finishes the same way whether the tracker read it alone or the batch engine
    read it as a child. ``values`` carries what only one caller knows, such as a child's
    platform id. The alpha comes back only when this write won, so a row something else
    ended first is never handed on twice.
    """
    alpha_id = outcome.alpha_id
    won = await transition(
        session,
        record.id,
        from_=ACTIVE,
        status=outcome.status,
        platform_status=str(outcome.platform_status) if outcome.platform_status else None,
        alpha_id=alpha_id,
        message=outcome.message,
        progress=1.0 if alpha_id else record.progress,
        finished_at=utcnow(),
        **values,
    )
    if not won or alpha_id is None:
        return None
    # Without this, re-running an identical alpha spends daily quota to recreate something
    # that already exists. Batch parents are excluded: their payload is the array.
    if not record.is_batch:
        await remember(session, record, alpha_id)
    return alpha_id


def new_record(
    request: SimulationRequest, *, task: str, status: SimStatus, **extra: Any
) -> SimulationRecord:
    """A row describing ``request``, before anything is sent for it."""
    payload = request.to_wire()
    settings = request.settings
    return SimulationRecord(
        request_hash=request_hash(request),
        payload=payload,
        expression=request.regular or request.combo or request.selection,
        sim_type=str(request.type),
        instrument_type=settings.instrument_type,
        region=settings.region,
        delay=settings.delay,
        language=settings.language,
        universe=settings.universe,
        task=task,
        status=status,
        **extra,
    )


async def record_launch(
    session: Any,
    record_id: int,
    platform_id: str,
    rate_limit: Any,
    *,
    children: Iterable[int] = (),
) -> bool:
    """Commit the id BRAIN just returned and flip the row to ``RUNNING``.

    Returns ``False`` when a cancel won the race while the POST was out: the id is still
    kept on the row, and the caller must cancel the simulation on BRAIN. ``ORPHANED`` is
    accepted too, since the stale-send sweep may have given up on a slow send whose id has
    now arrived. A batch's ``children`` ride along only when the parent itself moved.

    The ``x-ratelimit-*`` quota reading from the same response is stored in the same
    transaction, when the response carried one.
    """
    now = utcnow()
    sent_from = [SimStatus.PENDING, SimStatus.ORPHANED]
    won = await transition(
        session,
        record_id,
        from_=sent_from,
        platform_id=platform_id,
        status=SimStatus.RUNNING,
        submitted_at=now,
        finished_at=None,
    )
    if won:
        await transition(
            session,
            children,
            from_=sent_from,
            status=SimStatus.RUNNING,
            submitted_at=now,
            finished_at=None,
        )
    else:
        await session.execute(
            update(SimulationRecord)
            .where(SimulationRecord.id == record_id)
            .values(platform_id=platform_id, submitted_at=now)
        )
    if rate_limit is not None:
        session.add(
            QuotaSnapshot(
                limit_total=rate_limit.limit,
                remaining=rate_limit.remaining,
                reset_seconds=rate_limit.reset_seconds,
            )
        )
    return bool(won)


async def cancel_after_lost_race(
    db: Any, endpoints: Any, record_id: int, platform_id: str, *, children: Iterable[int] = ()
) -> bool:
    """Stop a simulation a cancel beat :func:`record_launch` to. Returns whether it stopped.

    If BRAIN refuses or the DELETE fails, the simulation runs on and spends quota, so its
    rows go back to ``RUNNING`` for the caller to poll: left ``CANCELLED``, its alpha was
    never recorded.
    """
    try:
        stopped = await endpoints.cancel_simulation(platform_id)
    except BrainError:
        stopped = False
    if stopped:
        return True
    now = utcnow()
    async with db.session() as session:
        await transition(
            session,
            record_id,
            from_=[SimStatus.CANCELLED],
            status=SimStatus.RUNNING,
            finished_at=None,
            message="BRAIN would not cancel it, so it is still running.",
        )
        await transition(
            session,
            children,
            from_=[SimStatus.PENDING, SimStatus.ORPHANED],
            status=SimStatus.RUNNING,
            submitted_at=now,
        )
    return False


# -- reading BRAIN's answers -------------------------------------------------


OutcomeKind = Literal["pending", "fanout", "finished", "retry", "gone", "unauthorized"]


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one ``GET /simulations/{id}`` means for the row that asked it.

    ``pending`` and ``retry`` read again after ``delay`` (``None``: caller's default);
    ``fanout`` hands the batch's ``children`` to the engine; ``finished`` ends the row as
    ``status``; ``gone`` and ``unauthorized`` are the 404 and 401 the caller acts on.
    """

    kind: OutcomeKind
    status: SimStatus = SimStatus.RUNNING
    platform_status: SimulationStatus | None = None
    alpha_id: str | None = None
    children: tuple[str, ...] = ()
    message: str | None = None
    progress: float | None = None
    delay: float | None = None


def read_outcome(status_code: int, body: Any, retry_after: float | None) -> Outcome:
    """Classify a simulation read, in the order BRAIN's own client checks it.

    The order is the point: ``{progress}``,
    ``{children}`` and ``{detail}`` bodies carry no ``status``, and nothing here may finish
    as COMPLETE without the platform saying so or an alpha to show for it.
    """
    if status_code == 401:
        return Outcome("unauthorized", delay=retry_after)
    if status_code == 404:
        return Outcome("gone", SimStatus.ERROR, message="BRAIN no longer has this simulation.")
    data: dict[str, Any] = body if isinstance(body, dict) else {}
    if status_code >= 400:
        # A 429, 403 or 5xx is about the read, not the simulation: it is still running.
        return Outcome("retry", delay=retry_after, message=_text(data, "detail"))

    progress = data.get("progress")
    platform = _platform_status(data.get("status"))
    if (
        retry_after is not None
        or platform in _RUNNING
        or (isinstance(progress, int | float) and not isinstance(progress, bool))
    ):
        return Outcome(
            "pending",
            progress=float(progress) if isinstance(progress, int | float) else None,
            delay=retry_after,
        )

    if data.get("status") and platform is None:
        # A status outside the documented eight. A running simulation may report a new one;
        # closing it would discard a paid-for alpha.
        unknown = f"Unrecognised status {data['status']!r}"
        return Outcome("retry", delay=retry_after, message=unknown)
    children = tuple(str(c) for c in data.get("children") or ())
    if children:
        # The children carry the outcomes, and may still be running: "ERROR is terminal
        # only when children is absent". Whatever the parent says, they must be read.
        return Outcome("fanout", platform_status=platform, children=children, progress=1.0)

    message = _text(data, "message") or _text(data, "detail") or _text(data, "details")
    alpha_id = data.get("alpha") if isinstance(data.get("alpha"), str) else None
    location = data.get("location")
    if isinstance(location, str) and location and not message and platform != "COMPLETE":
        # ``location`` is the partial alpha's id, not a place in the expression.
        message = f"BRAIN reported {platform or 'a problem'}; partial alpha {location}."

    if platform is not None:
        return Outcome(
            "finished",
            _STATUS_MAP[platform],
            platform_status=platform,
            alpha_id=alpha_id,
            message=message,
        )
    if message is not None and alpha_id is None:
        # ``{detail}`` / ``{details}``: the documented error bodies without a status.
        return Outcome("finished", SimStatus.ERROR, message=message)
    if alpha_id is not None:
        return Outcome("finished", SimStatus.COMPLETE, alpha_id=alpha_id, message=message)
    # Nothing documented matches. Keep reading rather than close what may still be running.
    return Outcome("retry", delay=retry_after, message=f"Unrecognised body: {data!r:.200}")


def _platform_status(raw: Any) -> SimulationStatus | None:
    try:
        return SimulationStatus(raw) if raw else None
    except ValueError:
        return None


def _text(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def describe_fields(fields: Any, fallback: str) -> str:
    """Flatten BRAIN's nested field errors into one readable line."""
    parts: list[str] = []

    def walk(prefix: str, node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(f"{prefix}.{key}" if prefix else str(key), value)
        elif isinstance(node, list):
            parts.extend(f"{prefix}: {item}" if prefix else str(item) for item in node)
        else:
            parts.append(f"{prefix}: {node}" if prefix else str(node))

    walk("", fields)
    return "; ".join(parts) or fallback


def extract_simulation_id(location: str | None) -> str | None:
    """Pull the id out of a ``Location`` header.

    The header is absolute (``https://api.worldquantbrain.com/simulations/{id}``) but
    tolerate a relative path too.
    """
    if not location:
        return None
    path = urlparse(location).path or location
    segment = path.rstrip("/").rsplit("/", 1)[-1]
    return segment or None


class SubmissionFailed(RuntimeError):
    """A simulation could not be started.

    ``message`` carries the platform's own words wherever it gave any — a researcher
    needs "TOP9000 is not a valid choice", not "rejected".
    """

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


# -- wire shape --------------------------------------------------------------


def serialise(record: SimulationRecord) -> dict[str, Any]:
    """Wire shape for the API and the live WebSocket feed."""
    return {
        "id": record.id,
        "platformId": record.platform_id,
        "alphaId": record.alpha_id,
        "status": record.status,
        "platformStatus": record.platform_status,
        "progress": record.progress,
        "message": record.message,
        "expression": record.expression,
        "task": record.task,
        "region": record.region,
        "delay": record.delay,
        "universe": record.universe,
        "instrumentType": record.instrument_type,
        "language": record.language,
        "simType": record.sim_type,
        "isBatch": record.is_batch,
        "childIds": record.child_ids or [],
        # The batch parent's record id, so a child stays tied to its batch even after the
        # parent has finished and left the active set.
        "parentId": record.parent_record_id,
        "createdAt": _iso(record.created_at),
        "submittedAt": _iso(record.submitted_at),
        "finishedAt": _iso(record.finished_at),
        "elapsedSeconds": record.elapsed_seconds,
        "settings": (record.payload or {}).get("settings"),
    }


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
