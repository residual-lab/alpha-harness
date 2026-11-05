"""Portfolio: submitted Alphas combined at equal weight, as BRAIN combines its own pool."""

import asyncio
import math
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ..brain.platform import REGION_AGNOSTIC_CLASS, TRADING_DAYS
from ..brain.settings_schema import chosen_labels
from ..labs.fastexpr import ParseError, data_fields, parse
from ..schemas import Out
from ..tools import portfolio, power_pool
from ..vault.store import SERIES_VERSION, json_list
from ..vault.yields import checks_of, limits_of
from .deps import State, refuse

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
    """:data:`BRAIN_METRICS` for one stored Alpha, and its Information Ratio."""
    found = {
        name: None if row.get(c) is None else float(row[c]) for name, c in BRAIN_METRICS.items()
    }
    sharpe = found["sharpe"]
    # BRAIN's Sharpe is its IR annualized over 250 days, so this is exact to Sharpe's two places.
    found["information_ratio"] = None if sharpe is None else sharpe / math.sqrt(TRADING_DAYS)
    return found


#: What a copied Alpha lists under Metrics, and how each reads: percentages to BRAIN's two
#: places. All from :func:`brain_metrics`.
COPIED_METRICS = (
    ("information_ratio", "Information Ratio", lambda v: f"{v:.4f}"),
    ("turnover", "Turnover", lambda v: f"{v:.2%}"),
    ("returns", "Returns", lambda v: f"{v:.2%}"),
    ("drawdown", "Drawdown", lambda v: f"{v:.2%}"),
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


@router.get("/members")
async def members(
    state: State,
    cost_bps: Annotated[float | None, Query(alias="costBps", gt=0, le=100)] = None,
) -> PortfolioMembers:
    """Every submitted Alpha stored locally. With ``costBps``, its figures and correlations are
    worked out on its daily PnL net of that cost instead of read from BRAIN."""
    rows = await state.alphas.submitted_members()
    ids = [str(r["alpha_id"]) for r in rows]
    series = await state.alphas.series(ids)
    meta = await state.alphas.by_ids(ids)
    # Laid out once for everything below: it was each one's slowest step.
    with_series = [a for a in dict.fromkeys(ids) if series.get(a)]
    framed = await asyncio.to_thread(portfolio.frame, series, meta, with_series, cost_bps or 0.0)
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


@router.post("/markdown")
async def markdown(body: CopyRequest, state: State) -> CopiedAlphas:
    """These Alphas as BRAIN shows them: expression, classifications and Simulation Settings,
    then a glossary of every data field they read."""
    ids = list(dict.fromkeys(body.alpha_ids))
    rows = await state.alphas.by_ids(ids)
    schema = await state.metadata.cached_settings_schema()
    blocks: list[str] = []
    #: Each field read, by the region of the first Alpha reading it: where it is described.
    regions: dict[str, str] = {}
    unsynced = 0

    # Highest Information Ratio first; an Alpha without one last.
    def by_ir(row: dict[str, Any]) -> float:
        ir = brain_metrics(row)["information_ratio"]
        return math.inf if ir is None else -ir

    for row in sorted((rows[a] for a in ids if a in rows), key=by_ir):
        settings = {name: row.get(column) for name, column, _ in COPIED_SETTINGS}
        labels = chosen_labels(schema, settings | {"instrumentType": row.get("instrument_type")})
        unsynced += row.get("language") is None
        expression = str(row.get("expression") or "")
        lines = [
            "Alpha Expression:",
            expression,
            "Classifications:",
            ", ".join(sorted(json_list(row.get("classifications")), key=str.casefold)) or "None",
            "Simulation Settings:",
        ]
        for name, _, title in COPIED_SETTINGS:
            value = settings[name]
            lines.append(f"{title}: {labels.get(name) or ('—' if value is None else value)}")
        figures = brain_metrics(row)
        lines.append("Metrics:")
        for key, title, shown in COPIED_METRICS:
            value = figures.get(key)
            lines.append(f"{title}: {'—' if value is None else shown(value)}")
        blocks.append("\n".join(lines))
        for field in _fields_in(expression):
            regions.setdefault(field, str(row.get("region")))
    described: dict[str, dict[str, Any]] = {}
    for region in set(regions.values()):
        wanted = [f for f, r in regions.items() if r == region]
        described |= await state.queries.described(wanted, region)
    datasets = await state.queries.datasets_described(
        sorted({str(d["dataset_id"]) for d in described.values() if d.get("dataset_id")})
    )
    if regions:
        blocks.append(_glossary(list(regions), described, datasets))
    return CopiedAlphas(text="\n\n---\n\n".join(blocks), unsynced=unsynced)


@router.post("/compute")
async def compute(body: PortfolioRequest, state: State) -> PortfolioResult:
    """These Alphas combined at equal weight: stats, yearly rows, partitions, correlations."""
    ids = list(dict.fromkeys(body.alpha_ids))
    series = await state.alphas.series(ids)
    missing = [a for a in ids if a not in series]
    meta = await state.alphas.by_ids(ids)
    found = await asyncio.to_thread(portfolio.compute, series, meta, ids, body.cost_bps)
    return PortfolioResult.model_validate(found | {"missing": missing})
