"""The alpha vault: every alpha you have run, its checks, and its daily returns."""

import itertools
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ..brain.filters import AlphaQuery
from ..brain.platform import INSTRUMENT_TYPE, PLATFORM_ALPHA_URL
from ..schemas import Out
from ..tools import portfolio
from ..vault import yields
from ..vault.yields import checks_of
from .deps import State, refuse

router = APIRouter(prefix="/api/vault", tags=["vault"])


class VaultCounts(Out):
    alphas: int
    with_returns: int
    daily_rows: int
    scored: int
    missing_returns: int


class VaultScope(BaseModel):
    """A scope holding alphas, in the store's own snake_case column names."""

    instrument_type: str | None
    region: str
    delay: int | None
    universe: str | None
    alphas: int
    scored: int
    best_sharpe: float | None


class LastSync(Out):
    state: Literal["done", "failed", "cancelled"]
    imported: int
    #: Only when it failed.
    error: str | None = None
    finished_at: str


class VaultOverview(Out):
    counts: VaultCounts
    scopes: list[VaultScope]
    #: Every tag the stored Alphas carry, for the Stored table's filter.
    tags: list[str]
    backfilling: bool
    last_sync: LastSync | None


class AlphaSettings(Out):
    region: str | None
    universe: str | None
    delay: int | None
    neutralization: str | None
    decay: int | None
    truncation: float | None


class SubmittableAlpha(Out):
    alpha_id: str
    expression: str | None
    lab: str | None
    date_created: str | None
    settings: AlphaSettings
    sharpe: float | None
    fitness: float | None
    turnover: float | None
    returns: float | None
    drawdown: float | None
    margin: float | None
    #: Sharpe over the train years and over the held-out test years, when there was a test.
    train_sharpe: float | None
    test_sharpe: float | None
    #: BRAIN's own check results, as stored.
    checks: list[dict[str, Any]]
    #: At most 120 points.
    pnl: list[float]
    brain_url: str


class SubmittableMarket(Out):
    instrument_type: str
    region: str
    delay: int
    universe: str
    count: int


class SubmittableResponse(Out):
    #: At most five distinct Alphas that held up in the test years, most stable first.
    shortlist: list[SubmittableAlpha]
    alphas: list[SubmittableAlpha]
    total: int
    markets: list[SubmittableMarket]
    #: Still being judged by the platform.
    pending: int
    #: Submittable but left out for an implausible Sharpe (beyond ±10 in any period).
    implausible_hidden: int = 0
    #: Failing one or two fixable checks.
    near_misses: int
    #: Why submittable Alphas are not on the shortlist.
    held_out_failed: int
    unvalidated: int
    correlated_pruned: int


class AlphaRow(Out):
    alpha_id: str
    name: str | None
    type: str | None
    status: str | None
    region: str | None
    universe: str | None
    delay: int | None
    neutralization: str | None
    decay: int | None
    truncation: float | None
    expression: str | None
    sharpe: float | None
    fitness: float | None
    turnover: float | None
    returns: float | None
    drawdown: float | None
    margin: float | None
    operator_count: int | None
    calmar: float | None
    date_created: str | None
    date_submitted: str | None
    has_pnl: bool
    long_count: int | None = None
    short_count: int | None = None
    max_trade: str | None = None
    max_position: str | None = None
    #: BRAIN's names, e.g. "Power Pool Alpha", and pyramids such as "USA/D1/PV".
    classifications: list[str] = Field(default_factory=list)
    pyramids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    train_sharpe: float | None = None
    test_sharpe: float | None = None
    #: The limit each of BRAIN's figure checks names for this Alpha, by check name.
    limits: dict[str, float] = Field(default_factory=dict)
    #: Picking seeds only: the region a region-agnostic Alpha's figures come from.
    standing_region: str | None = None


class AlphaPage(Out):
    total: int
    results: list[AlphaRow]


class SyncStarted(Out):
    task_id: str
    since: str | None


class AlphaDetail(Out):
    alpha_id: str
    expression: str | None
    settings: AlphaSettings
    sharpe: float | None
    checks: list[dict[str, Any]]
    brain_url: str


class AlphaPnl(Out):
    #: Cumulative PnL, one value per stored trading day.
    pnl: list[float]
    #: ``YYYY-MM-DD`` for each value in ``pnl``.
    dates: list[str]
    days: int
    #: Why ``pnl`` is empty, when the download failed.
    problem: str | None


