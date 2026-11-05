"""The quarter BRAIN judges a consultant on.

Levels are evaluated quarterly against three criteria, two of which are countable here:
signals submitted in the quarter, and pyramids formulated in it. The third, combined Alpha
performance, is BRAIN's own arithmetic and is not guessed at.
"""

import asyncio
from datetime import datetime, timedelta

import structlog
from fastapi import APIRouter

from ..brain.filters import PLATFORM_TZ
from ..brain.platform import PYRAMID_MIN_ALPHAS
from ..catalog.pyramids import next_quarter_start, quarter_start
from ..schemas import Out
from .deps import State

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/quarter", tags=["quarter"])


class QuarterStanding(Out):
    #: ``2026-Q3``, for a label that needs no explaining.
    label: str
    start: str
    end: str
    #: Alphas submitted inside the quarter, summed from BRAIN's own daily counts.
    submitted: int
    #: Pyramids with at least :data:`PYRAMID_MIN_ALPHAS` submitted Alphas in them.
    pyramids_formulated: int
    #: Pyramids with at least one submitted Alpha but not yet enough to count.
    pyramids_started: int
    #: How many Alphas a pyramid needs, so the UI can say it without repeating the rule.
    alphas_per_pyramid: int


@router.get("")
async def standing(state: State) -> QuarterStanding:
    """Submitted Alphas and formulated pyramids for the quarter in progress.

    Two reads, run together. Neither is derivable from the other: an Alpha belongs to as
    many pyramids as it has data categories, so the pyramid counts sum to more than the
    number of Alphas and cannot stand in for it.
    """
    # BRAIN's day, as the Pyramids screen counts it: a UTC date starts the next quarter hours early.
    today = datetime.now(PLATFORM_TZ).date()
    after = next_quarter_start(today)
    # Both ends inclusive.
    start, end = quarter_start(today), after - timedelta(days=1)
    days, pyramids = await asyncio.gather(
        state.endpoints.submission_activity(),
        # BRAIN's own site asks up to the day after the quarter, so its last day counts.
        state.endpoints.pyramid_alphas(start.isoformat(), after.isoformat()),
        return_exceptions=True,
    )
    if isinstance(days, BaseException):
        log.warning("quarter.submissions_failed", exc_info=days)
        days = []
    if isinstance(pyramids, BaseException):
        log.warning("quarter.pyramids_failed", exc_info=pyramids)
        pyramids = []

    first, last = start.isoformat(), end.isoformat()
    submitted = sum(count for day, count in days if first <= day <= last)
    counts = [int(p.get("alphaCount") or 0) for p in pyramids]
    return QuarterStanding(
        label=f"{start.year}-Q{(start.month - 1) // 3 + 1}",
        start=first,
        end=last,
        submitted=submitted,
        pyramids_formulated=sum(1 for n in counts if n >= PYRAMID_MIN_ALPHAS),
        pyramids_started=sum(1 for n in counts if 0 < n < PYRAMID_MIN_ALPHAS),
        alphas_per_pyramid=PYRAMID_MIN_ALPHAS,
    )
