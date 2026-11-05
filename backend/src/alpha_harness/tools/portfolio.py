"""The Portfolio page: submitted Alphas combined at equal weight.

Equal weight is what BRAIN itself does. Its own pool figure (``before-and-after-performance``)
is the mean of the members' daily PnL and turnover over the days it exported, leaving out the
final days each Alpha's own figures count. Filtered to one partition (region, delay, Power Pool
or Regular), this reproduces it to the dollar, measured, in total and every yearly row.

Across regions the calendars differ, so the headline puts every Alpha on the union of their
trading days, counting a day an Alpha did not trade as zero: once it has started, each Alpha
keeps an equal share of capital whether or not its market was open.
"""

import asyncio
import itertools
import math
from bisect import bisect_left
from collections import Counter
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

import numpy as np

from ..vault import metrics

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence
    from datetime import date

    from ..vault.metrics import Bools, Floats, Stats
    from ..vault.store import AlphaVault, PnlGrid

    type Framed = tuple[list[date], Floats, Floats, Bools]


def _blocks(found: dict[str, Stats | None]) -> dict[str, Any]:
    return {name: None if block is None else asdict(block) for name, block in found.items()}


def _split(meta: dict[str, dict[str, Any]], alpha_ids: list[str]) -> date | None:
    """The test start most members share. Every Alpha simulated with the default settings
    has the same one; a mixed pool takes the majority rather than failing."""
    starts = Counter(meta[a]["test_start"] for a in alpha_ids if meta.get(a, {}).get("test_start"))
    return starts.most_common(1)[0][0] if starts else None


def compute(
    series: dict[str, dict[date, tuple[float, float]]],
    meta: dict[str, dict[str, Any]],
    alpha_ids: list[str],
    cost_bps: float,
) -> dict[str, Any]:
    """Everything the Portfolio page shows for these Alphas. Ids without a series are left
    to the caller to report."""
    ids = [a for a in dict.fromkeys(alpha_ids) if series.get(a)]
    # Most recently submitted first, so the correlation grid reads newest to oldest.
    ids.sort(key=lambda a: str(meta.get(a, {}).get("date_submitted") or ""), reverse=True)
    if not ids:
        return {
            "alphas": 0,
            "ids": [],
            "test_start": None,
            "stats": None,
            "after_cost": None,
            "yearly": [],
            "dates": [],
            "curve": [],
            "periods": None,
            "after_cost_curve": [],
            "correlation": [],
            "top_pairs": [],
            "measured_pairs": 0,
            "highest": None,
        }
    framed = frame(series, meta, ids)
    dates, pnl, turnover = exported(*framed)
    filled = np.nan_to_num(pnl)
    count = len(ids)
    # BRAIN averages over the Alphas already trading that day: one that has not made its
    # first trade yet is left out of the count rather than diluting the others with zeros.
    started = np.maximum.accumulate((filled != 0) | (turnover != 0), axis=0)
    live = np.maximum(started.sum(axis=1), 1)
    book = filled.sum(axis=1) / live
    traded = turnover.sum(axis=1) / live
    split = _split(meta, ids)
    net = metrics.after_cost(book, traded, cost_bps)

    return {
        "alphas": count,
        "ids": ids,
        "test_start": split.isoformat() if split else None,
        "stats": _blocks(metrics.windows(dates, book, traded, split)),
        "after_cost": _blocks(metrics.windows(dates, net, traded, split)),
        # Whole calendar years, without the final days, as BRAIN's yearly rows are.
        "yearly": _yearly(dates, book, traded),
        "dates": [d.isoformat() for d in dates],
        "curve": np.cumsum(book).tolist(),
        "after_cost_curve": np.cumsum(net).tolist(),
        "periods": _periods(dates, book, traded, split),
        # Without the final days, though BRAIN's window ends with them, as for every pair here:
        # its correlations match to four decimals that way.
        **_correlation(ids, dates, pnl, metrics.window_start(framed[0][-1])),
    }