@router.get("")
async def overview(state: State) -> VaultOverview:
    """How much of your history is stored, and where it is."""
    return VaultOverview.model_validate(
        {
            "counts": await state.alphas.counts(),
            "scopes": await state.alphas.scopes(),
            "tags": await state.alphas.tags(),
            "backfilling": state.backfill.busy,
            "lastSync": state.backfill.last,
        }
    )


@router.get("/submittable")
async def submittable(
    state: State,
    region: str | None = None,
    delay: int | None = None,
    universe: str | None = None,
    instrument_type: str = INSTRUMENT_TYPE,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    shortlist: bool = True,
    implausible: bool = False,
) -> SubmittableResponse:
    """Alphas that passed every submission check, ready to submit on BRAIN. ``shortlist=false``
    asks for the counts alone, without the shortlist and the PnL it reads.

    Each entry carries the platform's own check results, the numbers behind them, and a
    link to the alpha on BRAIN — which is where it gets submitted. This application never does.
    """
    found = await yields.submittable(
        state.db,
        state.alphas,
        region=region,
        delay=delay,
        universe=universe,
        instrument_type=instrument_type,
        limit=limit,
        shortlist=shortlist,
        show_implausible=implausible,
    )
    # Without a series a candidate cannot be de-correlated against the others.
    state.backfill.schedule_returns(found.pop("pnlMissing"))
    return SubmittableResponse.model_validate(found)


# --- the simulations table ------------------------------------------------


