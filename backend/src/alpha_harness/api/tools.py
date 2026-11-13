"""Tools: helpers that act on an Alpha the consultant already has.

The Settings Sampler re-runs one proven expression everywhere BRAIN will accept it. The
Correlation Breaker does the opposite — it holds the settings still and re-shapes the
expression, for an Alpha the platform says is already in the production pool. Neither
simulates on its own: previews only read, and queueing hands work to the scheduler like any
lab.
"""

from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from ..brain.platform import (
    DEFAULT_TRUNCATION,
    MAX_DECAY,
    MULTI_SIMULATION_SIZE,
    REGION_AGNOSTIC_REGION,
    SIMULATION_SLOTS,
    TEST_PERIOD_PATTERN,
)
from ..brain.schemas import QUICK_MODE
from ..db.models import Study, Trial, TrialState, utcnow
from ..labs.launch import NO_NEUTRALIZATION, AddedTask, add_study
from ..labs.params import (
    CORRELATION_BREAKER,
    FIELDS_SAMPLER,
    PASTE_ALPHAS,
    SETTINGS_SAMPLER,
    BreakerParams,
    FieldsSamplerParams,
    PasteParams,
    SettingsParams,
)
from ..labs.search import MAX_SIMULATIONS
from ..schemas import Out
from ..tools import correlation_breaker, fields_sampler, paste_alphas, settings_sampler
from .deps import State, refuse
from .portfolio import CopiedAlphas, copied_alphas
from .vault import AlphaSettings

router = APIRouter(prefix="/api/tools", tags=["tools"])


class Pair(Out):
    max_trade: str
    max_position: str


class MarketRow(Out):
    region: str
    delay: int
    universe: str
    #: Of the thinnest data field the expression reads. BRAIN sometimes reports zero for
    #: markets that simulate fine (DEU, GBR), so it is shown, never used to exclude.
    coverage: float
    #: Simulations this market is worth: its neutralizations times its legal pairs.
    total: int
    #: What the Truncation Agent sets here.
    agent_truncation: float


class RegionPlan(Out):
    region: str
    delays: list[int]
    universes: list[str]
    neutralizations: list[str]
    pairs: list[Pair]
    #: Whether BRAIN accepts Max Position here.
    position_available: bool
    #: Simulations of the day's allowance one run here uses: 4 in All Regions, else 1.
    cost: int
    markets: list[MarketRow]
    total: int


class PlanTotals(Out):
    #: Of the day's allowance, All Regions counted at its own cost.
    total: int
    batches: int


class SourceSettings(Out):
    region: str | None
    universe: str | None
    delay: int | None
    neutralization: str | None
    decay: int | None
    truncation: float | None
    pasteurization: str
    nan_handling: str
    test_period: str
    max_trade: str
    max_position: str


class SettingsPlan(Out):
    alpha_id: str
    expression: str
    data_fields: list[str]
    #: Read by the expression but not counted as data by BRAIN, e.g. ``industry``.
    grouping_fields: list[str]
    settings: SourceSettings
    regions: list[RegionPlan]
    totals: PlanTotals
    #: Most concurrent slots one task may hold: the engine's own, so a lone sweep can use it all.
    max_cores: int
    problems: list[str]
    warnings: list[str]


class PreviewRequest(BaseModel):
    """An Alpha to read, or a bare expression with the decay and truncation to hold it at."""

    alpha_id: str = Field(default="", max_length=64, alias="alphaId")
    expression: str | None = Field(default=None, max_length=20_000)
    #: Each one left out keeps the source Alpha's own value, or the platform default when
    #: there is no Alpha.
    decay: int | None = Field(default=None, ge=0, le=MAX_DECAY)
    truncation: float | None = Field(default=None, ge=0, le=1)
    pasteurization: Literal["ON", "OFF"] | None = None
    nan_handling: Literal["ON", "OFF"] | None = Field(default=None, alias="nanHandling")
    #: ``P{years}Y{months}M0D``, the shape BRAIN's own field takes; its bounds are
    #: ``P0Y0M0D`` to ``P6Y0M0D``.
    test_period: str | None = Field(default=None, alias="testPeriod", pattern=TEST_PERIOD_PATTERN)

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if bool(self.alpha_id.strip()) == bool((self.expression or "").strip()):
            raise ValueError("Give either an Alpha ID or an expression.")
        return self

    async def plan(self, state: Any) -> dict[str, Any]:
        return await settings_sampler.plan(
            state,
            self.alpha_id.strip(),
            expression=self.expression.strip() if self.expression else None,
            decay=self.decay,
            truncation=self.truncation,
            pasteurization=self.pasteurization,
            nan_handling=self.nan_handling,
            test_period=self.test_period,
        )


