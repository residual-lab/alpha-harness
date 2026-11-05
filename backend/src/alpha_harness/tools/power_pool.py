"""Power Pool Correlation, measured locally against your own submitted Power Pool Alphas.

BRAIN answers this one Alpha at a time, slowly and under a rate limit, so a sweep of a
thousand results cannot be judged by asking it. The same number can be worked out here: the
correlation is Pearson over daily PnL on the days both Alphas traded, over BRAIN's own
window, which :func:`vault.metrics.window_start` reproduces to four decimals.

Which Alphas this applies to is BRAIN's own call, read off its ``POWER_POOL_CORRELATION``
check (``vault.yields.is_power_pool``). Two rules then decide eligibility, both BRAIN's:

- Power Pool Correlation must be below :data:`POWER_POOL_CORRELATION_CEILING`.
- Above it, the Alpha needs a Sharpe at least :data:`POWER_POOL_ESCAPE` times that of **every**
  Alpha it collides with — not merely the one it correlates with most.

That second rule is stricter than the platform's own wording, which says "10% higher than the
most correlated Alpha". Measured: ``omLgk09k``, Sharpe 2.43, correlated 0.627 with a Sharpe
1.70 Alpha and 0.5541 with a Sharpe 2.62 one. The documented rule sets the bar at 1.87 and
admits it; BRAIN refused it, which only the 2.88 bar from the second collision explains.

Scope is the region, and only the region. Measured against BRAIN's own endpoint: a TOP3000
candidate is correlated against TOP500 and TOP2000 members alike, and a USA **D0** candidate
against the USA D1 pool. Delay and universe are not part of the bucket.
"""

from bisect import bisect_left
from typing import TYPE_CHECKING, Any

import numpy as np

from ..brain.platform import (
    POWER_POOL_CLASS,
    POWER_POOL_CORRELATION_CEILING,
    POWER_POOL_ESCAPE,
    REGULAR_CLASS,
)
from ..vault import metrics, yields
from ..vault.store import json_list
from ..vault.yields import checks_of

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from ..vault.store import PnlGrid


def is_power_pool(row: dict[str, Any] | None) -> bool:
    """Whether BRAIN treats this Alpha as Power Pool, from its stored checks column.

    The check is the only local signal there is. BRAIN assigns the ``Power Pool Alpha``
    classification on submission alone — measured, none of 29,739 unsubmitted Alphas carried
    it, while the check separated 38 of 60 sweep results and agreed with the classification
    on all 14 submitted ones.
    """
    return yields.is_power_pool(checks_of((row or {}).get("checks")))


def scope_of(row: dict[str, Any]) -> str | None:
    """The bucket a correlation is measured within: the region, nothing else."""
    region = row.get("region")
    return str(region) if region else None


def correlate(
    grid: PnlGrid,
    meta: dict[str, dict[str, Any]],
    candidates: Sequence[str],
    pool: Sequence[str],
    ceiling: float = POWER_POOL_CORRELATION_CEILING,
) -> list[dict[str, Any]]:
    """Each candidate against every pool member, with the verdict BRAIN would reach.

    Both lists are one scope's, ``grid`` their stored PnL (:meth:`AlphaVault.pnl_grid`). A
    candidate that is itself in the pool is never measured against itself. A member with no
    PnL stored still counts in ``against``, and while any has none the answer is
    ``unmeasured`` unless a collision already refuses it: the missing one could hide another.
    """
    at = {a: i for i, a in enumerate(grid[0])}
    left = [a for a in dict.fromkeys(candidates) if a in at]
    right = [p for p in dict.fromkeys(pool) if p in at]
    rho = np.full((len(left), len(right)), np.nan)
    if left and right:
        ends = _ends(grid, meta, at, [*left, *right])
        rho = _rho(grid, at, left, right, ends[: len(left)], max(ends[len(left) :]))
    row_of = {a: i for i, a in enumerate(left)}
    return [
        _judge(
            alpha_id,
            meta,
            [p for p in pool if p != alpha_id],
            {p: float(rho[row_of[alpha_id], j]) for j, p in enumerate(right)}
            if alpha_id in row_of
            else {},
            at,
            ceiling,
        )
        for alpha_id in candidates
    ]


