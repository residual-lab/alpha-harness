"""Portfolio: submitted Alphas combined at equal weight, as BRAIN combines its own pool."""

import asyncio
import json
import math
import re
from typing import TYPE_CHECKING, Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, ValidationError

from ..brain.errors import BrainError
from ..brain.platform import (
    CORRELATION_WINDOW_YEARS,
    IGNORED_CHECKS,
    REGION_AGNOSTIC_CLASS,
    TRADING_DAYS,
)
from ..brain.schemas import QUICK_MODE, Alpha
from ..brain.settings_schema import chosen_labels
from ..labs.fastexpr import ParseError, data_fields, parse
from ..schemas import Out
from ..tools import portfolio, power_pool
from ..vault.store import SERIES_VERSION, json_list
from ..vault.yields import (
    REFUSING,
    checks_of,
    is_submitted,
    limits_of,
    verdict,
    without_quota_checks,
)
from .deps import State, refuse

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..state import AppState

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])

Investability = Literal["max_trade", "max_position", "none"]


class PortfolioMember(Out):
    alpha_id: str
    name: str | None
    region: str | None
    delay: int | None
    universe: str | None
    #: How the Alpha is held to its instruments' liquidity. BRAIN refuses Max Trade and Max
    #: Position both ON, so one of three.
    investability: Investability
    tags: list[str]
    #: BRAIN's names, e.g. "Power Pool Alpha".
    classifications: list[str]
    #: e.g. "ASI/D1/OTHER": region, delay and the data category the Pyramid pays on.
    pyramids: list[str]
    categories: list[str]
    #: ``False`` until a sync has read the Alpha's classifications and pyramids.
    labelled: bool
    #: BRAIN's own figures for the Alpha, as it reported them. The combination of several
    #: Alphas is worked out here because BRAIN does not publish one; a single Alpha's is not.
    sharpe: float | None
    turnover: float | None
    fitness: float | None
    returns: float | None
    drawdown: float | None
    margin: float | None
    long_count: int | None
    short_count: int | None
    #: When BRAIN took it, ISO 8601.
    date_submitted: str | None = None
    #: The region-agnostic parent this Alpha was submitted under, or ``None`` for one of its own
    #: region. Its siblings share it: BRAIN submits the passing children together.
    ra_parent: str | None = None
    #: Highest daily PnL correlation with any other submitted Alpha over BRAIN's four-year
    #: window. Every Alpha in the portfolio, unlike BRAIN's Self-Correlation, which keeps Power
    #: Pool Alphas apart. ``None`` without a series.
    max_pairwise_correlation: float | None
    #: Correlation with the equal-weighted Portfolio of every other submitted Alpha, over the
    #: same window. ``None`` without a series.
    portfolio_correlation: float | None
    #: BRAIN's Production Correlation for the submitted Alpha, as its listing gives it.
    prod_correlation: float | None = None
    #: The limit each of BRAIN's figure checks names for this Alpha, by check name.
    limits: dict[str, float] = Field(default_factory=dict)
    #: Self-Correlation as BRAIN's Alpha page shows it today, worked out locally from the stored
    #: PnL; ``None`` with nothing in its region to compare against.
    self_correlation: float | None = None
    #: Kestner's K-ratio (2013 form) of the in-sample PnL, worked out locally; net of the cost
    #: in After-Cost Mode.
    k_ratio: float | None = None
    #: A Selection Expression's metrics, by name: BRAIN's own figures (see :func:`brain_metrics`),
    #: whatever the page's cost. ``None`` where BRAIN gave none.
    metrics: dict[str, float | None]
    #: Sharpe of the equal-weighted Portfolio of every submitted Alpha, less its Sharpe without
    #: this one; net of the cost in After-Cost Mode.
    incremental_sharpe: float | None = None
    #: Power Pool Correlation as BRAIN's Alpha page shows it today, worked out locally the same
    #: way; ``None`` with no Power Pool Alpha in its region to compare against.
    power_pool_correlation: float | None = None
    #: The rest of what the Portfolio table can be set to show.
    neutralization: str | None = None
    decay: int | None = None
    truncation: float | None = None
    expression: str | None = None
    operator_count: int | None = None
    train_sharpe: float | None = None
    test_sharpe: float | None = None
    has_series: bool