class MarketPick(BaseModel):
    region: str
    delay: int
    universe: str


class PairPick(BaseModel):
    max_trade: Literal["ON", "OFF"] = Field(alias="maxTrade")
    max_position: Literal["ON", "OFF"] = Field(alias="maxPosition")

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _one_at_most(self) -> Self:
        # BRAIN refuses the pair outright; catching it here costs no round trip.
        if self.max_trade == "ON" and self.max_position == "ON":
            raise ValueError("Max Trade and Max Position cannot both be ON.")
        return self


class SampleRequest(PreviewRequest):
    """What to queue. An empty market or pair list means "everything the plan offers"; an
    empty neutralization list is refused, since nobody chose what to run."""

    markets: list[MarketPick] = Field(default_factory=list, max_length=500)
    neutralizations: list[str] = Field(default_factory=list, max_length=50)
    pairs: list[PairPick] = Field(default_factory=list, max_length=4)
    #: Bounded by the engine rather than by ``search.MAX_CORES``, which is the labs' own cap.
    #: The real ceiling is the engine's slot count, checked in the route where it is known.
    cores: int = Field(default=SIMULATION_SLOTS, ge=1, le=SIMULATION_SLOTS)
    #: Drop the one combination that is not market neutral -- ``NONE`` neutralization with
    #: neither Max Trade nor Max Position. On by default: those simulations cost the same as
    #: any other and produce an Alpha carrying the market's own direction.
    market_neutral_only: bool = Field(default=True, alias="marketNeutralOnly")
    #: Truncation set per market by the Truncation Agent rather than held at one value.
    truncation_agent: bool = Field(default=False, alias="truncationAgent")
    #: Every simulation run once at each of these instead of at ``decay`` alone.
    decays: list[Annotated[int, Field(ge=0, le=MAX_DECAY)]] = Field(
        default_factory=list[int], max_length=10
    )
    #: Off unless asked for, whatever the source Alpha ran with: it slows every simulation, and
    #: BRAIN keeps its charts for a week.
    visualization: bool = False


@router.post("/settings-sampler/preview")
async def preview(body: PreviewRequest, state: State) -> SettingsPlan:
    """Where one Alpha's expression could also run, and what that would cost.

    Reads the Alpha from BRAIN rather than the vault, because the vault stores no
    maxTrade/maxPosition and may not hold the Alpha at all.
    """
    found = await body.plan(state)
    return SettingsPlan.model_validate({**found, "maxCores": state.engine.slots})