def highest_correlations(ids: list[str], framed: Framed) -> dict[str, float | None]:
    """Each Alpha's highest correlation with any other of ``ids``, measured as the grid below
    measures it. ``framed`` is their :func:`frame`."""
    if len(ids) < 2:
        return dict.fromkeys(ids)
    dates, pnl, _, closing = framed
    since = metrics.window_start(dates[-1])
    rho = metrics.correlations(np.where(closing, np.nan, pnl)[bisect_left(dates, since) :])
    np.fill_diagonal(rho, np.nan)
    return {
        a: None if np.isnan(row).all() else float(np.nanmax(row))
        for a, row in zip(ids, rho, strict=True)
    }


#: Alphas whose series :func:`net_figures_of` holds in memory at once.
NET_CHUNK = 200


async def net_figures_of(
    alphas: AlphaVault, alpha_ids: Iterable[str], bps: float
) -> dict[str, dict[str, float | None]]:
    """:func:`net_figures` for these Alphas, read from the vault a slice at a time: 3,000
    series at once peaked at 3 GB, against 0.5 GB and a third of the time in slices of 200."""
    found: dict[str, dict[str, float | None]] = {}
    for chunk in itertools.batched(dict.fromkeys(alpha_ids), NET_CHUNK, strict=False):
        series = await alphas.series(list(chunk))
        meta = await alphas.by_ids(list(series))
        found |= await asyncio.to_thread(net_figures, series, meta, bps)
    return found


def net_figures(
    series: dict[str, dict[date, tuple[float, float]]],
    meta: dict[str, dict[str, Any]],
    bps: float,
) -> dict[str, dict[str, float | None]]:
    """Each Alpha's figures recomputed on its PnL net of ``bps``, with the formulas that give
    BRAIN's own gross figures: in sample, and Sharpe in its train and test years. At zero bps,
    the gross figures BRAIN does not report, such as the K-ratio."""
    found: dict[str, dict[str, float | None]] = {}
    for alpha_id, days in series.items():
        info = meta.get(alpha_id, {})
        rows = metrics.with_closing([(d, p, t) for d, (p, t) in sorted(days.items())], info)
        if not rows:
            continue
        turnover = np.array([t for _, _, t in rows])
        pnl = metrics.after_cost(np.array([p for _, p, _ in rows]), turnover, bps)
        found[alpha_id] = _figures([d for d, _, _ in rows], pnl, turnover, info)
    return found


def framed_grid(ids: list[str], framed: Framed, current: set[str], extra: PnlGrid) -> PnlGrid:
    """:meth:`AlphaVault.pnl_grid` from these Alphas' :func:`frame` at no cost, without
    reading their PnL again: the columns of those whose stored series is ``current``, with
    ``extra`` beside them, the Alphas holding a PnL and no turnover, which a frame leaves out."""
    dates, pnl, _ = exported(*framed)
    kept = [col for col, a in enumerate(ids) if a in current]
    names = [ids[col] for col in kept]
    pnl = pnl[:, kept]
    extra_ids, extra_dates, extra_pnl = extra
    if extra_ids:
        calendar = sorted(set(dates) | set(extra_dates))
        at = {d: i for i, d in enumerate(calendar)}
        merged = np.full((len(calendar), len(names) + len(extra_ids)), np.nan)
        merged[[at[d] for d in dates], : len(names)] = pnl
        merged[[at[d] for d in extra_dates], len(names) :] = extra_pnl
        dates, pnl, names = calendar, merged, [*names, *extra_ids]
    # Only days one of them traded, as the read would have them.
    traded = ~np.isnan(pnl).all(axis=1)
    return names, [d for d, t in zip(dates, traded, strict=True) if t], pnl[traded]


