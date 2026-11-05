"""Evolution Lab: choose seed Alphas, cores and simulations, then add the breeding to Tasks.

Nothing here runs a search: a task is added not started and is run from Tasks. Auto Select
reads the local store and downloads daily PnL where it is missing; it never simulates.
"""

import asyncio
import time
from typing import TYPE_CHECKING, Any, Literal

import structlog
from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..brain.platform import INSTRUMENT_TYPE, TEST_PERIOD
from ..db.models import Trial, TrialState, utcnow
from ..labs import ga, scheduler, search
from ..labs.launch import (
    NO_NEUTRALIZATION,
    NO_SIMULATIONS,
    OPERATORS_UNREAD,
    AddedTask,
    SampleAlpha,
    account_operators,
    add_study,
    neutralizations_for,
    preview_samples,
)
from ..labs.params import GA_SAMPLER, EvolutionParams
from ..labs.study import vault_summary
from ..schemas import Out
from ..tasks import spawn
from .deps import State, refuse

if TYPE_CHECKING:
    import random
    from collections.abc import Sequence

    from ..brain.schemas import SimulationRequest

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/evolution-lab", tags=["evolution-lab"])

POPULATIONS = (50, 100, 200)
MUTATION_RATES = (0.03, 0.05, 0.08)
MAX_SEEDS = 100


class EvolutionRequest(BaseModel):
    region: str
    delay: int = Field(ge=0, le=1)
    universe: str
    alpha_ids: list[str] = Field(default_factory=list, max_length=MAX_SEEDS)
    #: Empty keeps the lab's default four; anything here is what the genes may take.
    neutralizations: list[str] = Field(default_factory=list, max_length=20)
    cores: int = Field(default=search.MAX_CORES, ge=1, le=search.MAX_CORES)
    #: ``None`` sizes the population from the simulations.
    population: Literal[50, 100, 200] | None = None
    mutation_rate: float = Field(default=ga.MUTATION_RATE, ge=0.01, le=0.2)
    #: Needed to add a task; a preview uses it to size the population.
    simulations: int = Field(default=0, ge=0, le=search.MAX_SIMULATIONS)


class AutoSeedsRequest(BaseModel):
    region: str
    delay: int = Field(ge=0, le=1)
    universe: str
    count: int = Field(default=50, ge=2, le=MAX_SEEDS)


class EvolutionMarket(Out):
    """A market holding unsubmitted Alphas, and how many."""

    region: str
    delay: int
    universe: str
    alphas: int


class EvolutionOptions(Out):
    markets: list[EvolutionMarket]
    populations: list[int]
    mutation_rates: list[float]
    max_simulations: int


class SeedRow(Out):
    alpha_id: str
    expression: str
    sharpe: float | None
    fitness: float | None
    turnover: float | None
    returns: float | None
    drawdown: float | None
    margin: float | None
    score: float | None
    #: The limit each of BRAIN's figure checks names for this Alpha, by check name.
    limits: dict[str, float] = Field(default_factory=dict)


class SeedReason(Out):
    alpha_id: str
    reason: str


class EvolutionPreview(Out):
    seeds: list[SeedRow]
    skipped: list[SeedReason]
    population: int
    generations: int
    sample: list[SampleAlpha]
    problems: list[str]


class AutoSeedsStarted(Out):
    job_id: str


class AutoSeeds(Out):
    seeds: list[SeedRow]
    wanted: int
    pool: int
    examined: int
    reasons: list[SeedReason]


class AutoSeedsJob(Out):
    state: Literal["running", "done", "failed", "cancelled"]
    progress: float | None
    detail: str
    error: str | None
    result: AutoSeeds | None


async def _market(
    state: Any, region: str, delay: int, universe: str, chosen: Sequence[str] = ()
) -> tuple[ga.Market | None, list[str]]:
    """The market children are bred in, and why it cannot be used if it cannot.

    ``chosen`` is the neutralizations a gene may take. Empty keeps the lab's default, which
    only Auto Select relies on: a task with none chosen is refused in ``_plan``.
    """
    problems: list[str] = []
    operators = await account_operators(state, refresh=False)
    if not operators:
        problems.append(OPERATORS_UNREAD)
    neutralizations = await neutralizations_for(state, region, delay, chosen)
    if not neutralizations:
        problems.append(
            f"BRAIN offers none of the chosen neutralizations in {region} delay {delay}."
            if chosen and await state.metadata.cached_settings_schema()
            else "BRAIN's settings list is not loaded. Sign in again."
        )
    market = await ga.market_for(state.catalog, operators, region, delay, universe, neutralizations)
    if market is None and operators:
        problems.append(
            f"{region} delay {delay} {universe} is not downloaded. Sync it in the Data Explorer."
        )
    return market, problems