class PortfolioMembers(Out):
    members: list[PortfolioMember]


class PortfolioStats(Out):
    pnl: float
    returns: float
    turnover: float
    drawdown: float
    margin: float
    sharpe: float | None
    fitness: float | None
    days: int


class Windows(Out):
    in_sample: PortfolioStats | None
    train: PortfolioStats | None
    test: PortfolioStats | None


class YearRow(PortfolioStats):
    year: int


class Period(Out):
    start: str
    end: str


class Periods(Out):
    in_sample: Period
    train: Period | None
    test: Period | None


class CorrelatedPair(Out):
    a: str
    b: str
    correlation: float


class PortfolioResult(Out):
    alphas: int
    #: Asked for but with no PnL and turnover stored; a sync downloads them.
    missing: list[str]
    ids: list[str]
    test_start: str | None
    periods: Periods | None
    stats: Windows | None
    after_cost: Windows | None
    yearly: list[YearRow]
    dates: list[str]
    curve: list[float]
    after_cost_curve: list[float]
    #: Pairwise daily PnL correlation in ``ids`` order; ``None`` where too few days overlap.
    #: Empty past ``tools.portfolio.GRID_LIMIT`` Alphas, where ``top_pairs`` stands in for it.
    correlation: list[list[float | None]]
    #: The most correlated pairs, highest first, when the grid is too large to send.
    top_pairs: list[CorrelatedPair]
    #: Pairs sharing enough days to be measured.
    measured_pairs: int
    highest: CorrelatedPair | None
    #: The least correlated pair, which can be negative.
    lowest: CorrelatedPair | None = None
    #: Why a series is missing, when the single-Alpha route tried to download one and could
    #: not. Null everywhere else: on this page a missing series is listed in ``missing``.
    problem: str | None = None


class PortfolioRequest(BaseModel):
    alpha_ids: list[str] = Field(min_length=1, max_length=2000)
    cost_bps: float = Field(default=5.0, ge=0, le=100)


def _investability(row: dict[str, Any]) -> Investability:
    if row.get("max_trade") == "ON":
        return "max_trade"
    if row.get("max_position") == "ON":
        return "max_position"
    return "none"


class PortfolioSyncStarted(Out):
    task_id: str


class CopyRequest(BaseModel):
    alpha_ids: list[str] = Field(min_length=1, max_length=2000)


class CopiedAlphas(Out):
    text: str
    #: Stored before Language, Pasteurization and NaN Handling were: a sync fills them in.
    unsynced: int


#: What a copied Alpha lists under Simulation Settings, in BRAIN's order: BRAIN's name for the
#: setting, its column here, and its title.
COPIED_SETTINGS = (
    ("region", "region", "Region"),
    ("universe", "universe", "Universe"),
    ("language", "language", "Language"),
    ("decay", "decay", "Decay"),
    ("delay", "delay", "Delay"),
    ("truncation", "truncation", "Truncation"),
    ("neutralization", "neutralization", "Neutralization"),
    ("pasteurization", "pasteurization", "Pasteurization"),
    ("nanHandling", "nan_handling", "NaN Handling"),
    ("maxTrade", "max_trade", "Max Trade"),
    ("maxPosition", "max_position", "Max Position"),
)


#: A Selection Expression's metrics, every one BRAIN's own figure for the Alpha: the name an
#: expression reads it by, and the column holding it.
BRAIN_METRICS = {
    "sharpe": "sharpe",
    "fitness": "fitness",
    "returns": "returns",
    "turnover": "turnover",
    "drawdown": "drawdown",
    "margin": "margin",
    "pnl": "is_pnl",
    "long_count": "long_count",
    "short_count": "short_count",
    "prod_correlation": "prod_correlation",
    "self_correlation": "self_correlation",
}


def brain_metrics(row: dict[str, Any]) -> dict[str, float | None]:
    """:data:`BRAIN_METRICS` for one stored Alpha."""
    return {
        name: None if row.get(c) is None else float(row[c]) for name, c in BRAIN_METRICS.items()
    }