@router.post("/settings-sampler/tasks", status_code=201)
async def add_task(body: SampleRequest, state: State) -> AddedTask:
    """Add the chosen settings as a background task."""
    if not body.neutralizations:
        raise refuse(422, "no_neutralization", NO_NEUTRALIZATION)
    found = await body.plan(state)
    if found["problems"]:
        raise refuse(422, "settings_sampler_blocked", found["problems"][0])

    # Nothing chosen means every market, except All Regions: at four simulations a run it is
    # only swept when asked for by name.
    chosen = {(m.region, m.delay, m.universe) for m in body.markets} or {
        (str(m["region"]), int(m["delay"]), str(m["universe"]))
        for r in found["regions"]
        if r["region"] != REGION_AGNOSTIC_REGION
        for m in r["markets"]
    }
    source: dict[str, Any] = {
        **found["settings"],
        "expression": found["expression"],
        "visualization": body.visualization,
    }
    requests = settings_sampler.expand(
        found["regions"],
        chosen,
        set(body.neutralizations),
        {(p.max_trade, p.max_position) for p in body.pairs},
        source,
        market_neutral_only=body.market_neutral_only,
        truncation_agent=body.truncation_agent,
        decays=body.decays,
    )
    if not requests:
        raise refuse(
            422,
            "no_simulations",
            "Nothing to run: choose a market, a neutralization and a "
            "Max Trade / Max Position pair.",
        )

    markets = len(chosen)
    return await add_study(
        state,
        now=utcnow(),
        sampler=SETTINGS_SAMPLER,
        params=SettingsParams(
            region=str(found["settings"]["region"] or ""),
            delay=int(found["settings"]["delay"] or 0),
            alpha_id=body.alpha_id,
            markets=markets,
            decay=int(found["settings"]["decay"] or 0),
            decays=sorted(set(body.decays)),
            truncation=(
                DEFAULT_TRUNCATION
                if found["settings"]["truncation"] is None
                else float(found["settings"]["truncation"])
            ),
            truncation_agent=body.truncation_agent,
            nan_handling=str(found["settings"]["nanHandling"] or "ON"),
            test_period=str(found["settings"]["testPeriod"] or ""),
            visualization=body.visualization,
            cores=body.cores,
        ),
        simulations=len(requests),
        # One batch more than the cores can run, so a finished batch is replaced from the
        # queue on the engine's next 2s tick instead of waiting out the scheduler's 5s poll.
        # Measured: without the spare, 29% of this task's slot-time sat idle.
        batch_size=(body.cores + 1) * MULTI_SIMULATION_SIZE,
        template_source=found["expression"],
        template_name=f"Settings Sampler · {body.alpha_id or 'Expression'}",
        seeds=settings_sampler.seed_trials(requests),
    )


# --- Correlation Breaker ----------------------------------------------------


class BreakerRecipe(Out):
    id: str
    name: str
    #: Written by the consultant, kept on this computer, rather than built in.
    custom: bool = False
    why: str
    #: What it costs, when it costs something worth knowing before running it.
    caution: str
    #: Only the re-shape, written against ``alpha``; the binding is shown once, above.
    transform: str
    #: Why it cannot run here, empty when it can.
    blocked: str
    #: The whole program, counted the way Power Pool counts it.
    operators: int
    data_fields: int
    #: For a Power Pool Alpha, the limit this re-shape would cross; empty when none is.
    over_power_pool: str
    #: For a single dataset Alpha, the fields of other datasets this re-shape reads, which
    #: would cost it that classification; empty when none do.
    over_single_dataset: str = ""
    #: This account's runs of it whose Production Correlation BRAIN has settled, and of those
    #: the ones that passed it and stayed submittable.
    judged: int = 0
    worked: int = 0
    #: Run often enough here without once working that it starts unticked.
    gave_up: bool = False


class BreakerPlan(Out):
    alpha_id: str
    expression: str
    #: The source Alpha reduced to ``alpha``, shown once above the re-shapes.
    bound: str
    #: What every simulation runs at: the source Alpha's own, never varied.
    settings: AlphaSettings
    #: BRAIN's own production-correlation check, as it last reported it.
    correlation: dict[str, Any] | None
    #: BRAIN judges it as a Power Pool Alpha, so its re-shapes are held to the pool's limits.
    power_pool: bool
    #: BRAIN classes it a Single Data Set Alpha, so a re-shape reading another dataset is
    #: flagged.
    single_dataset: bool = False
    #: The source expression's own counts; null when it could not be read.
    operators: int | None
    data_fields: int | None
    recipes: list[BreakerRecipe]
    problems: list[str]


class CustomReshape(BaseModel):
    """A re-shape the consultant wrote around ``alpha``, or a built-in one edited."""

    #: The card's own id, which the recipe is chosen by as ``custom:{id}``.
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    # Kept loose: a blank or overlong one is refused on its own card, which stays there to fix.
    name: str = Field(max_length=10_000)
    code: str = Field(max_length=100_000)