@router.get("/options")
async def options(state: State) -> EvolutionOptions:
    markets = await state.alphas.evolvable_markets()
    return EvolutionOptions.model_validate(
        {
            "markets": [
                {
                    "region": m["region"],
                    "delay": int(m["delay"]),
                    "universe": m["universe"],
                    "alphas": int(m["alphas"]),
                }
                for m in markets
            ],
            "populations": list(POPULATIONS),
            "mutationRates": list(MUTATION_RATES),
            "maxSimulations": search.MAX_SIMULATIONS,
        }
    )


async def _plan(body: EvolutionRequest, state: Any) -> dict[str, Any]:
    """Everything a task would breed from, checked, without queueing anything."""
    market, problems = await _market(
        state, body.region, body.delay, body.universe, body.neutralizations
    )
    if not body.neutralizations:
        problems.append(NO_NEUTRALIZATION)
    ids = list(dict.fromkeys(body.alpha_ids))
    # A region-agnostic parent stands as its family: the region it would be scored on.
    rows = await state.alphas.with_families(await state.alphas.by_ids(ids))
    seeds: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for alpha_id in ids:
        row = rows.get(alpha_id)
        reason = ga.seed_problem(row, body.region, body.delay, body.universe, market)
        if reason:
            skipped.append({"alphaId": alpha_id, "reason": reason})
        else:
            seeds.append(row)
    seeds.sort(key=ga.seed_score, reverse=True)
    if ids and len(seeds) < 2:
        problems.append("Choose at least 2 seeds this market can breed from.")

    sample: list[SampleAlpha] = []
    if market is not None and len(seeds) >= 2 and not problems:
        run = EvolutionParams(region=body.region, delay=body.delay, universe=body.universe)
        parents = [p for r in seeds if (p := ga.Parent.of(str(r["alpha_id"]), r["expression"], r))]
        bred = market
        shapes = {p.tree: ga.Shape.of(p.tree, bred) for p in parents}

        def draw(rng: random.Random) -> SimulationRequest | None:
            a, b = rng.choice(parents), rng.choice(parents)
            made = ga.child(rng, a, b, bred, body.mutation_rate, shapes)
            return ga.request_for(*made, run) if made else None

        sample = preview_samples(draw, tries=50)

    population = ga.population_for(body.simulations, body.population)
    return {
        "seeds": seeds,
        "skipped": skipped,
        "population": population,
        "generations": body.simulations // population,
        "sample": sample,
        "problems": problems,
        "neutralizations": list(market.neutralizations) if market else [],
    }


@router.post("/preview")
async def preview(body: EvolutionRequest, state: State) -> EvolutionPreview:
    """What a task would breed from. Free; queues nothing."""
    plan = await _plan(body, state)
    return EvolutionPreview.model_validate(
        plan | {"seeds": [ga.seed_row(r) for r in plan["seeds"]]}
    )


#: Auto Select jobs by id: the task reporting progress, its market and, once done, its result.
_jobs: dict[str, dict[str, Any]] = {}
#: How long a finished Auto Select stays readable, so a screen polling it still gets its seeds.
JOB_TTL_SECONDS = 600.0
#: Held from the busy check to registration: both sides await, so two requests could interleave.
_jobs_lock = asyncio.Lock()


