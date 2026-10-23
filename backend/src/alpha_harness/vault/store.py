"""Keeping every alpha and its daily returns.

An alpha is a permanent result, so a local copy means never spending quota on it twice.

The daily returns matter more: two alphas with mediocre Sharpe that move independently
combine into something better than either, and which pairs those are is only answerable
if the series are kept. The stored series rebuilds the platform's own figures (see
:mod:`.metrics`), so a mix can be judged before a simulation is spent on it.
"""

import asyncio
import itertools
import json
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

import numpy as np
import structlog

from ..db.duck import ALPHA_COLUMNS, TRAIN_COLUMNS, Catalog
from . import metrics
from .yields import SUBMITTED, checks_of, children_check, limits_of, without_quota_checks

if TYPE_CHECKING:
    import pyarrow as pa

    from ..brain.schemas import Alpha

log = structlog.get_logger(__name__)

#: 3: turnover calibrated to ``yearly-stats`` where it misses by more than rounding. A series
#: stored under an older version is fetched again on the next sync.
SERIES_VERSION = 3
#: Alphas holding a current daily PnL, with or without its turnover. A full series written
#: before ``pnl_version`` existed carries only ``series_version``. One ``?``: the version.
PNL_STORED = "SELECT alpha_id FROM alpha WHERE coalesce(pnl_version, series_version) = ?"
#: Alphas whose series :meth:`AlphaVault.rebuild_after_cost_sharpe` holds in memory at once.
REBUILD_CHUNK = 100

#: :meth:`AlphaVault.pnl_grid`: the Alphas holding a PnL, the calendar, a ``date x alpha`` matrix.
type PnlGrid = tuple[list[str], list[date], metrics.Floats]


def checks_json(alpha: Alpha) -> str | None:
    """An alpha's submission checks as the string the vault stores.

    Shared with the backfill, which reads the same array to decide whether the platform
    is worth asking to finish checking an alpha.
    """
    stats = alpha.in_sample
    if stats is None:
        return None
    kept = without_quota_checks([c.model_dump(by_alias=True) for c in stats.checks])
    return json.dumps(kept)


def _names(alpha: Alpha, key: str) -> list[str]:
    """The ``name`` of each entry in one of the Alpha's undeclared lists, e.g. its
    classifications ("Power Pool Alpha") or pyramids ("ASI/D1/OTHER")."""
    raw = (alpha.model_extra or {}).get(key) or []
    return [str(c["name"]) for c in raw if isinstance(c, dict) and c.get("name")]


def _after_cost_sharpe(days: list[tuple[date, float, float]], info: dict[str, Any]) -> float | None:
    """This Alpha's After-Cost Sharpe as the tables show it (:func:`metrics.after_cost_sharpe`).

    The final days BRAIN counts but exports in no recordset are added first, so this reads the
    same series the Alpha page's after-cost chart draws, and agrees with its figure.
    """
    if not days:
        return None
    rows = metrics.with_closing(days, info)
    pnl = np.array([p for _, p, _ in rows], dtype=float)
    turnover = np.array([t for _, _, t in rows], dtype=float)
    return metrics.after_cost_sharpe(pnl, turnover)


def _end_date(alpha: Alpha) -> date | None:
    raw = (alpha.settings.model_extra or {}).get("endDate") if alpha.settings else None
    return _as_date(raw) if raw else None


def alpha_row(alpha: Alpha, fetched_at: datetime) -> tuple[Any, ...]:
    """One alpha flattened for storage."""
    stats = alpha.in_sample
    settings = alpha.settings
    code = alpha.regular or alpha.combo or alpha.selection
    return (
        alpha.id,
        (code.code if code else None),
        str(alpha.type) if alpha.type else None,
        settings.instrument_type if settings else None,
        settings.region if settings else None,
        settings.delay if settings else None,
        settings.universe if settings else None,
        settings.neutralization if settings else None,
        settings.decay if settings else None,
        settings.truncation if settings else None,
        stats.sharpe if stats else None,
        stats.fitness if stats else None,
        stats.turnover if stats else None,
        stats.returns if stats else None,
        stats.drawdown if stats else None,
        stats.margin if stats else None,
        stats.long_count if stats else None,
        stats.short_count if stats else None,
        alpha.grade,
        alpha.stage,
        alpha.status,
        (code.operator_count if code else None),
        alpha.date_created,
        checks_json(alpha),
        fetched_at,
        alpha.name,
        alpha.date_submitted,
        settings.max_trade if settings else None,
        settings.max_position if settings else None,
        json.dumps(alpha.tags),
        json.dumps(_names(alpha, "classifications")),
        json.dumps(_names(alpha, "pyramids")),
        stats.pnl if stats else None,
        _end_date(alpha),
        settings.simulation_mode if settings else None,
        stats.prod_correlation if stats else None,
        stats.self_correlation if stats else None,
        settings.language if settings else None,
        settings.pasteurization if settings else None,
        settings.nan_handling if settings else None,
    )