def _ends(
    grid: PnlGrid, meta: dict[str, dict[str, Any]], at: dict[str, int], alpha_ids: list[str]
) -> list[date]:
    """The last day BRAIN counts for each Alpha: its stored series' last, plus closing days."""
    _, dates, pnl = grid
    traded = ~np.isnan(pnl[:, [at[a] for a in alpha_ids]])
    stored_last = [dates[i] for i in len(dates) - 1 - traded[::-1].argmax(axis=0)]
    # Closing days move where BRAIN's window ends, as ``portfolio.frame`` counts them, but are
    # never correlated: that is what makes these match BRAIN's own figures.
    return [
        metrics.with_closing([(day, 0.0, 0.0)], meta.get(a, {}))[-1][0]
        for a, day in zip(alpha_ids, stored_last, strict=True)
    ]


def _rho(
    grid: PnlGrid,
    at: dict[str, int],
    left: list[str],
    right: list[str],
    left_ends: list[date],
    pool_end: date,
) -> metrics.Floats:
    """``left`` against ``right``, each candidate over its own window: ending with its series
    or the pool's, whichever is later. One end for the whole batch made a figure depend on what
    else was asked with it."""
    _, dates, pnl = grid
    rho = np.full((len(left), len(right)), np.nan)
    if not (left and right):
        return rho
    by_start: dict[date, list[int]] = {}
    for k, end in enumerate(left_ends):
        by_start.setdefault(metrics.window_start(max(end, pool_end)), []).append(k)
    pooled = pnl[:, [at[p] for p in right]]
    for start, picked in by_start.items():
        since = bisect_left(dates, start)
        measured = pnl[since:, [at[left[k]] for k in picked]]
        rho[picked] = metrics.correlations(measured, pooled[since:])
    return rho


def _judge(
    alpha_id: str,
    meta: dict[str, dict[str, Any]],
    against: list[str],
    values: dict[str, float],
    stored: dict[str, int],
    ceiling: float,
) -> dict[str, Any]:
    """The verdict from the pairs measured, ``values`` by pool member. Only pairs both sides of
    which have a stored series can be measured; the rest still count in ``against``, so the row
    says what it was judged against."""
    usable = [(p, v) for p in against if (v := values.get(p)) is not None and not np.isnan(v)]
    found = _verdict(alpha_id, meta, usable, len(against), ceiling)
    if found["verdict"] != "blocked" and any(p not in stored for p in against):
        found["verdict"] = "unmeasured"
    return found


#: Verdicts an Alpha can be submitted on.
PASSING = frozenset({"clear", "beats"})