def _imbalance(long: float | None, short: float | None) -> float | None:
    if long is None or short is None or not long + short:
        return None
    return (long - short) / (long + short)


def _pct(v: float) -> str:
    return f"{v:.2%}"


def _ratio(v: float) -> str:
    return f"{v:.2f}"


#: What a copied Alpha lists under Metrics, and how each reads: BRAIN's own figures to its own
#: places, and the four worked out here, which :data:`METRIC_GLOSSARY` defines.
COPIED_METRICS = (
    ("sharpe", "Sharpe", _ratio),
    ("turnover", "Turnover", _pct),
    ("fitness", "Fitness", _ratio),
    ("returns", "Returns", _pct),
    ("drawdown", "Drawdown", _pct),
    ("calmar", "Calmar", _ratio),
    ("margin", "Margin", lambda v: f"{v * 10_000:.2f}\u2031"),
    ("k_ratio", "K-Ratio", _ratio),
    ("long_count", "Long Count", lambda v: f"{v:,.0f}"),
    ("short_count", "Short Count", lambda v: f"{v:,.0f}"),
    ("imbalance", "Long/Short Imbalance", lambda v: f"{v:+.0%}"),
    ("prod_correlation", "Production Correlation", _ratio),
    ("max_pairwise_correlation", "Max Pairwise Correlation", _ratio),
    ("portfolio_correlation", "Portfolio Correlation", _ratio),
)

METRIC_GLOSSARY = "\n".join(
    (
        "# Metric Glossary",
        "",
        "Every figure is over the in-sample period on BRAIN's $20,000,000 book. Sharpe, "
        "Turnover, Fitness, Returns, Drawdown, Margin, Long Count, Short Count, Production "
        "Correlation, the Sub-Universe, Cluster, 2Y, regional and Robust Universe figures, the "
        "Investability Constrained Metrics (the same Alpha within BRAIN's liquidity limits on "
        "positions and trades) and the Checks are BRAIN's own. The rest are worked out locally "
        "from each Alpha's daily PnL, before cost:",
        "",
        "- **K-Ratio**: Kestner's K-ratio (2013 form). Fit a line to cumulative PnL against the "
        "day number, from the first day traded; "
        f"K = slope / (standard error of the slope * number of days) * sqrt({TRADING_DAYS}).",
        "- **Calmar**: Returns / Drawdown, from BRAIN's own two figures.",
        "- **Long/Short Imbalance**: (Long Count - Short Count) / (Long Count + Short Count). "
        "+100% is all long, -100% all short.",
        "- **Max Pairwise Correlation**: the highest Pearson correlation of daily PnL with any "
        "other submitted Alpha, on the days both traded, over the last "
        f"{CORRELATION_WINDOW_YEARS} calendar years of data, the latest included, as BRAIN "
        "measures its correlations.",
        "- **Portfolio Correlation**: the Pearson correlation of daily PnL with the "
        "equal-weighted portfolio of every other submitted Alpha (their daily PnL averaged over "
        "those trading that day), over the same window.",
    )
)


#: Where a field sits in BRAIN's catalog, broadest first.
PLACE = ("category_name", "subcategory_name", "dataset_id")


def _glossary(
    fields: list[str], described: dict[str, dict[str, Any]], datasets: dict[str, dict[str, Any]]
) -> str:
    """Every field read, under its category, subcategory and dataset, each level in alphabetical
    order. BRAIN describes datasets; categories and subcategories it only names."""

    def text(field: str, key: str) -> str:
        return str((described.get(field) or {}).get(key) or "")

    def order(field: str) -> tuple[bool | str, ...]:
        place = (text(field, k).casefold() for k in PLACE)
        return (field not in described, *place, field.casefold())

    # Markdown: a heading level each for category, subcategory and dataset, then the fields as
    # a list. Every heading and list has a blank line around it, or renderers run them together.
    lines: list[str] = []
    shown: tuple[str, ...] | None = None
    for field in sorted(fields, key=order):
        place = tuple(text(field, k) for k in PLACE) if field in described else None
        if place != shown:
            if place is None:
                lines += ["", "# Not in the local data catalog", ""]
            else:
                category, subcategory, dataset = place
                if shown is None or shown[:1] != place[:1]:
                    lines += ["", f"# Category: {category or '—'}"]
                if shown is None or shown[:2] != place[:2]:
                    lines += ["", f"## Subcategory: {subcategory or '—'}"]
                about = datasets.get(dataset) or {}
                named = f" ({about['name']})" if about.get("name") else ""
                lines += ["", f"### Dataset: {dataset or '—'}{named}", ""]
                if about.get("description"):
                    lines += [str(about["description"]), ""]
            shown = place
        if place is None:
            lines.append(f"- `{field}`")
        else:
            kind = text(field, "field_type") or "—"
            lines.append(f"- `{field}` ({kind}): {text(field, 'description') or '—'}")
    return "\n".join(lines).strip()


