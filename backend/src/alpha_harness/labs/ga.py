"""Evolution Lab: breed new Alphas from seed Alphas, scored on the train years only.

A hybrid of the research notes' gene GA and tree GP, because seeds are arbitrary expressions:

* **Genes** are what a mutation may change without changing the shape: an operator for a
  peer taking the same inputs, a lookback for a neighbouring economic window, a field for
  another in its dataset, a group for another group, and the neutralization and decay.
* **Crossover** is uniform, gene by gene, when two parents share a shape; otherwise a call
  of one is grafted into the other, never growing past the larger parent.

Every child is simulated holding the last two years out as a test and scored on Fitness
over the first eight, which BRAIN reports in the ``train`` block. Parents are the best of
everything scored so far, kept apart by the correlation of their daily PnL over the train
years.

In the all-regions market every child is a region-agnostic simulation, and each result is
scored as its second-best region (see :func:`..vault.store.family_row`): BRAIN submits one
only when two regions pass.
"""

import asyncio
import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
import structlog
from sqlalchemy import String, func, select, type_coerce

from ..brain.platform import (
    IGNORED_CHECKS,
    REGION_AGNOSTIC_REGION,
    TEST_PERIOD,
    UNCOUNTED_OPERATORS,
)
from ..brain.schemas import SimulationRequest, SimulationSettings
from ..db.models import Study, Trial, TrialState
from ..vault.metrics import MIN_OVERLAP
from ..vault.store import EVOLVABLE_TYPES, SEEDABLE
from ..vault.yields import SUBMITTED, checks_of, is_submitted, limits_of
from . import search
from .fastexpr import (
    Node,
    OperatorInfo,
    ParseError,
    Path,
    graft,
    graft_sites,
    grafts,
    guarded,
    node_at,
    operator_count,
    operator_table,
    parse,
    relabel,
    render,
    validate,
    walk_with_parents,
)
from .objectives import FAILURE
from .params import EvolutionParams, params_of

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Awaitable, Callable
    from datetime import date

    from ..db.duck import Catalog
    from .study import Optimizer

log = structlog.get_logger(__name__)

#: The economic windows from the research notes, a week to a year.
WINDOWS = (5, 10, 21, 42, 63, 126, 252)
ARITHMETIC = ("+", "-", "*", "/")
MUTATION_RATE = 0.05
#: Children spent without a new best before a task stops early, from the research notes.
PATIENCE = 1_000
#: Candidates Auto Select examines at most, so a weak market cannot cost hundreds of downloads.
MAX_EXAMINED = 200
STALLED = "Stopped early: no new best Train Fitness in the last 1,000 simulations."
#: Never swapped in: ``ts_step`` takes no input a seed can give, vector operators prepare a
#: field rather than make a signal, and the other two categories are not for Alphas.
NOT_SWAPPED = frozenset({"ts_step"})
NOT_SWAPPED_CATEGORIES = frozenset({"Vector", "Special", "Reduce"})


# --- seed scores ------------------------------------------------------------

#: The notes' Sharpe curve is ln(x - 0.58) from 1.58 to 4.0, a reward phase 2.42 wide.
_CURVE = 2.42
#: The notes' turnover curve falls 0.5 a point past 40%, against a best of 1.5 over 27.5 points.
_SLOPE = 27.5 / 3


def more(x: float, low: float, high: float) -> float:
    """The notes' Sharpe curve: punished below ``low``, a log reward to ``high``, then flat."""
    t = (x - low) / (high - low)
    if t < 0:
        return -0.5 * (_CURVE * t) ** 2 / math.log1p(_CURVE)
    return math.log1p(_CURVE * min(t, 1.0)) / math.log1p(_CURVE)


def less(x: float, best: float, worst: float) -> float:
    """The notes' turnover curve: full reward to ``best``, a quadratic fall to ``worst``, then a
    steep line."""
    t = (x - best) / (worst - best)
    if t <= 0:
        return 1.0
    if t <= 1:
        return 1.0 - t * t
    return -_SLOPE * (t - 1.0)