class BreakerRequest(BaseModel):
    alpha_id: str = Field(min_length=1, max_length=64, alias="alphaId")
    #: Which recipes to run; empty means every one the plan offers.
    recipes: list[str] = Field(default_factory=list, max_length=100)
    #: Re-shapes of the consultant's own, offered beside the built-in ones.
    custom: list[CustomReshape] = Field(default_factory=list, max_length=50)
    cores: int = Field(default=SIMULATION_SLOTS, ge=1, le=SIMULATION_SLOTS)

    model_config = {"populate_by_name": True}

    def plan(self, state: Any) -> Any:
        return correlation_breaker.plan(
            state, self.alpha_id.strip(), [(c.id, c.name.strip(), c.code) for c in self.custom]
        )


@router.post("/correlation-breaker/preview")
async def breaker_preview(body: BreakerRequest, state: State) -> BreakerPlan:
    """The Alpha, the settings its re-shapes will hold, and every recipe's expression.

    Free: reads the Alpha and the catalog, simulates nothing.
    """
    return BreakerPlan.model_validate(await body.plan(state))


@router.post("/correlation-breaker/tasks")
async def breaker_task(body: BreakerRequest, state: State) -> AddedTask:
    """Queue one simulation per chosen recipe, every one at the Alpha's own settings."""
    found = await body.plan(state)
    if found["problems"]:
        raise refuse(422, "correlation_breaker_blocked", found["problems"][0])

    wanted = set(body.recipes)
    # A recipe the plan marked blocked is never queued, whether or not it was asked for.
    runnable = {r["id"] for r in found["recipes"] if not r["blocked"]}
    chosen = [r for r in found["recipeSet"] if r.id in runnable and (not wanted or r.id in wanted)]
    if not chosen:
        raise refuse(
            422,
            "no_simulations",
            "Nothing to run: every chosen re-shape needs an operator or a field this market "
            "does not have.",
        )

    compressed = correlation_breaker.compress(found["expression"])
    requests = correlation_breaker.requests(compressed, chosen, found["rawSettings"])
    settings = found["settings"]
    return await add_study(
        state,
        now=utcnow(),
        sampler=CORRELATION_BREAKER,
        params=BreakerParams(
            region=str(settings["region"] or ""),
            delay=int(settings["delay"] or 0),
            alpha_id=body.alpha_id,
            universe=str(settings["universe"] or ""),
            neutralization=str(settings["neutralization"] or ""),
            decay=int(settings["decay"] or 0),
            truncation=(
                DEFAULT_TRUNCATION
                if settings["truncation"] is None
                else float(settings["truncation"])
            ),
            recipes=[r.id for r in chosen],
            cores=body.cores,
        ),
        simulations=len(requests),
        batch_size=(body.cores + 1) * MULTI_SIMULATION_SIZE,
        template_source=found["expression"],
        template_name=f"Correlation Breaker · {body.alpha_id}",
        seeds=settings_sampler.seed_trials(requests),
    )


# -- Fields Sampler ------------------------------------------------------------


class SwapCandidate(Out):
    field: str
    dataset: str
    #: Where Similar found it: ``dataset``, ``subcategory``, ``category`` or ``market``;
    #: ``bucket`` for Buckets, whose list is in pick order.
    tier: str
    coverage: float | None


class FieldSlot(Out):
    #: A data field the Alpha reads.
    field: str
    #: What it can be swapped for, in the order they would run.
    candidates: list[SwapCandidate]
    #: Why it has none; empty when it has some.
    problem: str


class SwapCount(Out):
    field: str
    count: int


class ProvenSettings(Out):
    """Another set of settings the same expression is submitted at."""

    alpha_id: str
    status: str | None
    settings: AlphaSettings
    raw_settings: dict[str, Any]
    setting_labels: dict[str, str]
    #: Each of the Alpha's fields, with how many swaps it has in this market.
    swaps: list[SwapCount]