def framed_figures(
    ids: list[str], framed: Framed, meta: dict[str, dict[str, Any]]
) -> dict[str, dict[str, float | None]]:
    """:func:`net_figures` from these Alphas' :func:`frame`, whose columns already hold each
    one's days, closing ones included, net of the frame's cost: read off as slices, rather
    than rebuilt day by day from Python lists. Measured, a fraction of the time."""
    dates, pnl, turnover, _ = framed
    stamps = np.array(dates, dtype="datetime64[D]")
    found: dict[str, dict[str, float | None]] = {}
    for col, alpha_id in enumerate(ids):
        held = ~np.isnan(pnl[:, col])
        if held.any():
            found[alpha_id] = _figures(
                stamps[held], pnl[held, col], turnover[held, col], meta.get(alpha_id, {})
            )
    return found


def _figures(
    dates: Sequence[date] | np.ndarray, pnl: Floats, turnover: Floats, info: dict[str, Any]
) -> dict[str, float | None]:
    blocks = metrics.windows(dates, pnl, turnover, info.get("test_start"))
    whole, train, test = blocks["in_sample"], blocks["train"], blocks["test"]
    return {
        "sharpe": whole.sharpe if whole else None,
        "fitness": whole.fitness if whole else None,
        "returns": whole.returns if whole else None,
        "drawdown": whole.drawdown if whole else None,
        "margin": whole.margin if whole else None,
        "train_sharpe": train.sharpe if train else None,
        "test_sharpe": test.sharpe if test else None,
        "k_ratio": metrics.k_ratio(pnl, turnover),
    }


#: Alphas the leave-one-out figures work through at once: one array operation per block
#: rather than a Python step per Alpha, without a days-by-Alphas copy of every intermediate.
LEAVE_ONE_OUT_BLOCK = 256


def _blocks_of(count: int) -> Iterator[slice]:
    for start in range(0, count, LEAVE_ONE_OUT_BLOCK):
        yield slice(start, min(start + LEAVE_ONE_OUT_BLOCK, count))


def _sharpes(book: Floats, moved: Floats) -> Floats:
    """:func:`metrics.stats`'s Sharpe of each column, NaN where it has none: trimmed to the
    column's first trade, as ``stats`` trims."""
    active = (book != 0) | (moved != 0)
    first = np.where(active.any(axis=0), active.argmax(axis=0), len(book))
    valid = np.arange(len(book))[:, None] >= first[None, :]
    days = valid.sum(axis=0)
    mean = np.where(valid, book, 0.0).sum(axis=0) / np.maximum(days, 1)
    squares = np.where(valid, (book - mean) ** 2, 0.0).sum(axis=0)
    spread = np.sqrt(squares / np.maximum(days - 1, 1))
    good = (days > 1) & (spread > 0)
    return np.where(good, mean / np.where(good, spread, 1.0), np.nan) * math.sqrt(
        metrics.TRADING_DAYS
    )


def incremental_sharpes(ids: list[str], framed: Framed) -> dict[str, float | None]:
    """Each Alpha's Incremental Sharpe: the in-sample Sharpe of the equal-weighted Portfolio of
    ``ids`` with it, less the Sharpe of the same Portfolio without it. ``framed`` is their
    :func:`frame`.

    Both are combined exactly as :func:`compute` combines them.
    """
    if len(ids) < 2:
        return dict.fromkeys(ids)
    _, pnl, turnover = exported(*framed)
    filled = np.nan_to_num(pnl)
    started = np.maximum.accumulate((filled != 0) | (turnover != 0), axis=0)
    total, traded, live = filled.sum(axis=1), turnover.sum(axis=1), started.sum(axis=1)
    held = np.maximum(live, 1)
    whole = metrics.stats(total / held, traded / held)
    if whole is None or whole.sharpe is None:
        return dict.fromkeys(ids)
    found: dict[str, float | None] = {}
    for block in _blocks_of(len(ids)):
        held = np.maximum(live[:, None] - started[:, block], 1)
        without = _sharpes(
            (total[:, None] - filled[:, block]) / held,
            (traded[:, None] - turnover[:, block]) / held,
        )
        for alpha_id, sharpe in zip(ids[block], without.tolist(), strict=True):
            found[alpha_id] = None if math.isnan(sharpe) else whole.sharpe - sharpe
    return found