#: Weight, curve and anchors per stored metric. Returns plateau at 15% so a volatile Alpha
#: cannot buy its rank with returns alone (the notes' Alpha 1).
UTILITY: dict[str, tuple[float, Callable[[float, float, float], float], float, float]] = {
    "fitness": (0.30, more, 1.0, 2.5),
    "sharpe": (0.25, more, 1.58, 4.0),
    "turnover": (0.15, less, 0.125, 0.40),
    "margin": (0.10, more, 0.0005, 0.0020),
    "drawdown": (0.10, less, 0.05, 0.20),
    "returns": (0.10, more, 0.05, 0.15),
}
#: Checks measuring the last two years, which the train/test split holds out.
TEST_YEAR_CHECKS = frozenset({"LOW_2Y_SHARPE", "IS_LADDER_SHARPE"})


def train_figure(row: dict[str, Any], key: str) -> Any:
    """``key`` over the train years where the Alpha has it, so the held-out years pick nothing."""
    train = row.get(f"train_{key}")
    return row.get(key) if train is None else train


def utility(row: dict[str, Any]) -> float:
    """The weighted utility of an Alpha's stored metrics. A missing metric leaves the weights."""
    total = weight = 0.0
    for key, (share, curve, low, high) in UTILITY.items():
        value = train_figure(row, key)
        if value is not None:
            total += share * curve(float(value), low, high)
            weight += share
    return total / weight if weight else -math.inf


def pass_rate(checks: list[Any]) -> float | None:
    """The share of judged checks passed; pending, labelling and test-year checks are not judged."""
    skipped = IGNORED_CHECKS | TEST_YEAR_CHECKS
    judged = [
        str(c.get("result", "")).upper()
        for c in checks
        if isinstance(c, dict) and str(c.get("name", "")).upper() not in skipped
    ]
    judged = [r for r in judged if r in ("PASS", "FAIL", "WARNING", "ERROR")]
    return sum(r == "PASS" for r in judged) / len(judged) if judged else None


def seed_score(row: dict[str, Any]) -> float:
    """Utility scaled by the checks passed, the notes' S x (1 + passed / total).

    A negative score is divided instead, so passing more checks always ranks higher.
    """
    score = utility(row)
    rate = pass_rate(checks_of(row.get("checks")))
    if rate is None or not math.isfinite(score):
        return score
    return score * (1 + rate) if score >= 0 else score / (1 + rate)