class FieldsPlan(Out):
    alpha_id: str
    expression: str
    #: What every simulation runs at: the source Alpha's own, never varied.
    settings: AlphaSettings
    #: Every simulation setting the Alpha has, as BRAIN names them, and each chosen one by
    #: BRAIN's label for it: all shown, none editable.
    raw_settings: dict[str, Any]
    setting_labels: dict[str, str]
    slots: list[FieldSlot]
    #: The same expression's other submitted Alphas, each at its own settings.
    others: list[ProvenSettings]
    #: Whether it reads several fields of one dataset, so Buckets can swap them together.
    bucketable: bool
    problems: list[str]


class FieldsRequest(BaseModel):
    alpha_id: str = Field(min_length=1, max_length=64, alias="alphaId")
    mode: fields_sampler.Mode = "sweep"
    #: Similar: the most fields taken from each tier. Buckets: the picks from each bucket.
    k: int = Field(default=10, ge=1, le=50)
    #: Which of the Alpha's fields to swap first; empty means every one.
    fields: list[str] = Field(default_factory=list, max_length=50)
    #: Other Alphas of the same expression whose settings each swap also runs at.
    alphas: list[str] = Field(default_factory=list, max_length=50)
    #: Every simulation the task may run, the winners' sweeps included; none set is no limit.
    #: Under the first sweep, only that many of it run, and nothing is left to grow from.
    budget: int | None = Field(default=None, ge=1, le=MAX_SIMULATIONS)
    cores: int = Field(default=SIMULATION_SLOTS, ge=1, le=SIMULATION_SLOTS)

    model_config = {"populate_by_name": True}


@router.post("/fields-sampler/preview")
async def fields_preview(body: FieldsRequest, state: State) -> FieldsPlan:
    """The Alpha, the settings it holds, and what each of its fields could be swapped for.

    Free: reads the Alpha and the catalog, simulates nothing.
    """
    found = await fields_sampler.plan(state, body.alpha_id.strip(), body.mode, body.k)
    return FieldsPlan.model_validate(found)


@router.post("/fields-sampler/tasks")
async def fields_task(body: FieldsRequest, state: State) -> AddedTask:
    """Queue the chosen fields' swaps, and let every winner be swept the same way after."""
    found = await fields_sampler.plan(state, body.alpha_id.strip(), body.mode, body.k)
    if found["problems"]:
        raise refuse(422, "fields_sampler_blocked", found["problems"][0])
    wanted = set(body.fields)
    fields = [s["field"] for s in found["slots"] if not wanted or s["field"] in wanted]
    include = set(body.alphas)
    sources = [found["rawSettings"]] + [
        o["rawSettings"] for o in found["others"] if o["alphaId"] in include
    ]
    bucketed = body.mode == fields_sampler.BUCKETS
    if bucketed:
        # Every field changes at once, so every field is in the grid.
        fields = [s["field"] for s in found["slots"]]
    seen: set[fields_sampler.Identity] = set()
    groups = []
    for held in sources:
        swaps = await fields_sampler.swaps_at(state.queries, held, fields, body.mode, body.k)
        groups.append(
            fields_sampler.grid(found["expression"], held, swaps, body.k, seen)
            if bucketed
            else fields_sampler.variants(found["expression"], held, swaps, seen)
        )
    chosen = fields_sampler.taking_turns(groups, MULTI_SIMULATION_SIZE)
    if not chosen:
        raise refuse(422, "no_simulations", "Nothing to run: no chosen field has a swap.")
    # None set runs until no passing swap leads anywhere new. A Sweep of one field leads
    # nowhere: a winner's sweep is the same dataset again, so the first sweep is the task.
    # Buckets grows no tree, so its grid is the task too.
    one_sweep = body.mode == fields_sampler.SWEEP and len(found["slots"]) == 1
    budget = body.budget or (len(chosen) if one_sweep or bucketed else MAX_SIMULATIONS)
    # A budget under the first sweep runs its best-covered swaps, every field taking turns.
    chosen = chosen[:budget]
    settings = found["settings"]
    return await add_study(
        state,
        now=utcnow(),
        sampler=FIELDS_SAMPLER,
        params=FieldsSamplerParams(
            region=str(settings["region"] or ""),
            delay=int(settings["delay"] or 0),
            alpha_id=body.alpha_id,
            universe=str(settings["universe"] or ""),
            neutralization=str(settings["neutralization"] or ""),
            decay=int(settings["decay"] or 0),
            truncation=(
                DEFAULT_TRUNCATION
                if settings["truncation"] is None
                else float(settings["truncation"])
            ),
            mode=body.mode,
            k=body.k,
            fields=fields,
            alphas=[o["alphaId"] for o in found["others"] if o["alphaId"] in include],
            cores=body.cores,
        ),
        simulations=budget,
        batch_size=(body.cores + 1) * MULTI_SIMULATION_SIZE,
        template_source=found["expression"],
        template_name=f"Fields Sampler · {body.alpha_id}"
        + (f" +{len(sources) - 1}" if len(sources) > 1 else ""),
        seeds=fields_sampler.seed_trials(chosen),
    )