def portfolio_correlations(ids: list[str], framed: Framed) -> dict[str, float | None]:
    """Each Alpha's correlation with the equal-weighted Portfolio of every *other* Alpha in
    ``ids``: the Portfolio as it stands without it. ``framed`` is their :func:`frame`.

    The rest are combined as :func:`compute` combines them, averaged over those already
    trading that day, and correlated with the Alpha's own daily PnL over BRAIN's correlation
    window, on the days BRAIN exported, as every correlation here is measured.
    """
    if len(ids) < 2:
        return dict.fromkeys(ids)
    # BRAIN's window ends on the last day it counts, rebuilt or not, as for every pair here.
    start = metrics.window_start(framed[0][-1])
    dates, pnl, turnover = exported(*framed)
    filled = np.nan_to_num(pnl)
    started = np.maximum.accumulate((filled != 0) | (turnover != 0), axis=0)
    total, live = filled.sum(axis=1), started.sum(axis=1)
    since = bisect_left(dates, start)
    found: dict[str, float | None] = {}
    for block in _blocks_of(len(ids)):
        own = pnl[since:, block]
        others = live[since:, None] - started[since:, block]
        rest = np.full(own.shape, np.nan)
        np.divide(total[since:, None] - filled[since:, block], others, out=rest, where=others > 0)
        keep = ~np.isnan(own) & ~np.isnan(rest)
        days = keep.sum(axis=0)
        x = np.where(keep, own, 0.0)
        y = np.where(keep, rest, 0.0)
        dx = np.where(keep, x - x.sum(axis=0) / np.maximum(days, 1), 0.0)
        dy = np.where(keep, y - y.sum(axis=0) / np.maximum(days, 1), 0.0)
        xx, yy = (dx * dx).sum(axis=0), (dy * dy).sum(axis=0)
        measurable = (days >= metrics.MIN_OVERLAP) & (xx > 0) & (yy > 0)
        scale = np.sqrt(np.where(measurable, xx * yy, 1.0))
        correlation = np.clip((dx * dy).sum(axis=0) / scale, -1.0, 1.0)
        for alpha_id, ok, value in zip(
            ids[block], measurable.tolist(), correlation.tolist(), strict=True
        ):
            found[alpha_id] = value if ok else None
    return found


def exported(
    dates: list[date], pnl: Floats, turnover: Floats, closing: Bools
) -> tuple[list[date], Floats, Floats]:
    """A :func:`frame` cut to the days BRAIN exported. Its own pool figures leave out the final
    days it counts in each Alpha's own: measured, the pool's PnL matches to the dollar only
    without them."""
    pnl = np.where(closing, np.nan, pnl)
    turnover = np.where(closing, 0.0, turnover)
    keep = ~np.isnan(pnl).all(axis=1)
    kept = [d for d, k in zip(dates, keep, strict=True) if k]
    return kept, pnl[keep], turnover[keep]


