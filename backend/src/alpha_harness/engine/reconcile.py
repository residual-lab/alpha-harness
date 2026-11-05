"""Resolve simulations whose send outcome is unknown.

``POST /simulations`` has no idempotency key, so when the answer to one is lost BRAIN may
be running it anyway: resending at once pays twice for the same alpha, and giving up loses
the alpha the quota already paid for.

So such rows wait as ``ORPHANED`` while this pass looks for the alpha they produced among
the account's alphas created around the send. A row with no match once :data:`WINDOW` has
passed is queued again: by then a simulation BRAIN accepted would have finished, so the
likeliest story is that the request never ran.
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from ..brain.errors import BrainAuthError, BrainError
from ..brain.filters import AlphaQuery
from ..brain.platform import ALPHA_LIST_MAX_OFFSET, ALPHA_LIST_PAGE
from ..brain.schemas import produced_type
from ..db.models import SimStatus, SimulationRecord, utcnow
from .lifecycle import remember, transition

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..brain.endpoints import BrainEndpoints
    from ..db.sqlite import Database

log = structlog.get_logger(__name__)

#: How long after a lost send its alpha is waited for before the request is sent again.
WINDOW = timedelta(minutes=30)
#: Tolerance between our clock and BRAIN's ``dateCreated``.
CLOCK_SLACK = timedelta(minutes=2)
#: Lost sends queued again before one more is given up on.
MAX_ORPHAN_REQUEUES = 2

_EXPRESSION_KEYS = ("regular", "combo", "selection")


def squash(code: Any) -> str | None:
    """An expression with whitespace collapsed, unwrapped from ``{"code": ...}`` if needed."""
    if isinstance(code, dict):
        code = code.get("code")  # pyright: ignore[reportUnknownMemberType]
    return " ".join(code.split()) if isinstance(code, str) else None


def same_value(ours: Any, theirs: Any) -> bool:
    """A setting we sent against BRAIN's echo of it; numbers within float noise match.

    The tolerance absorbs a single-precision round trip (~1e-7) and stays far below any
    step a sweep takes between two requests.
    """
    numbers = (int, float)
    if (
        isinstance(ours, numbers)
        and isinstance(theirs, numbers)
        and not isinstance(ours, bool)
        and not isinstance(theirs, bool)
    ):
        return math.isclose(ours, theirs, rel_tol=1e-7, abs_tol=1e-9)
    return ours == theirs


def matches(payload: dict[str, Any], alpha: dict[str, Any]) -> bool:
    """Whether ``alpha`` (a listing row) is what the request ``payload`` produced.

    Only what we asked for is compared: the platform echoes settings we left to its
    defaults (``maxTrade``, ``testPeriod``...), and those say nothing against a match.
    Expressions are compared with whitespace collapsed, since the stored code gains a
    trailing newline.
    """
    # Translated, not compared as-is: a region-agnostic request comes back as an RA parent,
    # so the literal comparison refused to adopt every orphaned RA simulation there was.
    if produced_type(payload.get("type", "REGULAR")) != alpha.get("type", "REGULAR"):
        return False
    for key in _EXPRESSION_KEYS:
        if key in payload and squash(payload[key]) != squash(alpha.get(key)):
            return False
    theirs = alpha.get("settings") or {}
    return all(
        key in theirs and same_value(value, theirs[key])
        for key, value in (payload.get("settings") or {}).items()
    )


@dataclass(slots=True)
class _Cluster:
    start: datetime
    end: datetime
    rows: list[tuple[SimulationRecord, datetime]]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _clusters(rows: list[tuple[SimulationRecord, datetime]], now: datetime) -> list[_Cluster]:
    """Group sends whose search windows overlap, so each group costs one listing.

    One window over every orphan would span days when old rows exist, and a day of this
    app's output alone passes the listing's 1,000-row offset limit.
    """
    clusters: list[_Cluster] = []
    for row, anchor in sorted(rows, key=lambda item: item[1]):
        start, end = anchor - CLOCK_SLACK, min(now, anchor + WINDOW + CLOCK_SLACK)
        if clusters and start <= clusters[-1].end:
            clusters[-1].end = max(clusters[-1].end, end)
            clusters[-1].rows.append((row, anchor))
        else:
            clusters.append(_Cluster(start, end, [(row, anchor)]))
    return clusters


async def _listed(endpoints: BrainEndpoints, cluster: _Cluster) -> list[dict[str, Any]]:
    """Every alpha created inside the cluster, oldest first.

    Merged clusters can span hours, past the listing's offset limit. At the limit the
    window restarts one second before the last ``dateCreated`` seen; the overlap covers
    ties, and ids de-duplicate it.
    """
    found: dict[str, dict[str, Any]] = {}
    start = cluster.start
    offset = 0
    while True:
        page = await endpoints.list_alphas(
            AlphaQuery(
                limit=ALPHA_LIST_PAGE,
                offset=offset,
                order="dateCreated",
                hidden=None,
                created_after=start,
                created_before=cluster.end,
            )
        )
        results = [r for r in page["results"] if isinstance(r, dict)]
        for r in results:
            found.setdefault(str(r.get("id")), r)
        if len(results) < ALPHA_LIST_PAGE:
            break
        offset += ALPHA_LIST_PAGE
        if offset <= ALPHA_LIST_MAX_OFFSET:
            continue
        last = _created(results[-1])
        # A second holding more than the whole window cannot advance; stop rather than loop.
        if last is None or last - timedelta(seconds=1) <= start:
            break
        start, offset = last - timedelta(seconds=1), 0
    return list(found.values())


def _created(alpha: dict[str, Any]) -> datetime | None:
    raw = alpha.get("dateCreated")
    try:
        return _aware(datetime.fromisoformat(raw)) if isinstance(raw, str) else None
    except ValueError:
        return None


async def reconcile_orphans(
    db: Database,
    endpoints: BrainEndpoints,
    *,
    on_alpha: Callable[[str], None],
) -> dict[str, int]:
    """One pass over every ``ORPHANED`` row. Returns counts of what it resolved."""
    now = utcnow()
    async with db.session() as session:
        orphans = list(
            await session.scalars(
                select(SimulationRecord).where(SimulationRecord.status == SimStatus.ORPHANED)
            )
        )
        parents = {
            r.id: r
            for r in await session.scalars(
                select(SimulationRecord).where(
                    SimulationRecord.id.in_(
                        {o.parent_record_id for o in orphans if o.parent_record_id}
                    )
                )
            )
        }

    batches = [o for o in orphans if o.is_batch]
    rows: list[tuple[SimulationRecord, datetime]] = []
    for row in orphans:
        if row.is_batch:
            continue
        parent = parents.get(row.parent_record_id or -1)
        anchor = row.sent_at or (parent.sent_at or parent.created_at if parent else None)
        rows.append((row, anchor or row.created_at))

    adopted: list[tuple[SimulationRecord, str]] = []
    requeue: list[int] = []
    abandon: list[int] = []
    for cluster in _clusters(rows, now):
        try:
            listed = await _listed(endpoints, cluster)
        except BrainAuthError:
            return {"adopted": 0, "requeued": 0}  # The session watch deals with it.
        except BrainError as exc:
            log.warning("reconcile.list_failed", error=str(exc))
            continue
        for row, anchor in cluster.rows:
            hit = next(
                (
                    a
                    for a in listed
                    if isinstance(a.get("id"), str)
                    and (created := _created(a)) is not None
                    and created >= anchor - CLOCK_SLACK
                    and matches(row.payload or {}, a)
                ),
                None,
            )
            if hit is not None:
                adopted.append((row, hit["id"]))
            elif anchor + WINDOW < now:
                # A send that keeps losing its answer would otherwise loop here forever.
                lost_before = row.orphan_requeues >= MAX_ORPHAN_REQUEUES
                (abandon if lost_before else requeue).append(row.id)

    landed: list[str] = []
    async with db.session() as session:
        for row, alpha_id in adopted:
            if await transition(
                session,
                row.id,
                from_=[SimStatus.ORPHANED],
                status=SimStatus.COMPLETE,
                alpha_id=alpha_id,
                # It was sent, so it spent quota on the day it went out.
                submitted_at=row.sent_at or utcnow(),
                progress=1.0,
                message=f"Recovered after BRAIN's answer was lost: matched alpha {alpha_id}.",
                finished_at=utcnow(),
            ):
                await remember(session, row, alpha_id)
                landed.append(alpha_id)
        requeued = await transition(
            session,
            requeue,
            from_=[SimStatus.ORPHANED],
            status=SimStatus.QUEUED,
            parent_record_id=None,
            sent_at=None,
            finished_at=None,
            orphan_requeues=SimulationRecord.orphan_requeues + 1,
            message="No alpha appeared within 30 minutes of the lost send; queued again.",
        )
        await transition(
            session,
            abandon,
            from_=[SimStatus.ORPHANED],
            status=SimStatus.ERROR,
            finished_at=utcnow(),
            message=f"BRAIN's answer to this simulation was lost {MAX_ORPHAN_REQUEUES + 1} "
            "times and no alpha appeared, so it is not sent again.",
        )
        # A batch parent carries no alpha of its own; its children are resolved one by
        # one above. Close it so it does not sit unfinished in every list.
        await transition(
            session,
            [b.id for b in batches],
            from_=[SimStatus.ORPHANED],
            status=SimStatus.ERROR,
            children_expanded=True,
            finished_at=utcnow(),
            message="BRAIN's answer to this batch was lost; its simulations are resolved "
            "one by one.",
        )

    for alpha_id in landed:
        on_alpha(alpha_id)
    if landed or requeued or abandon:
        log.warning(
            "reconcile.resolved",
            adopted=len(landed),
            requeued=len(requeued),
            abandoned=len(abandon),
        )
    return {"adopted": len(landed), "requeued": len(requeued), "abandoned": len(abandon)}
