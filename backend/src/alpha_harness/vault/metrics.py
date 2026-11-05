"""BRAIN's performance figures, rebuilt from an alpha's ``pnl`` and ``turnover`` recordsets.

Measured against 36 alphas across nine regions; each figure matches the platform's to the
decimals it shows, apart from $1 PnL differences that come from the recordsets being rounded.
What that measurement settled, where it departs from the documentation:

- A year is **250** days, not the documented 252, for both Sharpe and returns.
- Daily PnL is the difference of the cumulative ``pnl`` recordset. The ``daily-pnl`` recordset
  is rounded separately and drifts from the platform's own figures.
- Each ``turnover`` row is stamped one trading day early. The day the book is first built
  trades it whole (1.0) and can fall off that shift, and a gap in the series after trading
  began is the book being sold whole (1.0 again).
- Drawdown's running peak starts at the first day's cumulative PnL, not at zero.
- Fitness is computed from the Sharpe as displayed, rounded to two places.

- BRAIN's IS and test blocks include final days no recordset carries (see
  :func:`closing_days`). The yearly rows leave them out.
- For some Alphas the turnover recordset is not the turnover BRAIN's figures use; its yearly
  figures are, so turnover is scaled to them (see :func:`calibrate`).
"""

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt

from ..brain.platform import (
    BOOK_SIZE,
    CORRELATION_WINDOW_YEARS,
    FITNESS_TURNOVER_FLOOR,
    TRADING_DAYS,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

type Floats = npt.NDArray[np.float64]
type Bools = npt.NDArray[np.bool_]

#: Days two Alphas must share before their correlation means anything.
MIN_OVERLAP = 250
#: Half the last decimal of BRAIN's four-decimal turnover.
ROUNDING = 0.00005


@dataclass(frozen=True, slots=True)
class Stats:
    pnl: float
    returns: float
    turnover: float
    drawdown: float
    margin: float
    #: ``None`` below two days, where a spread is undefined.
    sharpe: float | None
    fitness: float | None
    days: int


def _day(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        return date.fromisoformat(value[:10])
    return None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def daily_rows(
    pnl_rows: Sequence[dict[str, Any]], turnover_rows: Sequence[dict[str, Any]]
) -> list[tuple[date, float, float]]:
    """``(date, pnl, turnover)`` per trading day, from the two recordsets' rows by column name."""
    return with_turnover(daily_pnl(pnl_rows), turnover_rows)


def daily_pnl(pnl_rows: Sequence[dict[str, Any]]) -> list[tuple[date, float]]:
    """``(date, pnl)`` per trading day, from the cumulative ``pnl`` recordset."""
    out: list[tuple[date, float]] = []
    previous_cum = 0.0
    for row in pnl_rows:
        day, cum = _day(row.get("date")), _number(row.get("pnl"))
        if day is not None and cum is not None:
            out.append((day, cum - previous_cum))
            previous_cum = cum
    return out


def with_turnover(
    days: Sequence[tuple[date, float]], turnover_rows: Sequence[dict[str, Any]]
) -> list[tuple[date, float, float]]:
    """Each day of :func:`daily_pnl` with the turnover traded on it."""
    if not days:
        return []
    recorded = {
        day: _number(row.get("turnover"))
        for row in turnover_rows
        if (day := _day(row.get("date"))) is not None
    }
    first_value = next((v for v in recorded.values() if v is not None), None)

    out: list[tuple[date, float, float]] = []
    holding = False
    for i, (day, pnl) in enumerate(days):
        if i == 0:
            built = recorded.get(day) is not None and first_value != 1.0
            turnover = 1.0 if built else 0.0
            holding = built
        else:
            value = recorded.get(days[i - 1][0])
            if value is None:
                turnover = 1.0 if holding else 0.0
                holding = False
            else:
                turnover = value
                holding = True
        out.append((day, pnl, turnover))
    return out


def calibrate(
    days: Sequence[tuple[date, float, float]],
    yearly: Sequence[dict[str, Any]],
    split: date | None,
) -> list[tuple[date, float, float]]:
    """Scale each ``yearly-stats`` segment's turnover to the figure BRAIN reports for it.

    For delay-0 Alphas with a group (or no) neutralization, and for CHN and GLB, the turnover
    recordset differs from the turnover BRAIN's own figures use, by 0.1 to 20%, spread through
    every year and not recoverable day by day. Its yearly figures are exact, so each year (the
    split year cut at the split day, as BRAIN cuts it) is scaled to match. Measured on 155 Alphas
    across 15 region and delay pairs: IS, train and test then match BRAIN on 98% of figures,
    the rest four-decimal rounding.
    """
    turnover = [t for _, _, t in days]
    for row in yearly:
        target = _number(row.get("turnover"))
        year = int(str(row.get("year")))
        if target is None:
            continue
        test = row.get("stage") == "TEST"
        segment = [
            i
            for i, (d, _, _) in enumerate(days)
            if d.year == year and (split is None or year != split.year or (d >= split) == test)
        ]
        have = sum(turnover[i] for i in segment)
        # BRAIN rounds its figure to four decimals: a segment already within that is exact, and
        # scaling it to the rounded figure would only add the rounding.
        if segment and have > 0 and abs(have / len(segment) - target) > ROUNDING:
            for i in segment:
                turnover[i] *= target * len(segment) / have
    return [(d, p, t) for (d, p, _), t in zip(days, turnover, strict=True)]


def closing_days(
    days: Sequence[tuple[date, float, float]],
    *,
    end: date,
    is_pnl: float | None,
    split: date | None,
    test_turnover: float | None,
) -> list[tuple[date, float, float]]:
    """The final days BRAIN counts in an Alpha's IS and test blocks but exports in no recordset:
    every trading day between the last recordset row and the simulation's end, plus one more.

    Only their total is known, so it is split evenly. PnL comes from the reported IS PnL;
    turnover from the reported test turnover, a mean that includes them, so it is only as exact
    as that figure's four decimals allow.
    """
    if is_pnl is None or not days or end <= days[-1][0]:
        return []
    last = days[-1][0]
    between = [
        d
        for d in (last + timedelta(days=k) for k in range(1, (end - last).days + 1))
        if d.weekday() < 5
    ]
    # The extra day falls on the end date, or on the next weekday when the end is itself one.
    after = end + timedelta(days=1)
    while after.weekday() >= 5:
        after += timedelta(days=1)
    stamps = [*between, after if end in between else end]
    count = len(stamps)
    pnl = (is_pnl - sum(p for _, p, _ in days)) / count
    turnover = 0.0
    if split is not None and test_turnover is not None:
        tested = [t for d, _, t in days if d >= split]
        total = test_turnover * (len(tested) + count) - sum(tested)
        turnover = min(2.0, max(0.0, total / count))
    return [(d, pnl, turnover) for d in stamps]


def with_closing(
    rows: list[tuple[date, float, float]], info: dict[str, Any]
) -> list[tuple[date, float, float]]:
    """An Alpha's recordset days, oldest first, plus its :func:`closing_days`. ``info`` is its
    stored row: ``end_date``, ``is_pnl``, ``test_start`` and ``test_turnover``."""
    end = info.get("end_date")
    if not isinstance(end, date):
        return rows
    return [
        *rows,
        *closing_days(
            rows,
            end=end,
            is_pnl=info.get("is_pnl"),
            split=info.get("test_start"),
            test_turnover=info.get("test_turnover"),
        ),
    ]


def stats(pnl: Floats, turnover: Floats, *, trim: bool = True) -> Stats | None:
    """The platform's stat block over these days. ``trim`` drops the idle days before the first
    trade, as BRAIN's IS, train and test blocks do; its yearly rows keep them."""
    if trim:
        active = np.flatnonzero((pnl != 0) | (turnover != 0))
        if not active.size:
            return None
        pnl, turnover = pnl[active[0] :], turnover[active[0] :]
    days = int(pnl.size)
    if not days:
        return None

    total = float(pnl.sum())
    traded = float(turnover.sum())
    returns = float(pnl.mean()) * TRADING_DAYS / (BOOK_SIZE / 2)
    mean_turnover = traded / days
    cum = np.cumsum(pnl)
    drawdown = float((np.maximum.accumulate(cum) - cum).max()) / (BOOK_SIZE / 2)
    margin = total / (traded * BOOK_SIZE) if traded else 0.0

    spread = float(pnl.std(ddof=1)) if days > 1 else 0.0
    sharpe = float(pnl.mean()) / spread * math.sqrt(TRADING_DAYS) if spread > 0 else None
    fitness = (
        round(sharpe, 2) * math.sqrt(abs(returns) / max(mean_turnover, FITNESS_TURNOVER_FLOOR))
        if sharpe is not None
        else None
    )
    return Stats(total, returns, mean_turnover, drawdown, margin, sharpe, fitness, days)


def k_ratio(pnl: Floats, turnover: Floats) -> float | None:
    """Kestner's K-ratio, 2013 form: the slope of a line fitted to cumulative PnL against the
    day number, over its standard error times the number of days, annualized by the square
    root of BRAIN's 250 trading days.

    Rewards steady growth over the same total earned in a few jumps, and unlike the 1996 and
    2003 forms does not depend on the length of the history or its sampling. From the first
    trade, as every other figure here.
    """
    active = np.flatnonzero((pnl != 0) | (turnover != 0))
    if not active.size:
        return None
    y = np.cumsum(pnl[active[0] :])
    n = y.size
    if n < 3:
        return None
    x = np.arange(1, n + 1, dtype=float)
    dx = x - x.mean()
    slope = float(dx @ (y - y.mean())) / float(dx @ dx)
    residual = y - y.mean() - slope * dx
    spread = float(residual @ residual) / (n - 2)
    error = math.sqrt(spread / float(dx @ dx))
    return slope / (error * n) * math.sqrt(TRADING_DAYS) if error > 0 else None


def windows(
    dates: Sequence[date] | np.ndarray, pnl: Floats, turnover: Floats, split: date | None
) -> dict[str, Stats | None]:
    """IS, train and test blocks. Train and test both include the split day, as BRAIN's do."""
    stamps = np.array(dates, dtype="datetime64[D]")
    out: dict[str, Stats | None] = {"in_sample": stats(pnl, turnover)}
    if split is None:
        out["train"] = out["test"] = None
        return out
    cut = np.datetime64(split, "D")
    train, test = stamps <= cut, stamps >= cut
    out["train"] = stats(pnl[train], turnover[train])
    out["test"] = stats(pnl[test], turnover[test])
    return out


def yearly(
    dates: Sequence[date], pnl: Floats, turnover: Floats, split: date | None
) -> list[tuple[int, str, Stats]]:
    """BRAIN's yearly rows. The split year is cut in two at the split day."""

    def stage(day: date) -> str:
        if split is None:
            return "IS"
        return "TEST" if day >= split else "TRAIN"

    groups = [(d.year, stage(d)) for d in dates]
    rows: list[tuple[int, str, Stats]] = []
    for year, name in dict.fromkeys(groups):
        mask = np.array([g == (year, name) for g in groups])
        found = stats(pnl[mask], turnover[mask], trim=False)
        if found is not None:
            rows.append((year, name, found))
    return rows


def after_cost(pnl: Floats, turnover: Floats, bps: float) -> Floats:
    """Daily PnL less an estimated trading cost of ``bps`` on every dollar traded."""
    return pnl - turnover * BOOK_SIZE * bps / 10_000


#: The trading cost every after-cost figure is quoted at, in basis points of the dollars
#: traded. The screens that let it be changed default to the same number.
DEFAULT_COST_BPS = 5.0


def after_cost_sharpe(pnl: Floats, turnover: Floats) -> float | None:
    """The Sharpe once :data:`DEFAULT_COST_BPS` is charged against each day's turnover,
    annualized over BRAIN's 250 trading days like every other Sharpe here.

    The cost is charged per day against *that day's* turnover — never an average cost off the
    gross Sharpe, which flatters an Alpha that trades unevenly. Straight through :func:`stats`,
    so it is the same arithmetic and the same idle-day trim as every other Sharpe here.
    Charged at :data:`DEFAULT_COST_BPS`.
    """
    found = stats(after_cost(pnl, turnover, DEFAULT_COST_BPS), turnover)
    return found.sharpe if found else None


def window_start(last: date) -> date:
    """First day BRAIN correlates over: 1 January, ``CORRELATION_WINDOW_YEARS`` calendar years
    back including ``last``'s own. Whole years, not four years back from ``last``: measured,
    this reproduces BRAIN's Power Pool correlations to four decimals on all eight pairs checked,
    and the rolling window was off by up to 0.0005."""
    return date(last.year - CORRELATION_WINDOW_YEARS + 1, 1, 1)


def correlations(m: Floats, against: Floats | None = None) -> Floats:
    """Pairwise Pearson correlation, each pair measured only on the days both Alphas traded.

    One masked matrix operation rather than ``labs.ga.correlation`` per pair: that is the same
    arithmetic, but 278 Alphas is 38,503 pairs and the loop costs half a minute.

    Every sum a pair needs is a Gram matrix, so all 38,503 pairs come out of five products at
    once. ``z`` is exactly zero where an Alpha did not trade, which is what lets a pair's sums
    carry only the *other* Alpha's mask; only the day count needs both. Measured 19x faster
    than the same arithmetic walked column by column, and identical to the last bit.

    ``against``, on the same calendar, asks for one block only: each column of ``m`` against
    each of its own. A sweep judged against a pool of fifty never reads its own pairs, which
    at 2,500 Alphas were 98% of a second's work and half a gigabyte.
    """
    # Float: ``@`` on two boolean arrays is an OR-of-ANDs, which would turn ``count`` into
    # "did they ever overlap" and quietly retire the whole correlation ceiling.
    have = (~np.isnan(m)).astype(np.float64)
    z = np.nan_to_num(m)
    zz = z * z
    if against is None:
        have_r, z_r, zz_r = have, z, zz
    else:
        have_r = (~np.isnan(against)).astype(np.float64)
        z_r = np.nan_to_num(against)
        zz_r = z_r * z_r

    count = have.T @ have_r
    sum_l = z.T @ have_r
    sum_r = sum_l.T if against is None else have.T @ z_r
    square_l = zz.T @ have_r
    square_r = square_l.T if against is None else have.T @ zz_r
    safe = np.maximum(count, 1.0)

    cov = (z.T @ z_r) - sum_l * sum_r / safe
    dev_l = np.sqrt(np.maximum(square_l - sum_l**2 / safe, 0.0))
    dev_r = np.sqrt(np.maximum(square_r - sum_r**2 / safe, 0.0))
    usable = (dev_l > 0) & (dev_r > 0) & (count >= MIN_OVERLAP)
    # Cancellation in the one-pass moments can land a correlation a few ulps outside [-1, 1],
    # which is harmless arithmetic and an alarming thing to print next to a 0.50 ceiling.
    out = np.clip(np.where(usable, cov / np.maximum(dev_l * dev_r, 1e-12), np.nan), -1.0, 1.0)
    if against is None:
        np.fill_diagonal(out, 1.0)
    return out