def _fields_in(expression: str) -> list[str]:
    try:
        return data_fields(parse(expression), grouping=True)
    except ParseError:
        return []


def _pnl_version(row: dict[str, Any]) -> Any:
    """The version of the PnL :meth:`AlphaVault.pnl_grid` reads: ``pnl_version`` when only a PnL
    is stored, else the series'."""
    version = row.get("pnl_version")
    return row.get("series_version") if version is None else version


type Framing = tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[str], portfolio.Framed]

#: The last :func:`_framed_submitted` at each cost, with the catalog revision it was read at.
#: Two at most: the Portfolio at no cost and at the cost being looked at.
_FRAMED: dict[float, tuple[int, Framing]] = {}


async def _framed_submitted(state: AppState, cost_bps: float | None) -> Framing:
    """Every submitted Alpha stored, its row, and the :func:`portfolio.frame` of those with a
    series. Laid out once for every figure worked out from it, it was each one's slowest step,
    and kept until the catalog is next written to: the Portfolio, its copy and a cost change
    read the same Alphas."""
    cost = cost_bps or 0.0
    revision = state.catalog.revision
    kept = _FRAMED.get(cost)
    if kept is not None and kept[0] == revision:
        return kept[1]
    rows = await state.alphas.submitted_members()
    ids = [str(r["alpha_id"]) for r in rows]
    series = await state.alphas.series(ids)
    meta = await state.alphas.by_ids(ids)
    with_series = [a for a in dict.fromkeys(ids) if series.get(a)]
    framed = await asyncio.to_thread(portfolio.frame, series, meta, with_series, cost)
    found: Framing = (rows, meta, with_series, framed)
    for stale in [c for c, (r, _) in _FRAMED.items() if r != revision or c not in (0.0, cost)]:
        del _FRAMED[stale]
    _FRAMED[cost] = (revision, found)
    return found


