"""The quarter BRAIN judges a consultant on.

Levels are evaluated quarterly against three criteria, two of which are countable here:
signals submitted in the quarter, and pyramids formulated in it. The third, combined Alpha
performance, is BRAIN's own arithmetic and is not guessed at. Ties are broken partly on the
distinct operators the quarter's Alphas use, which is countable too.
"""

import asyncio
from collections import Counter
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import APIRouter

from ..brain.filters import PLATFORM_TZ, AlphaQuery, Filter
from ..brain.platform import (
    ALPHA_LIST_MAX_OFFSET,
    ALPHA_LIST_PAGE,
    PYRAMID_MIN_ALPHAS,
    UNCOUNTED_OPERATORS,
)
from ..brain.schemas import Alpha, SimulationType
from ..catalog.pyramids import next_quarter_start, quarter_start
from ..labs.fastexpr import ParseError, parse
from ..labs.launch import account_operators
from ..schemas import Out
from ..tools.description import operators_in
from .deps import State, refuse

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ..state import AppState

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


class OperatorUse(Out):
    name: str
    #: BRAIN's own, e.g. ``Time Series``.
    category: str
    #: How many of the quarter's submitted Alphas use it.
    alphas: int


class OperatorUsage(Out):
    label: str
    #: The quarter's submitted Alphas whose operators are counted.
    alphas: int
    #: Submitted, but with an expression the parser could not read, so not counted.
    unreadable: int
    #: Submitted, but past the most BRAIN lists, so not counted.
    unlisted: int
    #: Every operator a regular Alpha can use, unused first, then the least used.
    operators: list[OperatorUse]


def _quarter() -> tuple[date, date]:
    """The quarter in progress: its first day, and the next quarter's."""
    # BRAIN's day, as the Pyramids screen counts it: a UTC date starts the next quarter hours early.
    today = datetime.now(PLATFORM_TZ).date()
    return quarter_start(today), next_quarter_start(today)


def _label(start: date) -> str:
    return f"{start.year}-Q{(start.month - 1) // 3 + 1}"


@router.get("")
async def standing(state: State) -> QuarterStanding:
    """Submitted Alphas and formulated pyramids for the quarter in progress.

    Two reads, run together. Neither is derivable from the other: an Alpha belongs to as
    many pyramids as it has data categories, so the pyramid counts sum to more than the
    number of Alphas and cannot stand in for it.
    """
    start, after = _quarter()
    # Both ends inclusive.
    end = after - timedelta(days=1)
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
        label=_label(start),
        start=first,
        end=last,
        submitted=submitted,
        pyramids_formulated=sum(1 for n in counts if n >= PYRAMID_MIN_ALPHAS),
        pyramids_started=sum(1 for n in counts if 0 < n < PYRAMID_MIN_ALPHAS),
        alphas_per_pyramid=PYRAMID_MIN_ALPHAS,
    )


@router.get("/operators")
async def operators(state: State) -> OperatorUsage:
    """How many of the quarter's submitted Alphas use each of the account's operators.

    The unused lead: BRAIN ranks the distinct operators a consultant used in the quarter, so
    they are the ones worth a try.
    """
    start, after = _quarter()
    listed = await account_operators(state, refresh=False)
    if not listed:
        raise refuse(
            503, "no_operators", "BRAIN's operator list could not be read. Sign in again and retry."
        )
    alphas, total = await _submitted(state, start, after)
    # A region-agnostic parent repeats its children's expression; a SuperAlpha has no regular one.
    expressions = [
        a.regular.code
        for a in alphas
        if a.type != SimulationType.RA_PARENT and a.regular and a.regular.code
    ]
    rows, unreadable = usage(expressions, listed)
    return OperatorUsage(
        label=_label(start),
        alphas=len(expressions) - unreadable,
        unreadable=unreadable,
        unlisted=max(0, total - len(alphas)),
        operators=rows,
    )