class AlphaPageRequest(BaseModel):
    submitted: bool = False
    sort_by: str = "date_created"
    sort_desc: bool = True
    regions: list[str] | None = None
    delays: list[int] | None = None
    universes: list[str] | None = None
    minimum: dict[str, float] = Field(default_factory=dict)
    maximum: dict[str, float] = Field(default_factory=dict)
    search: str | None = None
    #: Alphas carrying any of these tags, or all of them with ``tags_all``.
    tags: list[str] | None = None
    tags_all: bool = False
    #: Days on BRAIN's clock, both included.
    created_after: date | None = None
    created_before: date | None = None
    #: Only Alphas the Evolution Lab can breed from, for when this table is a seed picker.
    evolvable: bool = False
    limit: int = Field(default=100, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


@router.post("/alphas/query")
async def query_alphas(body: AlphaPageRequest, state: State) -> AlphaPage:
    """The Simulations table: stored alphas, sorted and filtered locally."""
    return AlphaPage.model_validate(await state.alphas.page(**body.model_dump()))


class AfterCostRequest(BaseModel):
    alpha_ids: list[str] = Field(max_length=10_000)
    cost_bps: float = Field(gt=0, le=100)


class NetFigures(Out):
    sharpe: float | None
    fitness: float | None
    returns: float | None
    drawdown: float | None
    margin: float | None
    train_sharpe: float | None
    test_sharpe: float | None
    k_ratio: float | None


class AfterCost(Out):
    #: By Alpha, only those whose daily PnL and turnover are stored here; nothing is downloaded.
    figures: dict[str, NetFigures]


#: The most Alphas one analysis reads, as the Portfolio's own combination takes.
GROUP_LIMIT = 2000
#: A tag as BRAIN keeps them: letters, digits, "_" and "-".
TAG = r"^[A-Za-z0-9_-]{1,40}$"


class AlphaGroup(Out):
    """Every Alpha a filter keeps, for analysing them together."""

    alpha_ids: list[str]
    #: More matched than one analysis reads; the newest are the ones listed.
    more: bool


class TaggedRequest(BaseModel):
    tags: list[Annotated[str, Field(pattern=TAG)]] = Field(min_length=1, max_length=20)


class AlphaIdList(BaseModel):
    alpha_ids: list[str] = Field(min_length=1, max_length=GROUP_LIMIT, alias="alphaIds")

    model_config = {"populate_by_name": True}


class JobStarted(Out):
    task_id: str


@router.post("/alphas/group")
async def alpha_group(body: AlphaPageRequest, state: State) -> AlphaGroup:
    """The Alphas a filter keeps, newest first, ready to be analysed as one portfolio."""
    found = await state.alphas.matching_ids(
        limit=GROUP_LIMIT + 1,
        submitted=body.submitted,
        regions=body.regions,
        delays=body.delays,
        universes=body.universes,
        minimum=body.minimum,
        maximum=body.maximum,
        search=body.search,
        tags=body.tags,
        tags_all=body.tags_all,
        created_after=body.created_after,
        created_before=body.created_before,
    )
    return AlphaGroup(alpha_ids=found[:GROUP_LIMIT], more=len(found) > GROUP_LIMIT)


@router.post("/import-tagged")
async def import_tagged(body: TaggedRequest, state: State) -> JobStarted:
    """Bring in every Alpha BRAIN lists under any of these tags, made here or elsewhere."""
    try:
        task_id = await state.backfill.start_tagged(list(dict.fromkeys(body.tags)))
    except RuntimeError as exc:
        raise refuse(409, "already_running", str(exc)) from exc
    return JobStarted(task_id=task_id)


@router.post("/download-pnl")
async def download_pnl(body: AlphaIdList, state: State) -> JobStarted:
    """Download the daily PnL and turnover these Alphas lack, so they can be analysed."""
    alpha_ids = list(dict.fromkeys(body.alpha_ids))

    async def work(task: Any) -> str:
        lacking = await state.alphas.lacking_series(alpha_ids)
        failed = await state.backfill.fetch_each(
            task, lacking, state.backfill.fetch_returns, what="PnL and turnover"
        )
        done = len(lacking) - len(failed)
        return f"Downloaded {done} of {len(lacking)}" + (
            f"; {len(failed)} could not be read" if failed else ""
        )

    try:
        task_id = await state.backfill.start_job("group-pnl", "PnL for an Alpha group", work)
    except RuntimeError as exc:
        raise refuse(409, "already_running", str(exc)) from exc
    return JobStarted(task_id=task_id)


@router.post("/alphas/after-cost")
async def after_cost(body: AfterCostRequest, state: State) -> AfterCost:
    """Each Alpha's figures worked out again on its daily PnL net of ``cost_bps``, charged on
    each day's own turnover, as the Portfolio page works them out."""
    net = await portfolio.net_figures_of(state.alphas, body.alpha_ids, body.cost_bps)
    return AfterCost(figures={a: NetFigures.model_validate(f) for a, f in net.items()})


@router.post("/sync")
async def sync_alphas(state: State) -> SyncStarted:
    """Bring the stored alphas up to date with BRAIN.

    Incremental once the store holds as many alphas as BRAIN reports — only alphas newer
    than the newest stored, with a day of overlap because the platform's date filter works
    in whole days. Otherwise everything is listed, which also finishes an interrupted sync.
    The listing only: metrics, but no daily PnL and no submission checks.
    """
    remote = await state.endpoints.list_alphas(AlphaQuery(limit=1, hidden=None))
    stored = await state.alphas.stored_count()
    latest = await state.alphas.latest_created()
    complete = latest is not None and stored >= int(remote["count"])
    since = latest - timedelta(days=1) if complete and latest else None
    try:
        task_id = await state.backfill.start(since=since)
    except RuntimeError as exc:
        raise refuse(409, "already_running", str(exc)) from exc
    return SyncStarted(task_id=task_id, since=since.isoformat() if since else None)


@router.get("/alphas/{alpha_id}/detail")
async def alpha_detail(alpha_id: str, state: State) -> AlphaDetail:
    """One stored alpha: its expression, settings and checks. Local only, so it answers at once;
    the PnL comes from :func:`alpha_pnl`, which may have to download it."""
    row = (await state.alphas.by_ids([alpha_id])).get(alpha_id)
    if row is None:
        raise refuse(
            404, "unknown_alpha", f"{alpha_id} is not stored here yet. Sync from BRAIN first."
        )
    return AlphaDetail.model_validate(
        {
            "alphaId": alpha_id,
            "expression": row.get("expression"),
            "sharpe": row.get("sharpe"),
            "settings": {
                k: row.get(k)
                for k in ("region", "universe", "delay", "neutralization", "decay", "truncation")
            },
            "checks": checks_of(row.get("checks")),
            "brainUrl": f"{PLATFORM_ALPHA_URL}{alpha_id}",
        }
    )


@router.get("/alphas/{alpha_id}/pnl")
async def alpha_pnl(alpha_id: str, state: State) -> AlphaPnl:
    """One alpha's cumulative PnL. Downloads the daily PnL from BRAIN the first time."""
    problem = None
    if await state.alphas.series_length(alpha_id) == 0:
        try:
            await state.backfill.fetch_returns(alpha_id)
        # Any failure is shown to the user as the reason the chart is empty.
        except Exception as exc:  # noqa: BLE001
            problem = f"Could not download the daily PnL: {exc}"
    rows = await state.alphas.pnl_series(alpha_id)
    # Stored rows are daily PnL; the chart wants the running total, every day with its date.
    values = [round(total, 2) for total in itertools.accumulate(float(r["pnl"]) for r in rows)]
    return AlphaPnl(
        pnl=values, dates=[str(r["date"]) for r in rows], days=len(values), problem=problem
    )