@router.get("/members")
async def members(
    state: State,
    cost_bps: Annotated[float | None, Query(alias="costBps", gt=0, le=100)] = None,
) -> PortfolioMembers:
    """Every submitted Alpha stored locally. With ``costBps``, its figures and correlations are
    worked out on its daily PnL net of that cost instead of read from BRAIN."""
    rows, meta, with_series, framed = await _framed_submitted(state, cost_bps)
    # Worked out even at no cost: some figures, like the K-ratio, BRAIN does not report.
    net = await asyncio.to_thread(portfolio.framed_figures, with_series, framed, meta)
    if cost_bps is None:
        # The PnL the grid reads, from the frame: only an Alpha holding a PnL without turnover
        # is read again, as a frame has no column for it.
        current = {a for a, row in meta.items() if _pnl_version(row) == SERIES_VERSION}
        framed_ids = set(with_series)
        extra = await state.alphas.pnl_grid([a for a in current if a not in framed_ids])
        grid = await asyncio.to_thread(portfolio.framed_grid, with_series, framed, current, extra)
    else:
        # Net of cost, the grid is the frame cut to the days BRAIN exported: measured, the
        # same to the cell as reading the PnL again and costing it, at a fifth of the time.
        dates, pnl, _ = await asyncio.to_thread(portfolio.exported, *framed)
        grid = (with_series, dates, pnl)
    highest = await asyncio.to_thread(portfolio.highest_correlations, with_series, framed)
    together = await asyncio.to_thread(portfolio.portfolio_correlations, with_series, framed)
    adds = await asyncio.to_thread(portfolio.incremental_sharpes, with_series, framed)
    own = await asyncio.to_thread(power_pool.self_correlations, grid, meta, rows)
    pooled = await asyncio.to_thread(power_pool.power_pool_correlations, grid, meta, rows)
    out: list[PortfolioMember] = []
    for r in rows:
        pyramids = json_list(r["pyramids"])
        # Net of cost, the figures are ours rather than BRAIN's; without a series, blank.
        figures: dict[str, Any] = r if cost_bps is None else net.get(str(r["alpha_id"]), {})
        out.append(
            PortfolioMember(
                alpha_id=str(r["alpha_id"]),
                name=r["name"],
                region=r["region"],
                delay=r["delay"],
                universe=r["universe"],
                investability=_investability(r),
                tags=json_list(r["tags"]),
                # BRAIN gives a region-agnostic child no class of its own; named here so it
                # can be filtered on like any other.
                classifications=[
                    *json_list(r["classifications"]),
                    *([REGION_AGNOSTIC_CLASS] if r.get("ra_parent") else []),
                ],
                pyramids=pyramids,
                categories=sorted({p.rsplit("/", 1)[-1] for p in pyramids}),
                labelled=r["pyramids"] is not None,
                sharpe=figures.get("sharpe"),
                turnover=r["turnover"],
                fitness=figures.get("fitness"),
                returns=figures.get("returns"),
                drawdown=figures.get("drawdown"),
                margin=figures.get("margin"),
                long_count=r["long_count"],
                ra_parent=r.get("ra_parent"),
                date_submitted=r["date_submitted"].isoformat() if r.get("date_submitted") else None,
                short_count=r["short_count"],
                max_pairwise_correlation=highest.get(str(r["alpha_id"])),
                portfolio_correlation=together.get(str(r["alpha_id"])),
                prod_correlation=r["prod_correlation"],
                self_correlation=own.get(str(r["alpha_id"])),
                power_pool_correlation=pooled.get(str(r["alpha_id"])),
                neutralization=r["neutralization"],
                decay=r["decay"],
                truncation=r["truncation"],
                expression=r["expression"],
                operator_count=r["operator_count"],
                train_sharpe=figures.get("train_sharpe"),
                test_sharpe=figures.get("test_sharpe"),
                k_ratio=net.get(str(r["alpha_id"]), {}).get("k_ratio"),
                metrics=brain_metrics(meta.get(str(r["alpha_id"])) or r),
                incremental_sharpe=adds.get(str(r["alpha_id"])),
                has_series=bool(r["has_series"]),
                limits=limits_of(checks_of((meta.get(str(r["alpha_id"])) or {}).get("checks"))),
            )
        )
    return PortfolioMembers(members=out)


@router.post("/sync")
async def sync(state: State) -> PortfolioSyncStarted:
    """Refresh the submitted Alphas from BRAIN and download the PnL and turnover of any not
    stored yet. Nothing else is listed."""
    try:
        return PortfolioSyncStarted(task_id=await state.backfill.start_submitted())
    except RuntimeError as exc:
        raise refuse(409, "already_running", str(exc)) from exc