@router.post("/seeds/auto", status_code=202)
async def auto_seeds(body: AutoSeedsRequest, state: State) -> AutoSeedsStarted:
    """Choose seeds in the background: the best, mutually uncorrelated unsubmitted Alphas."""
    wanted = (body.region, body.delay, body.universe)
    async with _jobs_lock:
        for job_id, job in _jobs.items():
            if job["task"].state == "running":
                if job["market"] == wanted:
                    return AutoSeedsStarted(job_id=job_id)
                raise refuse(
                    409, "busy", "Auto Select is already choosing seeds for another market."
                )
        market, problems = await _market(state, *wanted)
        if market is None or problems:
            raise refuse(422, "no_market", (problems or ["This market cannot breed."])[0])

        # Finished jobs are kept for a while rather than cleared: a screen still reading the
        # last run's seeds was told to start again the moment another market was chosen.
        cutoff = time.monotonic() - JOB_TTL_SECONDS
        ended = {i: j["task"].finished for i, j in _jobs.items() if j["task"].finished is not None}
        for stale in [i for i, finished in ended.items() if finished < cutoff]:
            del _jobs[stale]
        task = await state.tasks.start("evolution-seeds", "Choosing seeds")
        job: dict[str, Any] = {
            "task": task,
            "market": wanted,
            "result": None,
        }
        _jobs[task.id] = job

    async def report(kept: int, examined: int) -> None:
        detail = f"{kept} of {body.count} seeds · {examined} examined"
        await state.tasks.update(task, progress=kept / body.count, detail=detail)

    async def run() -> None:
        try:
            found = await ga.auto_seeds(state, market, *wanted, body.count, report)
            job["result"] = {
                "seeds": [ga.seed_row(r) for r in found["rows"]],
                "wanted": body.count,
                "pool": found["pool"],
                "examined": found["examined"],
                "reasons": found["reasons"],
            }
            await state.tasks.finish(task)
        except asyncio.CancelledError:
            await state.tasks.finish(task, state="cancelled")
            raise
        except Exception as exc:
            log.exception("evolution.auto_seeds_failed")
            await state.tasks.finish(task, state="failed", error=str(exc)[:300])

    spawn(run(), name="evolution-seeds")
    return AutoSeedsStarted(job_id=task.id)


@router.get("/seeds/auto/{job_id}")
async def auto_seeds_job(job_id: str) -> AutoSeedsJob:
    job = _jobs.get(job_id)
    if job is None:
        raise refuse(404, "unknown_job", "That Auto Select is no longer known. Start it again.")
    task = job["task"]
    return AutoSeedsJob.model_validate(
        {
            "state": task.state,
            "progress": task.progress,
            "detail": task.detail,
            "error": task.error,
            "result": job["result"],
        }
    )


@router.post("/tasks", status_code=201)
async def add_task(body: EvolutionRequest, state: State) -> AddedTask:
    """Add the breeding to Tasks, not started. It spends nothing until it is run there."""
    if body.simulations < 1:
        raise refuse(422, "no_simulations", NO_SIMULATIONS)
    plan = await _plan(body, state)
    if plan["problems"]:
        raise refuse(422, "evolution_blocked", plan["problems"][0])
    seeds = plan["seeds"]
    if len(seeds) < 2:
        raise refuse(422, "no_seeds", "Choose at least 2 seeds.")

    now = utcnow()

    def generation_zero(study_id: int) -> list[Trial]:
        """The seeds, scored on their stored Train Fitness: nothing is simulated.

        Train Fitness, as the children are: whole-period Fitness includes the held-out
        test years, so ranking seeds on it would let those years pick the parents. A seed
        simulated without a test period has no train numbers and falls back to whole-period
        Fitness.
        """
        trials: list[Trial] = []
        for number, seed in enumerate(seeds):
            settings = {
                "instrumentType": INSTRUMENT_TYPE,
                "region": body.region,
                "delay": body.delay,
                "universe": body.universe,
                "neutralization": seed.get("neutralization"),
                "decay": seed.get("decay"),
                "truncation": seed.get("truncation"),
            }
            trials.append(
                Trial(
                    study_id=study_id,
                    number=number,
                    params={"parents": [], "generation": 0},
                    distributions={},
                    expression=seed["expression"],
                    settings={k: v for k, v in settings.items() if v is not None},
                    state=TrialState.COMPLETE,
                    values=[
                        float(
                            seed["fitness"]
                            if seed.get("train_fitness") is None
                            else seed["train_fitness"]
                        )
                    ],
                    result=vault_summary(seed),
                    alpha_id=str(seed["alpha_id"]),
                    generation=0,
                    message=scheduler.FREE,
                    finished_at=now,
                )
            )
        return trials

    return await add_study(
        state,
        now=now,
        sampler=GA_SAMPLER,
        params=EvolutionParams(
            region=body.region,
            delay=body.delay,
            universe=body.universe,
            cores=body.cores,
            seeds=[str(r["alpha_id"]) for r in seeds],
            population=plan["population"],
            mutation_rate=body.mutation_rate,
            test_period=TEST_PERIOD,
            neutralizations=plan["neutralizations"],
        ),
        simulations=body.simulations,
        batch_size=body.cores * 10,
        template_source="# Evolution Lab breeds from seed Alphas; there is no template.",
        seeds=generation_zero,
    )