def submission_order(
    grid: PnlGrid,
    meta: dict[str, dict[str, Any]],
    candidates: Sequence[str],
    pools: dict[str, list[str]],
    ceiling: float = POWER_POOL_CORRELATION_CEILING,
) -> tuple[list[dict[str, Any]], dict[str, str], list[str]]:
    """Submit greedily, best After-Cost Sharpe first, without submitting anything: each pick
    joins its region's pool, and the region's other candidates are judged again against it.

    Returns the picks in order, each with its :func:`correlate` row at its turn; the
    candidates an earlier pick blocked, each with that pick; and those that could not be
    measured against a pick, having no stored series. ``pools`` is the submitted Power Pool
    by region.

    Each pick is measured against the rivals alone, their earlier pairs kept: the same answer
    as judging them against the whole pool again, unless the pick ends later than the pool,
    which moves every window and so measures the region afresh.
    """

    def rank(alpha_id: str) -> float:
        value = (meta.get(alpha_id) or {}).get("after_cost_5bps")
        return float(value) if value is not None else float("-inf")

    at = {a: i for i, a in enumerate(grid[0])}
    remaining = sorted(dict.fromkeys(candidates), key=lambda a: (-rank(a), a))
    region_of = {a: scope_of(meta.get(a) or {}) for a in remaining}
    pool = {region: list(ids) for region, ids in pools.items()}
    everyone = dict.fromkeys([*remaining, *(p for ids in pool.values() for p in ids)])
    stored = [a for a in everyone if a in at]
    end_of = dict(zip(stored, _ends(grid, meta, at, stored) if stored else [], strict=True))
    pool_end: dict[str, date] = {}
    values: dict[str, dict[str, float]] = {a: {} for a in remaining}

    def measure(region: str, rivals: list[str], members: list[str]) -> None:
        left = [a for a in rivals if a in at]
        right = [p for p in members if p in at]
        rho = _rho(grid, at, left, right, [end_of[a] for a in left], pool_end[region])
        for i, a in enumerate(left):
            values[a].update((p, float(rho[i, j])) for j, p in enumerate(right))

    def judge(alpha_id: str, region: str) -> dict[str, Any]:
        against = [p for p in pool.get(region, []) if p != alpha_id]
        return _judge(alpha_id, meta, against, values[alpha_id], at, ceiling)

    for region in {r for r in region_of.values() if r}:
        members = [p for p in pool.get(region, []) if p in at]
        if members:
            pool_end[region] = max(end_of[p] for p in members)
            measure(region, [a for a in remaining if region_of[a] == region], members)
    # Each candidate's row as last judged, so a pick reports the correlation it was taken at.
    judged = {a: judge(a, region) for a in remaining if (region := region_of[a])}
    # Only what could be submitted today starts out in the running.
    remaining = [a for a in remaining if judged.get(a, {}).get("verdict") in PASSING]
    picks: list[dict[str, Any]] = []
    blocked: dict[str, str] = {}
    unmeasured: list[str] = []
    while remaining:
        pick = remaining.pop(0)
        region = region_of[pick]
        picks.append(judged.get(pick, _row(pick, meta, _sharpe(meta.get(pick)))))
        if region is None:
            continue
        pool.setdefault(region, []).append(pick)
        rivals = [a for a in remaining if region_of[a] == region]
        if pick in at:
            if region not in pool_end or end_of[pick] > pool_end[region]:
                pool_end[region] = end_of[pick]
                measure(region, rivals, pool[region])
            else:
                measure(region, rivals, [pick])
        for alpha_id in rivals:
            row = judged[alpha_id] = judge(alpha_id, region)
            if row["verdict"] == "blocked":
                blocked[alpha_id] = pick
            elif row["verdict"] not in PASSING:
                unmeasured.append(alpha_id)
        out = set(blocked) | set(unmeasured)
        remaining = [a for a in remaining if a not in out]
    return picks, blocked, unmeasured


def self_pools(members: Sequence[dict[str, Any]]) -> dict[str, list[str]]:
    """By region, the submitted Alphas BRAIN's SELF_CORRELATION compares an Alpha with: all but
    those classed Power Pool alone, whether or not the Alpha compared is a Power Pool one.

    Measured: BRAIN's own answers list Regular Alphas against Power Pool candidates, and never
    one classed Power Pool alone against anything.
    """
    pools: dict[str, list[str]] = {}
    for row in members:
        classes = json_list(row.get("classifications"))
        if row.get("region") and (POWER_POOL_CLASS not in classes or REGULAR_CLASS in classes):
            pools.setdefault(str(row["region"]), []).append(str(row["alpha_id"]))
    return pools


def self_correlations(
    grid: PnlGrid, meta: dict[str, dict[str, Any]], members: Sequence[dict[str, Any]]
) -> dict[str, float | None]:
    """Each submitted Alpha's Self-Correlation as BRAIN's own Alpha page shows it today.

    Its highest correlation with every other submitted Alpha of its :func:`self_pools`.
    Measured against BRAIN on 20 Alphas across seven regions: identical to four decimals,
    ``None`` exactly where BRAIN has nothing to compare against.
    """
    return _by_region(grid, meta, members, self_pools(members))