async def _local_figures(state: AppState, alpha_ids: list[str]) -> dict[str, dict[str, Any]]:
    """K-Ratio, Max Pairwise Correlation and Portfolio Correlation for these Alphas, before
    cost. A submitted Alpha is measured against every other one, as the Portfolio table
    measures it; any other Alpha against every submitted one."""
    _, _, members, framed = await _framed_submitted(state, None)
    wanted = list(dict.fromkeys(alpha_ids))
    found: dict[str, dict[str, Any]] = {}
    inside = set(wanted) & set(members)
    if inside:
        highest = await asyncio.to_thread(portfolio.highest_correlations, members, framed)
        together = await asyncio.to_thread(portfolio.portfolio_correlations, members, framed)
        k_ratios = await asyncio.to_thread(portfolio.k_ratios, members, framed, inside)
        for a in inside:
            found[a] = {
                "k_ratio": k_ratios.get(a),
                "max_pairwise_correlation": highest.get(a),
                "portfolio_correlation": together.get(a),
            }
    outside = [a for a in wanted if a not in set(members)]
    own = await state.alphas.series(outside) if outside else {}
    held = [a for a in outside if own.get(a)]
    if held:
        ids = members + held
        alone = await asyncio.to_thread(portfolio.frame, own, await state.alphas.by_ids(held), held)
        combined = await asyncio.to_thread(portfolio.joined, framed, alone)
        against = await asyncio.to_thread(portfolio.against_pool, ids, combined, len(members))
        k_ratios = await asyncio.to_thread(portfolio.k_ratios, ids, combined, set(held))
        for a in held:
            highest, together = against[a]
            found[a] = {
                "k_ratio": k_ratios.get(a),
                "max_pairwise_correlation": highest,
                "portfolio_correlation": together,
            }
    return found


async def _read_bodies(state: AppState, alpha_ids: list[str]) -> None:
    """Read these Alphas from BRAIN and store them whole. One BRAIN will not give is left as
    stored: the copy goes ahead with what is known."""
    gate = asyncio.Semaphore(4)

    async def one(alpha_id: str) -> Alpha | None:
        async with gate:
            try:
                return Alpha.model_validate(await state.endpoints.alpha_body(alpha_id))
            except (BrainError, ValidationError) as exc:
                log.warning("portfolio.body_unread", alpha_id=alpha_id, error=str(exc)[:160])
                return None

    found = [a for a in await asyncio.gather(*(one(a) for a in alpha_ids)) if a is not None]
    if found:
        await state.alphas.save_alphas(found)


def _brain_of(row: dict[str, Any]) -> dict[str, Any]:
    try:
        found = json.loads(row.get("brain") or "{}")
    except json.JSONDecodeError:
        return {}
    return found if isinstance(found, dict) else {}


#: The eight figures of BRAIN's Investability Constrained block, in BRAIN's order.
CONSTRAINED_FIGURES = (
    ("sharpe", "Sharpe", _ratio),
    ("turnover", "Turnover", _pct),
    ("fitness", "Fitness", _ratio),
    ("returns", "Returns", _pct),
    ("drawdown", "Drawdown", _pct),
    ("margin", "Margin", lambda v: f"{v * 10_000:.2f}\u2031"),
    ("longCount", "Long Count", lambda v: f"{v:,.0f}"),
    ("shortCount", "Short Count", lambda v: f"{v:,.0f}"),
)
#: A check's value read as a metric, where the check measures something the Metrics do not
#: already show, listed in this order right after the metric it belongs with. The value is
#: the figure; whether it passed is BRAIN's business at submission.
CHECK_METRICS = {
    "LOW_SUB_UNIVERSE_SHARPE": ("sharpe", "Sub-Universe Sharpe", _ratio),
    "CLUSTER_TEST": ("sharpe", "Cluster Sharpe", _ratio),
    "LOW_2Y_SHARPE": ("sharpe", "2Y Sharpe", _ratio),
    "LOW_GLB_AMER_SHARPE": ("sharpe", "GLB AMER Sharpe", _ratio),
    "LOW_GLB_APAC_SHARPE": ("sharpe", "GLB APAC Sharpe", _ratio),
    "LOW_GLB_EMEA_SHARPE": ("sharpe", "GLB EMEA Sharpe", _ratio),
    "LOW_ASI_JPN_SHARPE": ("sharpe", "ASI JPN Sharpe", _ratio),
    "LOW_ROBUST_UNIVERSE_SHARPE": ("sharpe", "Robust Universe Sharpe", _ratio),
    "LOW_ROBUST_UNIVERSE_SHARPE.WITH_RATIO": ("sharpe", "Robust Universe Sharpe", _ratio),
    "LOW_ROBUST_UNIVERSE_RETURNS": ("returns", "Robust Universe Returns", _pct),
}
#: The checks copied as checks: what they report has no single figure to stand for it.
COPIED_CHECKS = ("IS_LADDER_SHARPE", "CONCENTRATED_WEIGHT")