#: What the Simulations table may sort and filter on, as SQL over ``alpha a``. Anything
#: not in here is ignored rather than interpolated.
ALPHA_METRICS: dict[str, str] = {
    "sharpe": "a.sharpe",
    "fitness": "a.fitness",
    "turnover": "a.turnover",
    "returns": "a.returns",
    "drawdown": "a.drawdown",
    "margin": "a.margin",
    "operator_count": "a.operator_count",
    "calmar": "CASE WHEN a.drawdown > 0 THEN a.returns / a.drawdown END",
    "date_created": "a.date_created",
    "date_submitted": "a.date_submitted",
}

#: Types that can seed the Evolution Lab.
#:
#: An ``RA_CHILD`` is an ordinary Alpha that happens to have arrived through a
#: region-agnostic run: it carries one real region, that region's own universe, its own
#: Sharpe and Fitness, and an expression. Excluding it left anyone running region-agnostic
#: simulations with a vault full of Alphas that Evolution refused to breed from.
#:
#: An ``RA_PARENT`` breeds only in the all-regions market, and has no metrics of its own
#: (measured: Sharpe and Fitness both null), so it is scored through its children: see
#: :func:`family_row`.
EVOLVABLE = "(coalesce(a.sim_type, 'REGULAR') IN ('REGULAR', 'RA_CHILD'))"

#: The same rule, for a row already in hand.
EVOLVABLE_TYPES = frozenset({"REGULAR", "RA_CHILD"})


def family_of(parent: str, child: str = "c") -> str:
    """SQL: ``c`` is a region-agnostic child of the row aliased ``parent``.

    Rows carry no parent id, but BRAIN writes a family in one second, from one expression and
    one set of settings (measured on every family stored: the same expression recurs at
    other seconds, never with the same second and settings).
    """
    c = child
    return f"""
        {c}.sim_type = 'RA_CHILD' AND {c}.expression = {parent}.expression
        AND {c}.date_created = {parent}.date_created AND {c}.delay = {parent}.delay
        AND {c}.neutralization IS NOT DISTINCT FROM {parent}.neutralization
        AND {c}.decay IS NOT DISTINCT FROM {parent}.decay
        AND {c}.truncation IS NOT DISTINCT FROM {parent}.truncation
    """


def same_family(x: str, y: str) -> str:
    """SQL: rows ``x`` and ``y`` are one region-agnostic family, parent or child, as
    :func:`family_of` tells them apart."""
    return f"""
        {x}.sim_type IN ('RA_PARENT', 'RA_CHILD') AND {y}.sim_type IN ('RA_PARENT', 'RA_CHILD')
        AND {x}.expression = {y}.expression AND {x}.date_created = {y}.date_created
        AND {x}.delay = {y}.delay
        AND {x}.neutralization IS NOT DISTINCT FROM {y}.neutralization
        AND {x}.decay IS NOT DISTINCT FROM {y}.decay
        AND {x}.truncation IS NOT DISTINCT FROM {y}.truncation
    """


#: What the lab ranks an Alpha by: Train Fitness, or whole-period Fitness without a split.
MERIT = "coalesce({row}.train_fitness, {row}.fitness)"

#: Whether the Evolution Lab takes ``a`` as a seed, as far as its stored row can say: a
#: single-region Alpha with a Fitness, or a region-agnostic parent two of whose regions are
#: scored, since BRAIN submits a region-agnostic Alpha only when two regions pass. Regions,
#: not children: twin families simulated in the same second are indistinguishable here.
SEEDABLE = f"""(
    ({EVOLVABLE} AND a.fitness IS NOT NULL)
    OR (a.sim_type = 'RA_PARENT' AND (
        SELECT count(DISTINCT c.region) FROM alpha c
        WHERE {family_of("a")} AND {MERIT.format(row="c")} IS NOT NULL
    ) >= 2)
)"""  # noqa: S608 - built from constants in this module

#: What the seed picker shows of a family: the standing child's figures.
FAMILY_METRICS = (
    "sharpe",
    "fitness",
    "turnover",
    "returns",
    "drawdown",
    "margin",
    "train_sharpe",
    "test_sharpe",
    "long_count",
    "short_count",
)

#: The vault with each region-agnostic parent's figures taken from its standing child, so
#: the seed picker sorts and filters families in SQL across pages. The same child
#: :func:`family_row` picks, by the same merit and tie-break; a parent with fewer than two
#: scored regions keeps its empty figures.
STANDING_VIEW = f"""(
    WITH best AS (
        SELECT p.alpha_id AS parent_id, c.*
        FROM alpha p JOIN alpha c ON {family_of("p")}
        WHERE p.sim_type = 'RA_PARENT' AND {MERIT.format(row="c")} IS NOT NULL
        QUALIFY row_number() OVER (
            PARTITION BY p.alpha_id, c.region ORDER BY {MERIT.format(row="c")} DESC, c.alpha_id
        ) = 1
    ), standing AS (
        SELECT * FROM best
        QUALIFY row_number() OVER (
            PARTITION BY parent_id ORDER BY {MERIT.format(row="best")} DESC, alpha_id
        ) = 2
    )
    SELECT a.* REPLACE ({", ".join(f"coalesce(a.{k}, s.{k}) AS {k}" for k in FAMILY_METRICS)})
        , s.region AS standing_region
    FROM alpha a LEFT JOIN standing s ON s.parent_id = a.alpha_id
)"""  # noqa: S608 - built from constants in this module