def power_pool_correlations(
    grid: PnlGrid, meta: dict[str, dict[str, Any]], members: Sequence[dict[str, Any]]
) -> dict[str, float | None]:
    """Each submitted Alpha's Power Pool Correlation as BRAIN's own Alpha page shows it today.

    Its highest correlation with the other submitted Alphas of its region that BRAIN judges as
    Power Pool. Measured against BRAIN on 18 Alphas across eight regions, Power Pool or not:
    identical to four decimals, ``None`` exactly where BRAIN has nothing to compare against.
    """
    pools: dict[str, list[str]] = {}
    for row in members:
        alpha_id = str(row["alpha_id"])
        if is_power_pool(meta.get(alpha_id)):
            pools.setdefault(str(row.get("region")), []).append(alpha_id)
    return _by_region(grid, meta, members, pools)


def _by_region(
    grid: PnlGrid,
    meta: dict[str, dict[str, Any]],
    members: Sequence[dict[str, Any]],
    pools: dict[str, list[str]],
) -> dict[str, float | None]:
    """Each member's highest correlation with its region's pool: one measurement a region,
    since a member per call cost the square of the account's submissions."""
    by_region: dict[str, list[str]] = {}
    for row in members:
        by_region.setdefault(str(row.get("region")), []).append(str(row["alpha_id"]))
    return {
        found["alphaId"]: found["correlation"]
        for region, alpha_ids in by_region.items()
        for found in correlate(grid, meta, alpha_ids, pools.get(region, []))
    }


def _verdict(
    alpha_id: str,
    meta: dict[str, dict[str, Any]],
    measured: list[tuple[str, float]],
    against: int,
    ceiling: float = POWER_POOL_CORRELATION_CEILING,
) -> dict[str, Any]:
    """One candidate's answer: its highest correlation, and whether that refuses it."""
    sharpe = _sharpe(meta.get(alpha_id))
    if not measured:
        return _row(alpha_id, meta, sharpe) | {
            "correlation": None,
            "against": against,
            "closest": None,
            "closestSharpe": None,
            "needed": None,
            # Nothing to collide with is not the same as nothing measurable. With an empty
            # pool -- no Power Pool Alpha submitted in this region yet -- the ceiling cannot
            # be crossed, so the first Alpha in a region is eligible on this rule. Only a
            # pool that exists but shares too few trading days is genuinely unmeasured.
            "verdict": "unmeasured" if against else "clear",
        }
    closest, highest = max(measured, key=lambda pair: pair[1])
    closest_sharpe = _sharpe(meta.get(closest))
    # Every collision sets a bar; the Alpha has to clear the tallest. Taking only the closest
    # one lets an Alpha through on the strength of beating a weak neighbour while a stronger
    # Alpha it also collides with goes unanswered.
    bars = [
        found * POWER_POOL_ESCAPE
        for other, value in measured
        if value >= ceiling and (found := _sharpe(meta.get(other))) is not None
    ]
    # Rounded before comparing: Sharpes are two-decimal figures, so a true bar has three at
    # most, and 1.10 x 1.50 lands on 1.6500000000000001 -- short of an Alpha at exactly 1.65,
    # which BRAIN's "better by 10.0% or more" admits.
    needed = round(max(bars), 4) if bars else None
    if highest < ceiling:
        verdict = "clear"
    elif needed is not None and sharpe is not None and sharpe >= needed:
        verdict = "beats"
    else:
        verdict = "blocked"
    return _row(alpha_id, meta, sharpe) | {
        "correlation": round(highest, 4),
        "against": against,
        "closest": closest,
        "closestSharpe": closest_sharpe,
        # Only meaningful once the ceiling is crossed; null below it keeps the column honest.
        "needed": needed if highest >= ceiling else None,
        "verdict": verdict,
    }


def _row(alpha_id: str, meta: dict[str, dict[str, Any]], sharpe: float | None) -> dict[str, Any]:
    """What identifies a candidate, whatever its verdict turns out to be."""
    row = meta.get(alpha_id) or {}
    return {
        "alphaId": alpha_id,
        "sharpe": sharpe,
        "universe": row.get("universe"),
        "delay": row.get("delay"),
    }


def _sharpe(row: dict[str, Any] | None) -> float | None:
    value = (row or {}).get("sharpe")
    return float(value) if value is not None else None