def _check_text(check: dict[str, Any]) -> str:
    """One check as BRAIN reports it: its result, then every figure it gives."""
    parts = [
        f"{re.sub(r'(?<!^)(?=[A-Z])', ' ', key).lower()} {value}"
        for key, value in check.items()
        if key not in ("name", "result") and value is not None
    ]
    detail = f" ({', '.join(parts)})" if parts else ""
    return f"{check.get('name')}: {check.get('result')}{detail}"


def _settings_lines(schema: dict[str, Any] | None, settings: dict[str, Any]) -> list[str]:
    """Simulation Settings as BRAIN labels them, from settings keyed by BRAIN's names."""
    labels = chosen_labels(schema, settings)
    return [
        f"{title}: {labels.get(name) or ('—' if settings.get(name) is None else settings[name])}"
        for name, _, title in COPIED_SETTINGS
    ]


def _settled_checks(row: dict[str, Any], brain: dict[str, Any]) -> list[dict[str, Any]]:
    """The more settled of the stored checks and the body's: fewer still PENDING, the stored
    ones on a tie, as ``/check`` may have spoken since the body was read."""
    stored = checks_of(row.get("checks"))
    body = without_quota_checks(
        [c for c in (brain.get("is") or {}).get("checks") or [] if isinstance(c, dict)]
    )

    def pending(checks: list[dict[str, Any]]) -> int:
        return sum(str(c.get("result", "")).upper() == "PENDING" for c in checks)

    return body if body and pending(body) < pending(stored) else stored


def _checks_line(checks: list[dict[str, Any]], row: dict[str, Any]) -> str:
    """BRAIN's verdict, and the checks behind a refusal."""
    if is_submitted(row):
        # Submitted says it all, and past submission some checks stay PENDING for good.
        return "Checks:"
    failed = [
        str(c.get("name"))
        for c in checks
        if str(c.get("result", "")).upper() in REFUSING
        and str(c.get("name", "")).upper() not in IGNORED_CHECKS
    ]
    if failed:
        return f"Checks: FAIL ({', '.join(failed)})"
    found = verdict(checks, row.get("simulation_mode"))
    if row.get("simulation_mode") == QUICK_MODE:
        return "Checks: QUICK (Quick mode runs no submission checks)"
    return {"submittable": "Checks: PASS", "pending": "Checks: PENDING"}.get(
        str(found), "Checks: —"
    )