async def _submitted(state: AppState, start: date, after: date) -> tuple[list[Alpha], int]:
    """The quarter's submitted Alphas as BRAIN lists them, and how many it says there are."""
    window = [
        Filter("status", "!=", "UNSUBMITTED"),
        Filter(
            "dateSubmitted", ">=", datetime(start.year, start.month, start.day, tzinfo=PLATFORM_TZ)
        ),
        Filter(
            "dateSubmitted", "<", datetime(after.year, after.month, after.day, tzinfo=PLATFORM_TZ)
        ),
    ]
    alphas: list[Alpha] = []
    total = 0
    # shortcut: stops at BRAIN's offset cap, 1,100 Alphas, beyond four submissions a day for a
    # quarter; page on by date as vault.backfill does if region-agnostic children ever pass it.
    for offset in range(0, ALPHA_LIST_MAX_OFFSET + 1, ALPHA_LIST_PAGE):
        page = await state.endpoints.list_alphas(
            AlphaQuery(
                limit=ALPHA_LIST_PAGE,
                offset=offset,
                order="-dateSubmitted",
                filters=window,
                # A submitted Alpha you hid is still submitted.
                hidden=None,
            )
        )
        results = page["results"]
        total = int(page["count"])
        alphas.extend(Alpha.model_validate(raw) for raw in results)
        if len(results) < ALPHA_LIST_PAGE:
            break
    return alphas, total


def usage(
    expressions: Iterable[str], operators: list[dict[str, Any]]
) -> tuple[list[OperatorUse], int]:
    """How many of ``expressions`` use each operator a regular Alpha can, unused first, then
    the least used; and how many expressions could not be read.

    ts_backfill and group_backfill are left out: BRAIN does not count them among the distinct
    operators it ranks on.
    """
    used: Counter[str] = Counter()
    unreadable = 0
    for expression in expressions:
        try:
            used.update(operators_in(parse(expression)))
        except ParseError:
            unreadable += 1
    rows = [
        OperatorUse(name=name, category=str(o.get("category") or ""), alphas=used[name])
        for o in operators
        if isinstance(name := o.get("name"), str)
        and name not in UNCOUNTED_OPERATORS
        and "REGULAR" in (o.get("scope") or ["REGULAR"])
    ]
    return sorted(rows, key=lambda r: (r.alphas, r.name)), unreadable


if __name__ == "__main__":
    import sys

    def offered(name: str, *scope: str) -> dict[str, Any]:
        return {"name": name, "category": "Test", "scope": list(scope or ("COMBO", "REGULAR"))}

    account = [
        offered(name)
        for name in (
            "rank",
            "ts_delta",
            "reverse",
            "divide",
            "greater",
            "if_else",
            "subtract",
            "ts_zscore",
            "ts_backfill",
        )
    ]
    account.append(offered("combo_a", "COMBO"))
    rows, unreadable = usage(
        [
            "rank(-ts_delta(close, 5))",
            "rank(close) - rank(open) // rank twice is one Alpha using it",
            "a = ts_backfill(close / open, 20); a > 0 ? -a : -1",
            "rank(",
        ],
        account,
    )
    counts = {row.name: row.alphas for row in rows}
    checks = {
        "an Alpha counts once per operator": counts["rank"] == 2,
        "symbols count as BRAIN's operators": counts["divide"]
        == counts["greater"]
        == counts["if_else"]
        == counts["subtract"]
        == 1,
        "a minus on a call is reverse, on a number the number": counts["reverse"] == 2,
        "unused first": rows[0].name == "ts_zscore" and rows[0].alphas == 0,
        "then the least used": [r.alphas for r in rows] == sorted(r.alphas for r in rows),
        "no backfills, nor operators a regular Alpha cannot use": "ts_backfill" not in counts
        and "combo_a" not in counts,
        "an unreadable expression is counted apart": unreadable == 1,
    }
    if failed := [name for name, ok in checks.items() if not ok]:
        raise SystemExit(f"quarter self-check failed: {'; '.join(failed)}")
    sys.stdout.write(f"{len(checks)} cases as expected\n")