def frame(
    series: dict[str, dict[date, tuple[float, float]]],
    meta: dict[str, dict[str, Any]],
    ids: list[str],
    bps: float = 0.0,
) -> tuple[list[date], Floats, Floats, Bools]:
    """These Alphas laid out day by day: the calendar, PnL, turnover, and which cells are
    closing days. With ``bps``, PnL is net of that cost on each day's turnover.

    The cost comes off only once the closing days are rebuilt: they are BRAIN's gross IS PnL
    less the stored days, so a series netted beforehand would pour every day's cost back into
    them as one spike.

    A cell is NaN where the Alpha has no row for that day. ``closing`` marks the final days
    rebuilt from the Alpha's own figures rather than exported by BRAIN; correlations leave
    them out, which is what makes them match BRAIN's to four decimals.
    """
    # Each Alpha's stored days, oldest first, plus the final days BRAIN counts in its IS and
    # test figures but exports in no recordset (see :func:`metrics.closing_days`).
    days = {
        a: metrics.with_closing(
            [(d, p, t) for d, (p, t) in sorted(series[a].items())], meta.get(a, {})
        )
        for a in ids
    }
    dates = sorted({d for a in ids for d, _, _ in days[a]})
    at = {d: i for i, d in enumerate(dates)}
    pnl = np.full((len(dates), len(ids)), np.nan)
    turnover = np.zeros((len(dates), len(ids)))
    closing = np.zeros((len(dates), len(ids)), dtype=bool)
    for col, alpha_id in enumerate(ids):
        for day, value, traded in days[alpha_id]:
            pnl[at[day], col] = value
            turnover[at[day], col] = traded
            closing[at[day], col] = day not in series[alpha_id]
    if bps:
        pnl = metrics.after_cost(pnl, turnover, bps)
    return dates, pnl, turnover, closing


def _yearly(dates: list[date], book: Floats, traded: Floats) -> list[dict[str, Any]]:
    return [
        {"year": year} | asdict(found)
        for year, _, found in metrics.yearly(dates, book, traded, None)
    ]


def _periods(
    dates: list[date], book: Floats, traded: Floats, split: date | None
) -> dict[str, dict[str, str] | None]:
    """First and last day of each block, from the first trade on as the stats count them."""
    active = np.flatnonzero((book != 0) | (traded != 0))
    start = dates[int(active[0])] if active.size else dates[0]
    end = dates[-1]

    def span(first: date, last: date) -> dict[str, str]:
        return {"start": first.isoformat(), "end": last.isoformat()}

    return {
        "in_sample": span(start, end),
        "train": span(start, split) if split and start <= split else None,
        "test": span(max(start, split), end) if split and split <= end else None,
    }


#: Past this many Alphas a full grid is too large to send and draw: only the
#: most correlated pairs are returned.
GRID_LIMIT = 150
TOP_PAIRS = 20


def _correlation(ids: list[str], dates: list[date], pnl: Floats, since: date) -> dict[str, Any]:
    """Pairwise daily PnL correlation from ``since``, BRAIN's window, and the pair that collides
    most. The full grid only up to ``GRID_LIMIT`` Alphas; past it, the ``TOP_PAIRS`` most
    correlated, so the answer does not grow with the square of them."""
    rho = metrics.correlations(pnl[bisect_left(dates, since) :])
    left, right = np.triu_indices(len(ids), k=1)
    measured = ~np.isnan(rho[left, right])
    # ``None`` rather than a number when no pair shared enough days: zero would read as
    # "perfectly uncorrelated", the most reassuring thing a failed measurement could claim.
    highest = lowest = None
    if measured.any():
        ends: list[dict[str, Any]] = []
        for k in (int(np.nanargmax(rho[left, right])), int(np.nanargmin(rho[left, right]))):
            a, b = int(left[k]), int(right[k])
            ends.append({"a": ids[a], "b": ids[b], "correlation": float(rho[a, b])})
        highest, lowest = ends
    if len(ids) <= GRID_LIMIT:
        matrix = [[None if np.isnan(v) else round(float(v), 4) for v in row] for row in rho]
        return {
            "correlation": matrix,
            "top_pairs": [],
            "measured_pairs": int(measured.sum()),
            "highest": highest,
            "lowest": lowest,
        }
    left, right = left[measured], right[measured]
    values = rho[left, right]
    top = np.argsort(-values, kind="stable")[:TOP_PAIRS]
    return {
        "correlation": [],
        "top_pairs": [
            {"a": ids[left[k]], "b": ids[right[k]], "correlation": round(float(values[k]), 4)}
            for k in top
        ],
        "measured_pairs": int(values.size),
        "highest": highest,
        "lowest": lowest,
    }