def population_for(simulations: int, chosen: int | None = None) -> int:
    """The notes' population of a hundred, smaller for a small task so it breeds about twenty
    generations, and never below twenty."""
    return chosen or min(100, max(20, 10 * (simulations // 200)))


def stalled(values: list[float | None]) -> bool:
    """True when the last :data:`PATIENCE` spent children found nothing better than those before.

    ``values`` are the spent children's scores in the order they were asked, ``None`` for one
    that failed.
    """
    if len(values) <= PATIENCE:
        return False
    before = [v for v in values[:-PATIENCE] if v is not None]
    recent = [v for v in values[-PATIENCE:] if v is not None]
    return bool(before) and (not recent or max(recent) <= max(before))


# --- the market ---------------------------------------------------------------


def _inputs(info: OperatorInfo, count: int) -> tuple[str, ...]:
    """The names of an operator's first ``count`` inputs, e.g. ``("x", "d")``."""
    return tuple(p.split("=")[0].strip() for p in info.params[:count])


@dataclass(frozen=True, slots=True)
class Market:
    """What a child in one market may be made of."""

    table: dict[str, OperatorInfo]
    #: Operators a mutation may swap in, by category.
    swappable: dict[str, tuple[OperatorInfo, ...]]
    #: Matrix fields and their dataset; only these are swapped for one another.
    dataset_of: dict[str, str]
    by_dataset: dict[str, tuple[str, ...]]
    #: Every field of the market and the grouping fields: what a child may name.
    names: frozenset[str]
    neutralizations: tuple[str, ...]

    def peers(self, node: Node) -> list[str]:
        """Operators that can stand in for this call: same category, the same inputs."""
        info = self.table.get(node.value)
        if info is None or node.kwargs or node.value in UNCOUNTED_OPERATORS:
            return []
        count = len(node.args)
        inputs = _inputs(info, count)
        return [
            other.name
            for other in self.swappable.get(info.category, ())
            if other.name != info.name
            and other.required <= count
            and (other.maximum is None or count <= other.maximum)
            and _inputs(other, count) == inputs
        ]

    def genes(self, tree: Node) -> list[tuple[Path, str]]:
        """Where a mutation may change this tree, and what kind of change each is."""
        found: list[tuple[Path, str]] = []
        for path, node, parent in walk_with_parents(tree):
            if node.kind == "binary" and node.value in ARITHMETIC:
                found.append((path, "op"))
            elif node.kind == "call":
                if self.peers(node):
                    found.append((path, "op"))
                info = self.table.get(node.value)
                if info is None or node.value in UNCOUNTED_OPERATORS:
                    continue
                names = _inputs(info, len(node.args))
                for index, (arg, name) in enumerate(zip(node.args, names, strict=False)):
                    if name == "d" and arg.kind == "num" and arg.value.isdigit():
                        found.append(((*path, index), "window"))
            elif node.kind == "name":
                grouping = (
                    parent is not None
                    and parent.kind == "call"
                    and parent.value.startswith("group_")
                    and 1 <= path[-1] < len(parent.args)
                )
                dataset = self.dataset_of.get(node.value)
                if grouping and node.value in search.GROUPS:
                    found.append((path, "group"))
                elif dataset and not guarded(parent, path) and len(self.by_dataset[dataset]) > 1:
                    found.append((path, "field"))
        return found


def build_market(
    operators: list[dict[str, Any]], fields: list[dict[str, Any]], neutralizations: list[str]
) -> Market:
    table = operator_table(operators)
    levels = {o.get("name"): o.get("level") for o in operators}
    # Only ALL-level operators are swapped in; one listed without a level is never swapped
    # in, though a seed may keep one.
    leveled = any(level is not None for level in levels.values())
    swappable: dict[str, list[OperatorInfo]] = defaultdict(list)
    for info in table.values():
        if (
            info.name in UNCOUNTED_OPERATORS
            or info.name in search.EXCLUDED
            or info.name in NOT_SWAPPED
            or info.category in NOT_SWAPPED_CATEGORIES
            or (leveled and levels.get(info.name) != "ALL")
        ):
            continue
        swappable[info.category].append(info)
    dataset_of = {
        str(f["field_id"]): str(f["dataset_id"])
        for f in fields
        if f.get("field_type") == "MATRIX" and f.get("dataset_id")
    }
    by_dataset: dict[str, list[str]] = defaultdict(list)
    for field_id, dataset in dataset_of.items():
        by_dataset[dataset].append(field_id)
    return Market(
        table=table,
        swappable={k: tuple(v) for k, v in swappable.items()},
        dataset_of=dataset_of,
        by_dataset={k: tuple(v) for k, v in by_dataset.items()},
        names=frozenset(str(f["field_id"]) for f in fields) | frozenset(search.GROUPS),
        neutralizations=tuple(neutralizations),
    )


#: Kept until restart, so a market downloaded again or new operators are only seen then.
_markets: dict[tuple[Any, ...], Market] = {}


async def market_for(
    catalog: Catalog,
    operators: list[dict[str, Any]],
    region: str,
    delay: int,
    universe: str,
    neutralizations: list[str],
) -> Market | None:
    """The market's fields with the account's operators, or ``None`` when either is missing."""
    key = (region, delay, universe, tuple(neutralizations), len(operators))
    if key not in _markets:
        fields = await catalog.query(
            "SELECT field_id, dataset_id, field_type FROM data_field "
            "WHERE instrument_type = 'EQUITY' AND region = ? AND delay = ? AND universe = ?",
            [region, delay, universe],
        )
        if not fields or not operators:
            return None
        _markets[key] = build_market(operators, fields, neutralizations)
    return _markets[key]


def unreadable(expression: str | None, market: Market) -> str | None:
    """Why an expression cannot breed in this market, or ``None``."""
    try:
        tree = parse(expression or "")
    except ParseError as exc:
        return f"Its expression could not be read: {exc}"
    problems = validate(tree, market.table, market.names)
    return problems[0] if problems else None


# --- breeding ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Parent:
    alpha_id: str | None
    tree: Node
    neutralization: str
    decay: int
    truncation: float

    @classmethod
    def of(
        cls, alpha_id: str | None, expression: str | None, settings: dict[str, Any]
    ) -> Parent | None:
        try:
            tree = parse(expression or "")
        except ParseError:
            return None
        return cls(
            alpha_id,
            tree,
            str(settings.get("neutralization") or "NONE"),
            int(settings.get("decay") or 0),
            float(settings.get("truncation") or search.TRUNCATION),
        )

    @property
    def key(self) -> tuple[Node, str, int, float]:
        return self.tree, self.neutralization, self.decay, self.truncation


def skeleton(tree: Node, genes: list[tuple[Path, str]]) -> Node:
    """The tree with its genes blanked: parents sharing one cross gene by gene."""
    return relabel(tree, {path: f"?{kind}" for path, kind in genes})


def uniform(rng: random.Random, a: Node, b: Node, genes: list[tuple[Path, str]]) -> Node:
    """Each gene from either parent with equal chance; the parents share a skeleton."""
    taken = {path: node_at(b, path).value for path, _ in genes if rng.random() < 0.5}
    return relabel(a, taken)


def _mutated(rng: random.Random, node: Node, kind: str, market: Market) -> str:
    if kind == "window":
        current = int(node.value)
        if current not in WINDOWS:
            return str(min(WINDOWS, key=lambda w: abs(w - current)))
        at = WINDOWS.index(current)
        return str(rng.choice([WINDOWS[i] for i in (at - 1, at + 1) if 0 <= i < len(WINDOWS)]))
    if kind == "field":
        dataset = market.dataset_of[node.value]
        return rng.choice([f for f in market.by_dataset[dataset] if f != node.value])
    if kind == "group":
        return rng.choice([g for g in search.GROUPS if g != node.value])
    if node.kind == "binary":
        return rng.choice([o for o in ARITHMETIC if o != node.value])
    return rng.choice(market.peers(node))


def mutate(
    rng: random.Random,
    tree: Node,
    settings: dict[str, Any],
    market: Market,
    rate: float,
    *,
    force: bool = False,
) -> tuple[Node, dict[str, Any]]:
    """Change each gene with probability ``rate``; with ``force``, at least one."""
    moves: list[tuple[Path, str]] = market.genes(tree)
    if any(n != settings["neutralization"] for n in market.neutralizations):
        moves.append(((), "neutralization"))
    moves.append(((), "decay"))
    chosen = [move for move in moves if rng.random() < rate]
    if force and not chosen:
        chosen = [rng.choice(moves)]
    settings = dict(settings)
    # Every new value is drawn in the order chosen, then written in one pass: a value depends
    # only on its own node, which no other edit here changes.
    values: dict[Path, str] = {}
    for path, kind in chosen:
        if kind == "neutralization":
            current = settings["neutralization"]
            settings[kind] = rng.choice([n for n in market.neutralizations if n != current])
        elif kind == "decay":
            settings[kind] = rng.choice([d for d in search.DECAYS if d != settings["decay"]])
        else:
            values[path] = _mutated(rng, node_at(tree, path), kind, market)
    return relabel(tree, values), settings


@dataclass(frozen=True, slots=True)
class Shape:
    """What breeding reads of a parent on every attempt, worked out once per parent."""

    genes: list[tuple[Path, str]]
    skeleton: Node
    operators: int
    #: Where crossover may graft into this tree, and the calls it may take from it.
    sites: list[Path]
    grafts: list[Node]

    @classmethod
    def of(cls, tree: Node, market: Market) -> Shape:
        genes = market.genes(tree)
        return cls(
            genes, skeleton(tree, genes), operator_count(tree), graft_sites(tree), grafts(tree)
        )


def child(
    rng: random.Random,
    a: Parent,
    b: Parent,
    market: Market,
    rate: float,
    shapes: dict[Node, Shape],
) -> tuple[Node, dict[str, Any]] | None:
    """A valid child of ``a`` and ``b`` that is neither of them, or ``None`` after ten tries.
    ``shapes`` holds each parent tree's :class:`Shape`."""
    shape_a, shape_b = shapes[a.tree], shapes[b.tree]
    genes = shape_a.genes
    same = shape_a.skeleton == shape_b.skeleton
    limit = max(shape_a.operators, shape_b.operators)
    parents = {a.key, b.key}

    def clone(tree: Node, settings: dict[str, Any]) -> bool:
        key = (tree, settings["neutralization"], settings["decay"], settings["truncation"])
        return key in parents

    for _ in range(10):
        left, right = (a, b) if rng.random() < 0.5 else (b, a)
        if same:
            tree = uniform(rng, a.tree, b.tree, genes)
        else:
            tree = (
                graft(rng, left.tree, shapes[left.tree].sites, shapes[right.tree].grafts)
                or left.tree
            )
            if operator_count(tree) > limit:
                continue
        settings = {
            "neutralization": rng.choice((a.neutralization, b.neutralization)),
            "decay": rng.choice((a.decay, b.decay)),
            "truncation": left.truncation,
        }
        tree, settings = mutate(rng, tree, settings, market, rate)
        if clone(tree, settings):
            tree, settings = mutate(rng, tree, settings, market, 0.0, force=True)
        if clone(tree, settings) or validate(tree, market.table, market.names):
            continue
        return tree, settings
    return None


def request_for(tree: Node, settings: dict[str, Any], run: EvolutionParams) -> SimulationRequest:
    return SimulationRequest(
        settings=SimulationSettings(
            region=run.region,
            delay=run.delay,
            universe=run.universe,
            neutralization=settings["neutralization"],
            decay=int(settings["decay"]),
            truncation=float(settings["truncation"]),
            test_period=TEST_PERIOD,
        ),
        regular=render(tree),
    )


def ga_key(expression: str | None, settings: dict[str, Any] | None) -> tuple[str, str]:
    """One child whatever its test period: a seed is that child holding nothing out."""
    return search.identity_of(expression, {**(settings or {}), "testPeriod": None})


# --- keeping parents apart ----------------------------------------------------


#: Two Alphas moving together at |correlation| of this or more are one idea, not two.
DEFAULT_MAX_CORRELATION = 0.5


@dataclass(frozen=True, slots=True)
class Series:
    """Daily PnL over the train years, by date."""

    dates: np.ndarray
    values: np.ndarray


def series_of(days: dict[date, float]) -> Series | None:
    if len(days) < MIN_OVERLAP:
        return None
    ordered = sorted(days)
    return Series(
        np.array(ordered, dtype="datetime64[D]"),
        np.array([days[d] for d in ordered], dtype=float),
    )


#: An Alpha's daily PnL by region: its one region, or each scored region of a family.
Profile = dict[str, Series]


def profile_of(regions: dict[str, str], days: dict[str, dict[date, float]]) -> Profile | None:
    """The usable series among ``regions`` (region to the Alpha whose PnL stands there)."""
    found = {r: s for r, alpha_id in regions.items() if (s := series_of(days.get(alpha_id) or {}))}
    return found or None


def correlation(a: Series, b: Series) -> float | None:
    """Pearson correlation over the days both have; ``None`` below the minimum overlap."""
    _, left, right = np.intersect1d(a.dates, b.dates, assume_unique=True, return_indices=True)
    if len(left) < MIN_OVERLAP:
        return None
    x, y = a.values[left], b.values[right]
    if x.std() == 0 or y.std() == 0:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def kinship(a: Profile, b: Profile) -> float | None:
    """The strongest correlation two Alphas show in any region both trade.

    Region by region, because one idea read in USA and in EUR can look unrelated: the two
    markets move apart even when the idea does not. ``None`` with no region in common.
    """
    found = [rho for r in a.keys() & b.keys() if (rho := correlation(a[r], b[r])) is not None]
    return max(found, key=abs) if found else None


def collision(
    candidate: Profile, kept: dict[str, Profile], threshold: float = DEFAULT_MAX_CORRELATION
) -> tuple[str, float] | None:
    """The first kept Alpha this one moves with, at |correlation| of ``threshold`` or more."""
    for alpha_id, other in kept.items():
        rho = kinship(candidate, other)
        if rho is not None and abs(rho) >= threshold:
            return alpha_id, rho
    return None


class _Kept:
    """One region's kept series on a shared day-by-day calendar, so a candidate is measured
    against all of them in one array operation rather than one :func:`correlation` per pair.

    The calendar spans only the series placed on it, widening when one reaches past it:
    measured, setting it up from every candidate made a five-pick shortlist of a thousand
    twice as slow. Column-major, so the kept columns are one contiguous block.
    """

    def __init__(self) -> None:
        self.origin: np.datetime64 | None = None
        self.days = 0
        self.columns = np.empty((0, 0), order="F")
        self.count = 0

    def place(self, series: Series) -> np.ndarray:
        """``series`` on the calendar: its values on its days, NaN on the rest."""
        first, last = series.dates[0], series.dates[-1]
        origin = self.origin
        if origin is None or first < origin or last >= origin + self.days:
            origin = self._widen(first, last)
        placed = np.full(self.days, np.nan)
        placed[(series.dates - origin).astype(int)] = series.values
        return placed

    def _widen(self, first: np.datetime64, last: np.datetime64) -> np.datetime64:
        origin = first if self.origin is None else min(first, self.origin)
        end = last if self.origin is None else max(last, self.origin + self.days - 1)
        days = int((end - origin).astype(int)) + 1
        columns = np.full((days, max(self.columns.shape[1], 8)), np.nan, order="F")
        if self.origin is not None and self.count:
            shift = int((self.origin - origin).astype(int))
            columns[shift : shift + self.days, : self.count] = self.columns[:, : self.count]
        self.origin, self.days, self.columns = origin, days, columns
        return origin

    def add(self, placed: np.ndarray) -> None:
        if self.count == self.columns.shape[1]:
            wider = np.full((self.days, 2 * self.count), np.nan, order="F")
            wider[:, : self.count] = self.columns
            self.columns = wider
        self.columns[:, self.count] = placed
        self.count += 1

    def moves_with(self, placed: np.ndarray, threshold: float) -> bool:
        """Whether ``placed`` correlates with any kept series at |correlation| of
        ``threshold`` or more: :func:`correlation`'s arithmetic, pair by pair at once."""
        if not self.count:
            return False
        x, y = placed[:, None], self.columns[:, : self.count]
        both = ~np.isnan(x) & ~np.isnan(y)
        days = both.sum(axis=0)
        x, y = np.where(both, x, 0.0), np.where(both, y, 0.0)
        dx = np.where(both, x - x.sum(axis=0) / np.maximum(days, 1), 0.0)
        dy = np.where(both, y - y.sum(axis=0) / np.maximum(days, 1), 0.0)
        xx, yy = (dx * dx).sum(axis=0), (dy * dy).sum(axis=0)
        usable = (days >= MIN_OVERLAP) & (xx > 0) & (yy > 0)
        rho = np.clip((dx * dy).sum(axis=0) / np.sqrt(np.where(usable, xx * yy, 1.0)), -1, 1)
        return bool((usable & (np.abs(rho) >= threshold)).any())


def independent(
    order: list[str],
    series: dict[str, Profile | None],
    limit: int,
    threshold: float = DEFAULT_MAX_CORRELATION,
) -> list[str]:
    """The notes' between-correlation selection: best first, each kept unless it moves with
    one already kept. An Alpha without a usable series counts as independent.

    :func:`collision`'s test, region by region: one region at the threshold is a collision,
    as the strongest of them is in :func:`kinship`. Measured on a hundred candidates, a third
    of the time, keeping the same Alphas.
    """
    regions: dict[str, _Kept] = {}
    kept: list[str] = []
    for alpha_id in order:
        if len(kept) >= limit:
            break
        own = series.get(alpha_id)
        if own is not None:
            placed = {r: regions.setdefault(r, _Kept()).place(s) for r, s in own.items()}
            if any(regions[r].moves_with(p, threshold) for r, p in placed.items()):
                continue
            for r, p in placed.items():
                regions[r].add(p)
        kept.append(alpha_id)
    return kept


async def _parents(optimizer: Optimizer, ranked: list[Trial], limit: int) -> list[Trial]:
    """The ranked trials kept apart by daily PnL. Missing PnL downloads in the background.

    A region-agnostic result has no series of its own; each scored region's child stands in.
    """
    ids = [t.alpha_id for t in ranked if t.alpha_id]
    where = await optimizer.alphas.region_series(ids)
    wanted = [alpha_id for regions in where.values() for alpha_id in regions.values()]
    days = await optimizer.alphas.train_pnl(wanted)
    missing = [alpha_id for alpha_id in wanted if alpha_id not in days]
    if missing and optimizer.backfill is not None:
        optimizer.backfill.schedule_returns(missing)
    series = {a: profile_of(regions, days) for a, regions in where.items()}
    kept = set(await asyncio.to_thread(independent, ids, series, limit))
    return [t for t in ranked if t.alpha_id in kept]


async def breed(
    optimizer: Optimizer, row: Study, want: int
) -> list[tuple[dict[str, Any], SimulationRequest]]:
    """Up to ``want`` new children of the best, mutually uncorrelated Alphas scored so far."""
    run = params_of(row, EvolutionParams)
    operators = await optimizer.metadata.cached_operators() or []
    market = await market_for(
        optimizer.alphas.catalog,
        operators,
        run.region,
        run.delay,
        run.universe,
        run.neutralizations,
    )
    if market is None:
        return []
    population = run.population
    # The database ranks and cuts to the population: a task can hold 100,000 trials, and
    # building each one to keep a hundred held the event loop for seconds.
    score = func.json_extract(Trial.values, "$[0]")
    async with optimizer.db.session() as session:
        ranked = list(
            (
                await session.scalars(
                    select(Trial)
                    .where(
                        Trial.study_id == row.id,
                        Trial.state == TrialState.COMPLETE,
                        score > FAILURE,
                    )
                    .order_by(score.desc(), Trial.number)
                    .limit(population)
                )
            ).all()
        )
        bred = await session.scalar(
            select(func.count()).where(Trial.study_id == row.id, Trial.generation > 0)
        )
        identities = (
            await session.execute(
                select(Trial.expression, type_coerce(Trial.settings, String)).where(
                    Trial.study_id == row.id
                )
            )
        ).all()
    chosen = await _parents(optimizer, ranked, population // 2)
    parents = [p for t in chosen if (p := Parent.of(t.alpha_id, t.expression, t.settings or {}))]
    if not parents:
        return []

    generation = 1 + (bred or 0) // population
    # Off the event loop: each key validates a trial's settings.
    seen = await asyncio.to_thread(
        lambda: {ga_key(e, json.loads(s or "null")) for e, s in identities}
    )
    rate = run.mutation_rate or MUTATION_RATE
    shapes = {p.tree: Shape.of(p.tree, market) for p in parents}
    rng = random.Random()
    out: list[tuple[dict[str, Any], SimulationRequest]] = []
    for _ in range(want * 10):
        if len(out) >= want:
            break
        a, b = rng.choice(parents), rng.choice(parents)
        made = child(rng, a, b, market, rate, shapes)
        if made is None:
            continue
        request = request_for(*made, run)
        key = ga_key(request.regular, request.settings.model_dump(by_alias=True, exclude_none=True))
        if key in seen:
            continue
        seen.add(key)
        out.append(({"parents": [a.alpha_id, b.alpha_id], "generation": generation}, request))
    log.info("ga.bred", study_id=row.id, generation=generation, children=len(out))
    return out


# --- choosing seeds -------------------------------------------------------------


def seed_problem(
    row: dict[str, Any] | None, region: str, delay: int, universe: str, market: Market | None
) -> str | None:
    """Why a stored Alpha cannot be a seed in this market, or ``None``."""
    if row is None or not row.get("expression"):
        return "Not stored here. Sync from BRAIN first."
    if is_submitted(row):
        return "Already submitted."
    kind = str(row.get("sim_type") or "REGULAR").upper()
    if kind == "RA_PARENT":
        if region != REGION_AGNOSTIC_REGION:
            return (
                "A region-agnostic Alpha breeds in All Regions. Here, seed from one of its regions."
            )
        if row.get("series_id") is None:
            # Not stood in for by a family: fewer than two regions have a Fitness.
            return "Fewer than two of its regions have a Fitness, and BRAIN needs two to submit it."
    elif kind not in EVOLVABLE_TYPES:
        # A SuperAlpha, say: its expression is a combo, not a regular expression.
        return f"A {kind} Alpha cannot be a seed: its expression is not a regular expression."
    elif region == REGION_AGNOSTIC_REGION:
        return "One region's Alpha breeds in its own market. Here, seed from region-agnostic ones."
    if (row.get("region"), row.get("delay"), row.get("universe")) != (region, delay, universe):
        return f"It ran in {row.get('region')} D{row.get('delay')} {row.get('universe')}."
    if row.get("fitness") is None:
        return "It has no Fitness yet."
    return unreadable(row["expression"], market) if market else None


def seed_row(row: dict[str, Any]) -> dict[str, Any]:
    score = seed_score(row)
    return {
        "alphaId": str(row["alpha_id"]),
        "expression": row.get("expression") or "",
        **{
            k: row.get(k)
            for k in ("sharpe", "fitness", "turnover", "returns", "drawdown", "margin")
        },
        "score": round(score, 4) if math.isfinite(score) else None,
        "limits": limits_of(checks_of(row.get("checks"))),
    }


async def _download(state: Any, alpha_id: str) -> None:
    """One Alpha's daily PnL, a failure logged rather than raised: it only costs a seed."""
    try:
        await state.backfill.fetch_pnl(alpha_id)
    except Exception:
        log.warning("ga.seed_pnl_failed", alpha_id=alpha_id, exc_info=True)


async def auto_seeds(
    state: Any,
    market: Market,
    region: str,
    delay: int,
    universe: str,
    want: int,
    report: Callable[[int, int], Awaitable[None]],
) -> dict[str, Any]:
    """The best, mutually uncorrelated unsubmitted Alphas of a market, and why the rest were
    not chosen. Downloads daily PnL where missing; simulates nothing."""
    found = await state.catalog.query(
        f"""
        SELECT a.* FROM alpha a
        WHERE coalesce(a.instrument_type, 'EQUITY') = 'EQUITY'
          AND a.region = ? AND a.delay = ? AND a.universe = ?
          AND NOT {SUBMITTED} AND {SEEDABLE} AND a.expression IS NOT NULL
        """,  # noqa: S608
        [region, delay, universe],
    )
    # Region-agnostic parents stand as their families, which is where their Sharpe is.
    stood = await state.alphas.with_families({str(r["alpha_id"]): r for r in found})
    rows = [
        r
        for r in stood.values()
        if (train_figure(r, "sharpe") or 0) > 0 and train_figure(r, "fitness") is not None
    ]
    kept: list[dict[str, Any]] = []
    kept_series: dict[str, Profile] = {}
    reasons: list[dict[str, str]] = []
    examined = 0
    # Off the event loop: a market holds tens of thousands, and each score parses its checks.
    ranked = await asyncio.to_thread(lambda: sorted(rows, key=seed_score, reverse=True))
    for row in ranked:
        if len(kept) >= want or examined >= MAX_EXAMINED:
            break
        examined += 1
        alpha_id = str(row["alpha_id"])
        problem = unreadable(row["expression"], market)
        own: Profile | None = None
        if problem is None:
            regions: dict[str, str] = row.get("region_series") or {str(row["region"]): alpha_id}
            days = await state.alphas.train_pnl(list(regions.values()))
            missing = [s for s in regions.values() if s not in days]
            # A family's regions together: four at most, the width every BRAIN read here uses.
            await asyncio.gather(*(_download(state, s) for s in missing))
            if missing:
                days = await state.alphas.train_pnl(list(regions.values()))
            own = profile_of(regions, days)
            hit = await asyncio.to_thread(collision, own, kept_series) if own else None
            if own is None:
                problem = "No usable daily PnL."
            elif hit:
                problem = f"Moves with {hit[0]}: correlation {hit[1]:.2f}."
        if problem or own is None:
            reasons.append({"alphaId": alpha_id, "reason": problem or "No usable daily PnL."})
        else:
            kept.append(row)
            kept_series[alpha_id] = own
        await report(len(kept), examined)
    return {"rows": kept, "reasons": reasons, "pool": len(rows), "examined": examined}