#: What a family row takes from the parent rather than from its standing child.
PARENT_OWN = (
    "alpha_id",
    "sim_type",
    "region",
    "universe",
    "expression",
    "status",
    "name",
    "date_created",
    "date_submitted",
)


def _merit(row: dict[str, Any]) -> float | None:
    """What the lab ranks by: Train Fitness, or whole-period Fitness without a test split."""
    value = row.get("train_fitness")
    if value is None:
        value = row.get("fitness")
    return float(value) if value is not None else None


def family_row(parent: dict[str, Any], children: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A region-agnostic parent as the Evolution Lab scores it, or ``None`` below two regions.

    BRAIN submits one only when two or more regions pass, so the family is as good as its
    second-best region: that child's metrics, checks and daily PnL (``series_id``) stand in
    for the parent's, which has none. Identity, status and settings stay the parent's.
    Chosen as :data:`STANDING_VIEW` chooses it: best child per region, then the second region.
    """
    by_merit = sorted(
        ((merit, c) for c in children if (merit := _merit(c)) is not None),
        key=lambda pair: (-pair[0], str(pair[1]["alpha_id"])),
    )
    best: dict[str, tuple[float, dict[str, Any]]] = {}
    for merit, child in by_merit:
        best.setdefault(str(child.get("region")), (merit, child))
    if len(best) < 2:
        return None
    scored = [c for _, c in sorted(best.values(), key=lambda p: (-p[0], str(p[1]["alpha_id"])))]
    standing = scored[1]
    return {
        **standing,
        **{key: parent.get(key) for key in PARENT_OWN},
        "series_id": standing["alpha_id"],
        "regions": [c["region"] for c in scored],
        #: Each scored region's best child: whose daily PnL stands for the family there.
        "region_series": {str(c["region"]): str(c["alpha_id"]) for c in scored},
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _page_row(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "alphaId": r["alpha_id"],
        "name": r["name"],
        "type": r["sim_type"],
        "status": r["status"],
        "region": r["region"],
        "universe": r["universe"],
        "delay": r["delay"],
        "neutralization": r["neutralization"],
        "decay": r["decay"],
        "truncation": r["truncation"],
        "expression": r["expression"],
        "sharpe": r["sharpe"],
        "fitness": r["fitness"],
        "turnover": r["turnover"],
        "returns": r["returns"],
        "drawdown": r["drawdown"],
        "margin": r["margin"],
        "limits": limits_of(checks_of(r["checks"])),
        "operatorCount": r["operator_count"],
        "calmar": r["calmar"],
        "dateCreated": _iso(r["date_created"]),
        "dateSubmitted": _iso(r["date_submitted"]),
        "hasPnl": bool(r["has_pnl"]),
        "longCount": r["long_count"],
        "shortCount": r["short_count"],
        "maxTrade": r["max_trade"],
        "maxPosition": r["max_position"],
        "classifications": json_list(r["classifications"]),
        "pyramids": json_list(r["pyramids"]),
        "trainSharpe": r["train_sharpe"],
        "testSharpe": r["test_sharpe"],
        "standingRegion": r.get("standing_region"),
    }


def _by_alpha[T](ids: list[str], days: list[date], values: list[T]) -> dict[str, dict[date, T]]:
    """Rows sorted by Alpha, each Alpha's days made a dict in one go rather than row by row."""
    # No rows means no starts, but ``ends`` below would still hold ``len(ids)``.
    if not ids:
        return {}
    starts = [i for i in range(len(ids)) if i == 0 or ids[i] != ids[i - 1]]
    ends = [*starts[1:], len(ids)]
    return {
        ids[s]: dict(zip(days[s:e], values[s:e], strict=True))
        for s, e in zip(starts, ends, strict=True)
    }


# The three below run in a worker thread: on a portfolio's 240,000 days each holds the
# event loop, and every live update with it, for a third of a second.


def _daily(table: pa.Table) -> dict[str, dict[date, float]]:
    """``alpha_id, date, pnl`` rows, sorted by Alpha and date, as each Alpha's PnL by date."""
    # Through numpy: Arrow's own to_pylist builds dates twenty times slower.
    return _by_alpha(
        table.column("alpha_id").to_pylist(),
        table.column("date").to_numpy().tolist(),
        table.column("pnl").to_numpy().tolist(),
    )


def _daily_traded(table: pa.Table) -> dict[str, dict[date, tuple[float, float]]]:
    """:func:`_daily` with each day's turnover beside its PnL."""
    pnl, traded = table.column("pnl").to_numpy(), table.column("turnover").to_numpy()
    return _by_alpha(
        table.column("alpha_id").to_pylist(),
        table.column("date").to_numpy().tolist(),
        list(zip(pnl.tolist(), traded.tolist(), strict=True)),
    )


def _grid(table: pa.Table) -> PnlGrid:
    """``alpha_id, date, pnl`` rows as a calendar of dates by Alphas, NaN where one has none."""
    ids = table.column("alpha_id").combine_chunks().dictionary_encode()
    # The few thousand distinct days sorted, not every row's: measured, a third of the time.
    dates = table.column("date").combine_chunks().dictionary_encode()
    distinct = dates.dictionary.to_numpy(zero_copy_only=False)
    order = np.argsort(distinct)
    rank = np.empty_like(order)
    rank[order] = np.arange(len(order))
    grid = np.full((len(distinct), len(ids.dictionary)), np.nan)
    grid[rank[dates.indices.to_numpy()], ids.indices.to_numpy()] = table.column("pnl").to_numpy()
    return ids.dictionary.to_pylist(), distinct[order].tolist(), grid


def json_list(raw: Any) -> list[str]:
    return [str(v) for v in json.loads(raw)] if isinstance(raw, str) else []


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


class AlphaVault:
    """The local record of every alpha and its returns."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    # -- writing ---------------------------------------------------------

    async def save_alpha(self, alpha: Alpha) -> None:
        from ..db.models import utcnow

        await self._save([alpha], utcnow())

    async def save_alphas(self, alphas: list[Alpha]) -> int:
        """A page of alphas in one write. Ids must be distinct within the page."""
        from ..db.models import utcnow

        return await self._save(alphas, utcnow())

    async def _save(self, alphas: list[Alpha], fetched_at: datetime) -> int:
        """Store alphas, carrying train and test statistics only for the ones that have
        them, so a listing that leaves them out never blanks stored values.

        Refused when no Alpha carries metrics or settings: every stored Alpha has both, so
        that is BRAIN's format changing, and writing it would blank the stored rows.
        """
        if alphas and all(a.in_sample is None and a.settings is None for a in alphas):
            raise ValueError(
                f"BRAIN returned {len(alphas)} Alpha(s) without metrics or settings, "
                "so nothing was stored. The platform's Alpha format may have changed."
            )
        # An Alpha's own body keeps the checks it was simulated with; once stored, its checks
        # change only through ``/check`` (:meth:`save_checks`), which a resave must not undo.
        written = await self.catalog.upsert(
            "alpha",
            ALPHA_COLUMNS,
            [alpha_row(a, fetched_at) for a in alphas if a.train is None],
            keep=("checks",),
        )
        return written + await self.catalog.upsert(
            "alpha",
            ALPHA_COLUMNS + TRAIN_COLUMNS,
            [
                (
                    *alpha_row(a, fetched_at),
                    a.train.sharpe,
                    a.train.fitness,
                    a.test.sharpe if a.test else None,
                    a.test.fitness if a.test else None,
                    _as_date(a.test.start_date) if a.test and a.test.start_date else None,
                    a.test.turnover if a.test else None,
                )
                for a in alphas
                if a.train
            ],
            keep=("checks",),
        )

    async def save_pnl(
        self,
        alpha_id: str,
        pnl_rows: list[dict[str, Any]],
        turnover_rows: list[dict[str, Any]],
        yearly_rows: list[dict[str, Any]],
    ) -> int:
        """Store one alpha's daily PnL and turnover from its recordsets, the turnover scaled to
        BRAIN's yearly figures (see :func:`metrics.calibrate`)."""
        return await self._save_series(
            alpha_id, metrics.daily_rows(pnl_rows, turnover_rows), yearly_rows
        )

    async def save_turnover(
        self,
        alpha_id: str,
        turnover_rows: list[dict[str, Any]],
        yearly_rows: list[dict[str, Any]],
    ) -> int:
        """Complete a PnL stored by :meth:`save_pnl_only` with its turnover."""
        days = [(r["date"], float(r["pnl"] or 0.0)) for r in await self.pnl_series(alpha_id)]
        return await self._save_series(
            alpha_id, metrics.with_turnover(days, turnover_rows), yearly_rows
        )

    async def _save_series(
        self,
        alpha_id: str,
        days: list[tuple[date, float, float]],
        yearly_rows: list[dict[str, Any]],
    ) -> int:
        stored = await self.by_ids([alpha_id])
        split = (stored.get(alpha_id) or {}).get("test_start")
        if yearly_rows:
            days = metrics.calibrate(days, yearly_rows, split if isinstance(split, date) else None)
        written = await self.catalog.replace_pnl(
            alpha_id, [(alpha_id, day, pnl, turnover) for day, pnl, turnover in days]
        )
        if written:
            # Worked out here, once, rather than on every table that shows it: reading a
            # thousand Alphas' series to fill a column would cost more than the sweep did.
            await self.catalog.upsert(
                "alpha",
                ("alpha_id", "series_version", "pnl_version", "after_cost_5bps"),
                [
                    (
                        alpha_id,
                        SERIES_VERSION,
                        SERIES_VERSION,
                        _after_cost_sharpe(days, stored.get(alpha_id) or {}),
                    )
                ],
            )
        return written

    async def save_pnl_only(self, alpha_id: str, pnl_rows: list[dict[str, Any]]) -> int:
        """Store one alpha's daily PnL without its turnover: a third of the requests, and all
        a correlation reads. :meth:`save_turnover` completes it."""
        days = metrics.daily_pnl(pnl_rows)
        written = await self.catalog.replace_pnl(
            alpha_id, [(alpha_id, day, pnl, None) for day, pnl in days]
        )
        if written:
            await self.catalog.upsert(
                "alpha",
                ("alpha_id", "series_version", "pnl_version", "after_cost_5bps"),
                [(alpha_id, None, SERIES_VERSION, None)],
            )
        return written

    async def rebuild_after_cost_sharpe(self) -> int:
        """Fill in After-Cost Sharpe for Alphas whose series was stored before it existed.

        Local only -- it reads the series already kept, never BRAIN -- so this costs no
        request and no quota. Without it the column stays empty for every Alpha downloaded
        before this release, which reads as "no cost" rather than "not worked out yet".
        """
        pending = await self.catalog.query(
            """
            SELECT a.alpha_id, a.end_date, a.is_pnl, a.test_start, a.test_turnover
            FROM alpha a
            WHERE a.after_cost_5bps IS NULL
              AND EXISTS (
                  SELECT 1 FROM alpha_pnl p
                  WHERE p.alpha_id = a.alpha_id AND p.turnover IS NOT NULL
              )
            """
        )
        rebuilt = 0
        # A slice at a time: every series at once, as Python objects, peaked at 4.4 GB on a
        # vault of 3,269 series.
        for chunk in itertools.batched(pending, REBUILD_CHUNK, strict=False):
            series = await self.series([str(r["alpha_id"]) for r in chunk])
            rows = [
                (a, _after_cost_sharpe([(d, p, t) for d, (p, t) in sorted(days.items())], info))
                for a, info in ((str(r["alpha_id"]), r) for r in chunk)
                if (days := series.get(a))
            ]
            rebuilt += await self.catalog.upsert("alpha", ("alpha_id", "after_cost_5bps"), rows)
        log.info("vault.after_cost_sharpe_rebuilt", alphas=rebuilt)
        return rebuilt

    async def save_checks(self, alpha_id: str, checks: list[dict[str, Any]]) -> None:
        """Replace one alpha's check array and touch nothing else.

        A partial-column upsert: the platform's ``/check`` endpoint returns resolved
        checks and no metrics, so writing a whole row from it would blank out the Sharpe
        this alpha was stored with.
        """
        kept = json.dumps(without_quota_checks(checks))
        await self.catalog.upsert("alpha", ("alpha_id", "checks"), [(alpha_id, kept)])

    # -- reading ---------------------------------------------------------

    async def counts(self) -> dict[str, Any]:
        rows = await self.catalog.query(
            """
            SELECT
                (SELECT count(*) FROM alpha)                              AS alphas,
                (SELECT count(DISTINCT alpha_id) FROM alpha_pnl)          AS with_returns,
                (SELECT count(*) FROM alpha_pnl)                          AS daily_rows,
                (SELECT count(*) FROM alpha WHERE sharpe IS NOT NULL)     AS scored
            """
        )
        row = rows[0] if rows else {}
        return {
            "alphas": int(row.get("alphas") or 0),
            "withReturns": int(row.get("with_returns") or 0),
            "dailyRows": int(row.get("daily_rows") or 0),
            "scored": int(row.get("scored") or 0),
            "missingReturns": int(row.get("alphas") or 0) - int(row.get("with_returns") or 0),
        }

    async def scopes(self) -> list[dict[str, Any]]:
        """Which scopes hold alphas. Mixing only ever happens inside one of these."""
        return await self.catalog.query(
            """
            SELECT instrument_type, region, delay, universe,
                   count(*) AS alphas,
                   count(*) FILTER (WHERE sharpe IS NOT NULL) AS scored,
                   max(sharpe) AS best_sharpe
            FROM alpha
            WHERE region IS NOT NULL
            GROUP BY 1, 2, 3, 4
            ORDER BY alphas DESC
            """
        )

    async def evolvable_markets(self) -> list[dict[str, Any]]:
        """Equity scopes holding unsubmitted alphas the Evolution Lab would take as seeds.

        Fitness included, as the lab requires it: a market counted without it advertises
        seeds that Auto Select then cannot use.
        """
        return await self.catalog.query(
            f"""
            SELECT a.region, a.delay, a.universe, count(*) AS alphas FROM alpha a
            WHERE coalesce(a.instrument_type, 'EQUITY') = 'EQUITY'
              AND a.region IS NOT NULL AND a.delay IS NOT NULL AND a.universe IS NOT NULL
              AND NOT {SUBMITTED} AND {SEEDABLE} AND a.expression IS NOT NULL
            GROUP BY 1, 2, 3
            ORDER BY alphas DESC
            """  # noqa: S608
        )

    async def stored_count(self) -> int:
        """Alphas stored from a BRAIN listing (they carry a creation date)."""
        value = await self.catalog.scalar(
            "SELECT count(*) FROM alpha WHERE date_created IS NOT NULL"
        )
        return int(value or 0)

    async def latest_created(self) -> datetime | None:
        """The newest alpha stored, which is where an incremental sync resumes."""
        return await self.catalog.scalar("SELECT max(date_created) FROM alpha")

    async def page(
        self,
        *,
        submitted: bool = False,
        sort_by: str = "date_created",
        sort_desc: bool = True,
        regions: list[str] | None = None,
        delays: list[int] | None = None,
        universes: list[str] | None = None,
        minimum: dict[str, float] | None = None,
        maximum: dict[str, float] | None = None,
        search: str | None = None,
        evolvable: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """One page of the Simulations table: sorted, filtered, with the total.

        ``evolvable`` keeps only what the Evolution Lab can breed from. The same predicate
        the lab itself enforces, so the table offering an Alpha and the lab accepting it can
        never disagree — a SuperAlpha carries a combo expression rather than a regular one,
        and picking one only to be told later that it cannot be a seed is the table's fault,
        not the user's.
        """
        clauses = ["a.date_created IS NOT NULL", SUBMITTED if submitted else f"NOT {SUBMITTED}"]
        if evolvable:
            # Every condition ``ga.seed_problem`` and ``ga.auto_seeds`` apply, not just the
            # type: an Alpha with no Fitness is refused there too, so offering it here would
            # be the same lie in a different place.
            clauses.extend(
                [
                    SEEDABLE,
                    "coalesce(a.instrument_type, 'EQUITY') = 'EQUITY'",
                    "a.expression IS NOT NULL",
                ]
            )
        params: list[Any] = []
        for column, values in (("region", regions), ("delay", delays), ("universe", universes)):
            if values:
                clauses.append(f"a.{column} IN ({', '.join('?' for _ in values)})")
                params.extend(values)
        for bounds, op in ((minimum, ">="), (maximum, "<=")):
            for key, value in (bounds or {}).items():
                if key in ALPHA_METRICS and not key.startswith("date_"):
                    clauses.append(f"({ALPHA_METRICS[key]}) {op} ?")
                    params.append(value)
        if search:
            needle = f"%{search.lower()}%"
            clauses.append(
                "(lower(a.alpha_id) LIKE ? OR lower(coalesce(a.expression, '')) LIKE ? "
                "OR lower(coalesce(a.name, '')) LIKE ?)"
            )
            params.extend([needle, needle, needle])

        where = " AND ".join(clauses)
        order = ALPHA_METRICS.get(sort_by, ALPHA_METRICS["date_created"])
        direction = "DESC" if sort_desc else "ASC"
        # Picking seeds, a region-agnostic parent sorts, filters and shows as its family.
        source = STANDING_VIEW if evolvable else "alpha"
        standing = "a.standing_region" if evolvable else "NULL AS standing_region"
        total = await self.catalog.scalar(f"SELECT count(*) FROM {source} a WHERE {where}", params)  # noqa: S608
        # has_pnl outside the LIMIT: inside, it probed the PnL table for every Alpha, not 100.
        rows = await self.catalog.query(
            f"""
            SELECT q.*,
                   EXISTS (SELECT 1 FROM alpha_pnl p WHERE p.alpha_id = q.alpha_id) AS has_pnl
            FROM (
                SELECT a.alpha_id, a.name, a.sim_type, a.status, a.region, a.universe, a.delay,
                       a.neutralization, a.decay, a.truncation, a.expression, a.sharpe,
                       a.fitness, a.turnover, a.returns, a.drawdown, a.margin,
                       a.operator_count, {ALPHA_METRICS["calmar"]} AS calmar,
                       a.date_created, a.date_submitted, a.long_count, a.short_count,
                       a.max_trade, a.max_position, a.classifications, a.pyramids,
                       a.train_sharpe, a.test_sharpe, a.checks, {standing}, {order} AS sort_key
                FROM {source} a
                WHERE {where}
                ORDER BY sort_key {direction} NULLS LAST, a.alpha_id
                LIMIT ? OFFSET ?
            ) q
            ORDER BY q.sort_key {direction} NULLS LAST, q.alpha_id
            """,  # noqa: S608
            [*params, limit, offset],
        )
        return {"total": int(total or 0), "results": [_page_row(r) for r in rows]}

    async def pnl_series(self, alpha_id: str) -> list[dict[str, Any]]:
        """The stored daily PnL, oldest first, as ``{date, pnl}`` rows."""
        return await self.catalog.query(
            "SELECT date, pnl FROM alpha_pnl WHERE alpha_id = ? ORDER BY date", [alpha_id]
        )

    async def series_length(self, alpha_id: str) -> int:
        value = await self.catalog.scalar(
            "SELECT count(*) FROM alpha_pnl WHERE alpha_id = ?", [alpha_id]
        )
        return int(value or 0)

    async def daily_pnl(self, alpha_ids: list[str]) -> dict[str, dict[date, float]]:
        """Each Alpha's stored daily PnL by date, oldest first. Alphas without one are absent."""
        if not alpha_ids:
            return {}
        placeholders = ", ".join("?" for _ in alpha_ids)
        table = await self.catalog.arrow(
            f"""
            SELECT alpha_id, date, coalesce(pnl, 0) AS pnl FROM alpha_pnl
            WHERE alpha_id IN ({placeholders})
            ORDER BY alpha_id, date
            """,  # noqa: S608
            list(alpha_ids),
        )
        return await asyncio.to_thread(_daily, table)

    async def series(self, alpha_ids: list[str]) -> dict[str, dict[date, tuple[float, float]]]:
        """Each Alpha's daily ``(pnl, turnover)`` by date. Alphas still on the old series,
        which has no turnover, are absent."""
        if not alpha_ids:
            return {}
        placeholders = ", ".join("?" for _ in alpha_ids)
        table = await self.catalog.arrow(
            f"""
            SELECT alpha_id, date, coalesce(pnl, 0) AS pnl, turnover FROM alpha_pnl
            WHERE alpha_id IN ({placeholders}) AND turnover IS NOT NULL
            ORDER BY alpha_id, date
            """,  # noqa: S608
            list(alpha_ids),
        )
        return await asyncio.to_thread(_daily_traded, table)

    async def pnl_grid(self, alpha_ids: list[str]) -> PnlGrid:
        """Of these Alphas, the ones holding a current daily PnL (turnover or not); every date
        any of them traded; and their PnL on that calendar, a column each, NaN where one has
        no row.

        Arrow into numpy, never a Python object per day. As dicts a series costs 1.3 MB and
        6 ms, so the Power Pool of a sweep of 1,500 took 1.9 GB and nine seconds a request.
        """
        if not alpha_ids:
            return [], [], np.empty((0, 0))
        placeholders = ", ".join("?" for _ in alpha_ids)
        table = await self.catalog.arrow(
            f"""
            SELECT alpha_id, date, coalesce(pnl, 0) AS pnl FROM alpha_pnl
            WHERE alpha_id IN ({placeholders}) AND alpha_id IN ({PNL_STORED})
            """,  # noqa: S608
            [*alpha_ids, SERIES_VERSION],
        )
        return await asyncio.to_thread(_grid, table)

    async def submitted_members(self) -> list[dict[str, Any]]:
        """Every submitted Alpha with what the Portfolio page filters on.

        A region-agnostic parent is left out: BRAIN submits its passing children, which carry
        the PnL and count towards combined performance, and the parent has no figures of its own.

        The figures are BRAIN's own, as it reported them for the Alpha. Nothing here is
        recomputed: only the combination of several Alphas is ours to work out, because
        that is the one thing BRAIN does not publish.
        """
        return await self.catalog.query(
            f"""
            SELECT a.alpha_id, a.name, a.region, a.delay, a.universe, a.max_trade,
                   a.max_position, a.tags, a.classifications, a.pyramids, a.date_submitted,
                   a.sharpe, a.turnover, a.fitness, a.returns, a.drawdown, a.margin,
                   a.long_count, a.short_count, a.prod_correlation, a.self_correlation,
                   a.neutralization, a.decay, a.truncation, a.expression, a.operator_count,
                   a.train_sharpe, a.test_sharpe,
                   (
                       SELECT p.alpha_id FROM alpha p
                       WHERE p.sim_type = 'RA_PARENT' AND {family_of("p", "a")}
                       ORDER BY p.alpha_id LIMIT 1
                   ) AS ra_parent,
                   EXISTS (
                       SELECT 1 FROM alpha_pnl p
                       WHERE p.alpha_id = a.alpha_id AND p.turnover IS NOT NULL
                   ) AS has_series
            FROM alpha a
            WHERE {SUBMITTED} AND coalesce(a.sim_type, 'REGULAR') <> 'RA_PARENT'
            ORDER BY a.date_submitted DESC NULLS LAST, a.alpha_id
            """  # noqa: S608
        )

    async def lacking_series(self, alpha_ids: list[str]) -> list[str]:
        """Those of these Alphas with no PnL and turnover stored, in the order given."""
        if not alpha_ids:
            return []
        placeholders = ", ".join("?" for _ in alpha_ids)
        rows = await self.catalog.query(
            f"""
            SELECT alpha_id FROM alpha
            WHERE alpha_id IN ({placeholders}) AND series_version = ?
            """,  # noqa: S608
            [*alpha_ids, SERIES_VERSION],
        )
        stored = {str(r["alpha_id"]) for r in rows}
        return [a for a in alpha_ids if a not in stored]

    async def lacking_pnl(self, alpha_ids: list[str]) -> list[str]:
        """Those of these Alphas with no daily PnL stored, turnover or not, in the order given."""
        if not alpha_ids:
            return []
        placeholders = ", ".join("?" for _ in alpha_ids)
        rows = await self.catalog.query(
            f"SELECT alpha_id FROM ({PNL_STORED}) WHERE alpha_id IN ({placeholders})",  # noqa: S608
            [SERIES_VERSION, *alpha_ids],
        )
        stored = {str(r["alpha_id"]) for r in rows}
        return [a for a in alpha_ids if a not in stored]

    async def train_pnl(self, alpha_ids: list[str]) -> dict[str, dict[date, float]]:
        """Each Alpha's daily PnL before its last two years, which a train/test split holds out.

        Only Alphas with a stored series appear.
        """
        if not alpha_ids:
            return {}
        placeholders = ", ".join("?" for _ in alpha_ids)
        # Arrow rather than a dict per row: measured, a third of the time for a hundred Alphas.
        table = await self.catalog.arrow(
            f"""
            SELECT p.alpha_id, p.date, coalesce(p.pnl, 0) AS pnl FROM alpha_pnl p
            JOIN (
                SELECT alpha_id, max(date) - INTERVAL 2 YEAR AS cutoff FROM alpha_pnl
                WHERE alpha_id IN ({placeholders}) GROUP BY alpha_id
            ) c ON p.alpha_id = c.alpha_id
            WHERE p.date < c.cutoff
            ORDER BY p.alpha_id, p.date
            """,  # noqa: S608
            list(alpha_ids),
        )
        return await asyncio.to_thread(_daily, table)

    async def families(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """The scorable region-agnostic parents among ``rows``, as family rows by id."""
        parents = {str(r["alpha_id"]): r for r in rows if r.get("sim_type") == "RA_PARENT"}
        if not parents:
            return {}
        placeholders = ", ".join("?" for _ in parents)
        found = await self.catalog.query(
            f"""
            SELECT p.alpha_id AS parent_of, c.* FROM alpha p JOIN alpha c ON {family_of("p")}
            WHERE p.alpha_id IN ({placeholders})
            """,  # noqa: S608
            list(parents),
        )
        children: dict[str, list[dict[str, Any]]] = {}
        for row in found:
            children.setdefault(str(row.pop("parent_of")), []).append(row)
        return {
            alpha_id: family
            for alpha_id, parent in parents.items()
            if (family := family_row(parent, children.get(alpha_id, [])))
        }

    async def ra_families(
        self, parent_ids: list[str] | None = None, limit: int = 200
    ) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        """Region-agnostic parents with every child row: these ids, or the newest ``limit``."""
        if parent_ids is not None and not parent_ids:
            return []
        params: list[Any] = []
        where = "p.sim_type = 'RA_PARENT'"
        if parent_ids is not None:
            where += f" AND p.alpha_id IN ({', '.join('?' for _ in parent_ids)})"
            params = list(parent_ids)
        parents = await self.catalog.query(
            f"""
            SELECT * FROM alpha p WHERE {where}
            ORDER BY p.date_created DESC NULLS LAST, p.alpha_id LIMIT {int(limit)}
            """,  # noqa: S608
            params,
        )
        if not parents:
            return []
        ids = [str(r["alpha_id"]) for r in parents]
        found = await self.catalog.query(
            f"""
            SELECT p.alpha_id AS parent_of, c.* FROM alpha p JOIN alpha c ON {family_of("p")}
            WHERE p.alpha_id IN ({", ".join("?" for _ in ids)})
            ORDER BY c.region
            """,  # noqa: S608
            ids,
        )
        children: dict[str, list[dict[str, Any]]] = {}
        for row in found:
            children.setdefault(str(row.pop("parent_of")), []).append(row)
        return [(p, children.get(str(p["alpha_id"]), [])) for p in parents]

    async def family_checks(
        self, rows: dict[str, dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        """For each region-agnostic parent among ``rows``, the one check that decides it,
        worked out from its children (:func:`yields.children_check`)."""
        parents = [a for a, r in rows.items() if r.get("sim_type") == "RA_PARENT"]
        return {
            str(parent["alpha_id"]): [children_check(children)]
            for parent, children in await self.ra_families(parents, limit=max(1, len(parents)))
        }

    async def with_families(self, rows: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """``rows``, each scorable region-agnostic parent standing as its family."""
        return rows | await self.families(list(rows.values()))

    async def region_series(self, alpha_ids: list[str]) -> dict[str, dict[str, str]]:
        """Whose daily PnL stands for each Alpha, by region: its own in its one region, or
        each scored region's best child for a region-agnostic family."""
        rows = await self.by_ids(alpha_ids)
        families = await self.families(list(rows.values()))
        return {
            a: families[a]["region_series"]
            if a in families
            else {str((rows.get(a) or {}).get("region")): a}
            for a in alpha_ids
        }

    async def by_ids(self, alpha_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Look several alphas up at once, keyed by id."""
        if not alpha_ids:
            return {}
        placeholders = ", ".join("?" for _ in alpha_ids)
        rows = await self.catalog.query(
            f"SELECT * FROM alpha WHERE alpha_id IN ({placeholders})",  # noqa: S608
            list(alpha_ids),
        )
        return {str(r["alpha_id"]): r for r in rows}