# -- Paste Alphas --------------------------------------------------------------


class PasteRequest(BaseModel):
    #: An LLM's output: each Alpha an ``Alpha Expression:`` with its settings below it.
    text: str = Field(min_length=1, max_length=2_000_000)
    cores: int = Field(default=SIMULATION_SLOTS, ge=1, le=SIMULATION_SLOTS)


class PastedAlpha(Out):
    #: Its place in the pasted text, from 1.
    number: int
    expression: str
    #: By BRAIN's names, as they will be sent.
    settings: dict[str, Any]
    #: Each chosen setting by BRAIN's label for it, e.g. ``Slow Factors`` for ``SLOW``.
    labels: dict[str, str]
    #: Dataset Categories it reads, grouping fields aside: over 2, BRAIN counts it toward no
    #: Genius pyramid.
    categories: list[str]
    #: Titles of the settings it left out, which take BRAIN's default.
    defaulted: list[str]
    #: Why it cannot run; empty when it can.
    problems: list[str]
    #: The earlier Alpha it repeats exactly, which runs once for both.
    duplicate_of: int | None


class PastePlan(Out):
    alphas: list[PastedAlpha]
    #: Simulations it would queue: every Alpha that can run, once.
    simulations: int
    #: The Multi-Simulations those fill.
    batches: int
    max_cores: int
    problems: list[str]


async def _paste_plan(state: Any, text: str) -> dict[str, Any]:
    metadata = state.metadata
    schema = await metadata.cached_settings_schema() or await metadata.refresh_metadata()
    found = paste_alphas.plan(schema, text)
    problems: list[str] = []
    if not found["alphas"]:
        problems.append('No Alpha found. Each one starts with a line reading "Alpha Expression:".')
    elif not found["requests"]:
        problems.append("None of the pasted Alphas can run as written.")
    elif len(found["requests"]) > MAX_SIMULATIONS:
        problems.append(f"At most {MAX_SIMULATIONS:,} Alphas can run in one task.")
    # Read where each Alpha runs: a field's dataset can differ between regions.
    read = {a["number"]: paste_alphas.fields_of(a["expression"]) for a in found["alphas"]}
    regions = {str(a["settings"].get("region")) for a in found["alphas"]}
    described = {
        region: await state.queries.described(
            sorted(
                {
                    f
                    for a in found["alphas"]
                    if a["settings"].get("region") == region
                    for f in read[a["number"]]
                }
            ),
            region,
        )
        for region in regions
    }
    for a in found["alphas"]:
        known = described[str(a["settings"].get("region"))]
        a["categories"] = sorted(
            {str(c) for f in read[a["number"]] if (c := (known.get(f) or {}).get("category_name"))}
        )
    return found | {"problems": problems}