async def copied_alphas(
    state: AppState,
    alpha_ids: list[str],
    *,
    glossaries: bool = True,
    failures: Sequence[dict[str, Any]] = (),
) -> CopiedAlphas:
    """These Alphas with everything BRAIN reports for them, highest Sharpe first: expression,
    description, classifications, Simulation Settings, Metrics, every period's figures with
    their breakdowns, every check, themes and pyramids. Then the Metric Glossary and every data
    field they read.

    An Alpha stored before its whole body was kept is read from BRAIN once. Without
    ``glossaries``, the Metric Glossary and the data fields are left out. ``failures`` are
    simulations that made no Alpha, each an ``expression``, its ``settings`` and BRAIN's
    ``error``, listed after the Alphas so a reader learns from those too.
    """
    ids = list(dict.fromkeys(alpha_ids))
    rows = await state.alphas.by_ids(ids)
    if missing := [a for a, row in rows.items() if not row.get("brain")]:
        await _read_bodies(state, missing)
        rows |= await state.alphas.by_ids(missing)
    schema = await state.metadata.cached_settings_schema()
    local = await _local_figures(state, [a for a in ids if a in rows])
    blocks: list[str] = []
    #: Each field read, by the region of the first Alpha reading it: where it is described.
    regions: dict[str, str] = {}
    unsynced = 0

    # Highest Sharpe first; an Alpha without one last.
    def by_sharpe(row: dict[str, Any]) -> float:
        sharpe = row.get("sharpe")
        return math.inf if sharpe is None else -float(sharpe)

    for row in sorted((rows[a] for a in ids if a in rows), key=by_sharpe):
        alpha_id = str(row["alpha_id"])
        settings = {name: row.get(column) for name, column, _ in COPIED_SETTINGS}
        unsynced += row.get("language") is None
        expression = str(row.get("expression") or "")
        brain = _brain_of(row)
        lines = [
            "Alpha Expression:",
            expression,
            "Classifications:",
            ", ".join(sorted(json_list(row.get("classifications")), key=str.casefold)) or "None",
            "Simulation Settings:",
            *_settings_lines(schema, settings | {"instrumentType": row.get("instrument_type")}),
        ]
        figures = brain_metrics(row) | local.get(alpha_id, {})
        figures["imbalance"] = _imbalance(figures["long_count"], figures["short_count"])
        returns, drawdown = figures["returns"], figures["drawdown"]
        # As the Alpha tables work it out: no ratio over a Drawdown of nothing.
        figures["calmar"] = returns / drawdown if returns is not None and drawdown else None
        lines.append("Metrics:")
        checks = _settled_checks(row, brain)
        measured = {
            str(c.get("name")): v for c in checks if isinstance(v := c.get("value"), int | float)
        }
        for key, title, shown in COPIED_METRICS:
            value = figures.get(key)
            lines.append(f"{title}: {'—' if value is None else shown(value)}")
            added: set[str] = set()
            for name, (after, label, form) in CHECK_METRICS.items():
                if after == key and name in measured and label not in added:
                    added.add(label)
                    lines.append(f"{label}: {form(measured[name])}")
        constrained = (brain.get("is") or {}).get("investabilityConstrained")
        if isinstance(constrained, dict) and constrained.get("sharpe") is not None:
            lines.append("Investability Constrained Metrics:")
            for key, title, shown in CONSTRAINED_FIGURES:
                value = constrained.get(key)
                lines.append(f"{title}: {shown(value) if isinstance(value, int | float) else '—'}")
        listed = [f"- {_check_text(c)}" for c in checks if c.get("name") in COPIED_CHECKS]
        verdict_line = _checks_line(checks, row)
        if listed or verdict_line != "Checks:":
            lines += [verdict_line, *listed]
        blocks.append("\n".join(lines))
        for field in _fields_in(expression):
            regions.setdefault(field, str(row.get("region")))
    for failure in failures:
        expression = str(failure.get("expression") or "")
        blocks.append(
            "\n".join(
                [
                    "Alpha Expression:",
                    expression,
                    "Simulation Settings:",
                    *_settings_lines(schema, failure.get("settings") or {}),
                    f"Simulation Error: {failure.get('error') or 'BRAIN gave no reason.'}",
                ]
            )
        )
        for field in _fields_in(expression):
            regions.setdefault(field, str((failure.get("settings") or {}).get("region")))
    if not glossaries:
        return CopiedAlphas(text="\n\n---\n\n".join(blocks), unsynced=unsynced)
    described: dict[str, dict[str, Any]] = {}
    for region in set(regions.values()):
        wanted = [f for f, r in regions.items() if r == region]
        described |= await state.queries.described(wanted, region)
    datasets = await state.queries.datasets_described(
        sorted({str(d["dataset_id"]) for d in described.values() if d.get("dataset_id")})
    )
    blocks.append(METRIC_GLOSSARY)
    if regions:
        blocks.append(_glossary(list(regions), described, datasets))
    return CopiedAlphas(text="\n\n---\n\n".join(blocks), unsynced=unsynced)


@router.post("/markdown")
async def markdown(body: CopyRequest, state: State) -> CopiedAlphas:
    """These Alphas as BRAIN shows them; see :func:`copied_alphas`."""
    return await copied_alphas(state, body.alpha_ids)


@router.post("/compute")
async def compute(body: PortfolioRequest, state: State) -> PortfolioResult:
    """These Alphas combined at equal weight: stats, yearly rows, partitions, correlations."""
    ids = list(dict.fromkeys(body.alpha_ids))
    series = await state.alphas.series(ids)
    missing = [a for a in ids if a not in series]
    meta = await state.alphas.by_ids(ids)
    found = await asyncio.to_thread(portfolio.compute, series, meta, ids, body.cost_bps)
    return PortfolioResult.model_validate(found | {"missing": missing})