@router.post("/paste-alphas/preview")
async def paste_preview(body: PasteRequest, state: State) -> PastePlan:
    """Every Alpha in the pasted text, its settings resolved. Free: simulates nothing."""
    found = await _paste_plan(state, body.text)
    return PastePlan.model_validate(
        found | {"simulations": len(found["requests"]), "maxCores": state.engine.slots}
    )


@router.post("/paste-alphas/tasks", status_code=201)
async def paste_task(body: PasteRequest, state: State) -> AddedTask:
    """Queue every pasted Alpha that can run, ordered to finish soonest."""
    found = await _paste_plan(state, body.text)
    if found["problems"]:
        raise refuse(422, "paste_alphas_blocked", found["problems"][0])
    requests = found["requests"]
    first = requests[0].settings
    return await add_study(
        state,
        now=utcnow(),
        sampler=PASTE_ALPHAS,
        params=PasteParams(
            region=first.region,
            delay=first.delay,
            markets=len({markets_of(r) for r in requests}),
            cores=body.cores,
        ),
        simulations=len(requests),
        batch_size=(body.cores + 1) * MULTI_SIMULATION_SIZE,
        # The output as pasted, so the task keeps what it was asked to run.
        template_source=body.text,
        template_name=f"Paste Alphas · {len(requests)} Alphas",
        seeds=settings_sampler.seed_trials(requests),
    )


def markets_of(request: Any) -> tuple[str, int, str]:
    return request.settings.region, request.settings.delay, request.settings.universe


class PasteResults(CopiedAlphas):
    #: Alphas in the text, simulations that failed, and ones not back yet.
    alphas: int
    failed: int
    waiting: int
    #: Alphas whose PnL is still downloading: their K-Ratio and correlations read "—".
    downloading: int


@router.post("/paste-alphas/tasks/{task_id}/copy")
async def paste_copy(task_id: int, state: State) -> PasteResults:
    """The task's results as Copy Alphas writes them, each with its checks, then the
    simulations that failed with BRAIN's reason. No glossaries: the LLM already has them."""
    async with state.db.session() as session:
        row = await session.get(Study, task_id)
        if row is None or row.sampler != PASTE_ALPHAS:
            raise refuse(404, "not_found", "No Paste Alphas task with that id.")
        trials = (
            await session.scalars(
                select(Trial).where(Trial.study_id == task_id).order_by(Trial.number)
            )
        ).all()
    # A Quick Alpha that passed is run again in Full; the Full one is what BRAIN submits.
    full_runs = {
        int(t.params["fullOf"]): t for t in trials if (t.params or {}).get("fullOf") is not None
    }
    alpha_ids: list[str] = []
    failures: list[dict[str, Any]] = []
    waiting = 0
    for trial in trials:
        if (trial.params or {}).get("fullOf") is not None:
            continue
        full = full_runs.get(trial.number)
        alpha_id = (full.alpha_id if full is not None else None) or trial.alpha_id
        if alpha_id:
            alpha_ids.append(alpha_id)
        elif trial.state == TrialState.FAIL:
            failures.append(
                {"expression": trial.expression, "settings": trial.settings, "error": trial.message}
            )
        elif trial.state != TrialState.PRUNED or trial.message == settings_sampler.PENDING_SEND:
            waiting += 1
    stored = await state.alphas.by_ids(alpha_ids)
    lacking = [
        a
        for a in await state.alphas.lacking_series(alpha_ids)
        # A region-agnostic Alpha has no PnL of its own to download, and a Quick one is never
        # downloaded: its Full run, when it passes, is the one copied.
        if (stored.get(a) or {}).get("region") != REGION_AGNOSTIC_REGION
        and (stored.get(a) or {}).get("simulation_mode") != QUICK_MODE
    ]
    state.backfill.schedule_returns(lacking)
    # The LLM already holds both glossaries from the Alphas it was first given.
    copied = await copied_alphas(state, alpha_ids, glossaries=False, failures=failures)
    return PasteResults(
        text=copied.text,
        unsynced=copied.unsynced,
        alphas=len(alpha_ids),
        failed=len(failures),
        waiting=waiting,
        downloading=len(lacking),
    )
