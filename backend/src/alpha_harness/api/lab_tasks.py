"""Tasks: what the labs added, run from one place.

A lab only adds a task. Here a task runs: it waits until its cores fit in the free slots,
runs until its simulations are spent, across days if it has to, and can be paused, changed,
continued, cloned or removed. It is never stopped: anything that would have ended it early
pauses it instead, to be resumed where it left off or cloned fresh.
"""

import asyncio
import contextlib
import itertools
import json
import math
import re
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update

from ..brain.errors import BrainError, BrainRateLimited
from ..db.models import (
    BrainCache,
    SimStatus,
    SimulationRecord,
    Study,
    StudyStatus,
    Trial,
    TrialState,
    utcnow,
)
from ..engine.slots import POSITIONAL_NOTE
from ..labs import power_pool as pool_lab
from ..labs import scheduler, search
from ..labs.launch import AddedTask, add_study
from ..labs.objectives import FAILURE, OBJECTIVES, StudyNotFoundError
from ..labs.params import (
    BY_SAMPLER,
    CORRELATION_BREAKER,
    GA_SAMPLER,
    LLM_SAMPLERS,
    REGION_AGNOSTIC_SAMPLER,
    SETTINGS_SAMPLER,
    TASK_SAMPLERS,
    TEMPLATE_SAMPLER,
    PowerPoolParams,
    task_params,
)
from ..labs.study import ranked
from ..schemas import Out
from ..tasks import Task
from ..tools import power_pool
from ..tools.settings_sampler import PENDING_SEND
from ..tools.submission_planner import ESCAPE
from ..vault.yields import checks_of, clean, is_submitted, verdict
from .deps import State, refuse
from .prompts import chosen

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/lab-tasks", tags=["lab-tasks"])

#: Labs that vary settings around one fixed expression, so ``template_source`` is that
#: expression rather than a skeleton to search.
ONE_EXPRESSION = frozenset({SETTINGS_SAMPLER, CORRELATION_BREAKER})

#: Where a task can be run from: never started, paused, or failed. A failed task may have
#: sent simulations that finished after it stopped; running it again scores them.
RUNNABLE = (StudyStatus.IDLE, StudyStatus.PAUSED, StudyStatus.FAILED)
FINISHED = (StudyStatus.COMPLETE, StudyStatus.FAILED)


class TaskChange(BaseModel):
    #: Up to the engine's slots, checked in the route: a lab starts at ``search.MAX_CORES``
    #: at most, but an edit may hand it the whole engine.
    cores: int | None = Field(default=None, ge=1)
    simulations: int | None = Field(default=None, ge=1, le=search.MAX_SIMULATIONS)


class TaskName(BaseModel):
    #: Blank clears it, back to the lab and template.
    name: str | None = Field(default=None, max_length=128)


class Failure(Out):
    reason: str
    count: int


#: Reasons a task's detail lists for simulations that returned no Alpha, most frequent first.
MAX_REASONS = 3


def _reason(message: str | None) -> str:
    return (message or "").replace(POSITIONAL_NOTE, "").strip() or "BRAIN gave no reason."


class LabTask(Out):
    id: int
    #: The name the user gave it, if any.
    name: str | None
    lab: str
    lab_name: str
    #: Template Lab only: the template's name and its skeleton.
    template_name: str | None
    template: str | None
    #: IDLE: not started. QUEUED: run, waiting for its cores to fit in the free slots.
    status: StudyStatus
    stopping: bool
    message: str | None
    region: str | None
    delay: int | None
    #: Settings Sampler only: what the sweep holds fixed, and where it came from.
    alpha_id: str | None = None
    markets: int | None = None
    truncation: float | None = None
    #: Settings Sampler: truncation set per market by the Truncation Agent.
    truncation_agent: bool = False
    nan_handling: str | None = None
    test_period: str | None = None
    #: The expression every simulation in the task ran, for the tasks that have one: the
    #: Settings Sampler and the Correlation Breaker vary settings around a fixed expression.
    #: Null for a lab that searches expressions, where there is no single answer.
    expression: str | None = None
    #: Evolution Lab only: its market's universe, seeds, population and mutation rate.
    universe: str | None
    seeds: int
    population: int | None
    mutation_rate: float | None
    #: Search Lab and Template Lab only.
    decay: int | None
    cores: int
    dataset_ids: list[str]
    #: The datasets' names, in the same order; the id where the catalog has no name.
    dataset_names: list[str] = Field(default_factory=list)
    #: Single fields the task was told to use, when it was; zero uses its datasets whole.
    chosen_fields: int = 0
    #: LLM Power Pool Lab only: the prompt it sends and the model it asks.
    prompt_name: str | None = None
    model: str | None = None
    fields: int
    target: int
    simulated: int
    #: Of ``simulated``, how many came back from the dedup cache without spending quota.
    cached: int
    #: Quick Alphas that passed and were simulated again in Full: quota spent on top of
    #: ``target``, which BRAIN needs before it will take them.
    full_runs: int = 0
    queued: int
    running: int
    failed: int
    #: Why simulations returned no Alpha, as BRAIN said it.
    failures: list[Failure]
    #: The best value of what the task searches for: Sharpe, or Train Fitness for Evolution Lab.
    best: float | None
    objective_label: str
    created_at: str | None
    #: When Run was pressed. A task then waits for its cores, which can be a long time and is
    #: not running time -- the two are shown apart rather than added together.
    queued_at: str | None = None
    #: When it first began running, which is where elapsed time is measured from.
    started_at: str | None
    finished_at: str | None


class LabTasks(Out):
    slots: int
    tasks: list[LabTask]


class TaskRemoved(Out):
    removed: int


class RankedAlpha(Out):
    trial_id: int
    #: Its place in the sweep, as queued.
    number: int
    alpha_id: str | None
    expression: str | None
    settings: dict[str, Any] | None
    value: float
    sharpe: float | None
    fitness: float | None
    turnover: float | None
    returns: float | None
    drawdown: float | None
    margin: float | None
    #: From the vault, which holds every simulated Alpha; a trial's own stats may lack them.
    long_count: int | None = None
    short_count: int | None = None
    #: The after-cost t-stat over sqrt(10): the Sharpe once 5 bps is charged against each
    #: day's own turnover, scaled by sqrt(years of data / 10) so fewer years prove less. Null
    #: until the daily PnL *and* turnover are stored.
    after_cost_sharpe: float | None = None
    #: The held-out test years, from the vault: where an Alpha that only fits its train years
    #: shows it. Shown, never searched on, so they stay out of sample. Null without a test period.
    test_sharpe: float | None = None
    test_fitness: float | None = None
    #: The highest correlation with any consultant Alpha in production, which BRAIN wants
    #: below 0.7. Null until BRAIN has been asked: it limits these checks per hour.
    prod_correlation: float | None = None
    feasible: bool | None
    failed_checks: list[str]
    #: Of ``failed_checks``, those that gate submission. A check BRAIN fails for a reason
    #: that is nothing to do with the Alpha is left out, so an empty list means submittable.
    refused_by: list[str]
    #: Already on the platform. It is still a result of this task, so it is listed, but it
    #: is no longer a candidate: its Power Pool correlation is measured against a pool it is
    #: itself in, and self-correlation is excluded, so the figure shown is its *next* closest.
    submitted: bool = False
    #: Nothing BRAIN has reported refuses it: no FAIL and no ERROR.
    submittable: bool
    #: Submittable only because a check has not answered yet. The Submission Planner waits for
    #: these rather than planning a permanent submission on them.
    pending: bool = False
    #: The Alpha the sweep started from, kept first as its reference point.
    source: bool = False
    #: Simulated in Quick mode: the figures stand, but BRAIN submits it only once it is
    #: simulated again in Full. The task does that for every one that passes, and the Full
    #: Alpha replaces it here once it is back.
    quick: bool = False
    #: A check that decides submission came back ERROR: BRAIN could not judge it, which is
    #: told apart from an Alpha a check refused.
    errored: bool = False


class WorkflowStarted(Out):
    task_id: str


class AlphaIds(BaseModel):
    """An explicit set of Alphas, for the panes that are not one task's results."""

    alpha_ids: list[str] = Field(min_length=1, max_length=20_000, alias="alphaIds")

    model_config = {"populate_by_name": True}


class PowerPoolRow(Out):
    """One of the task's Alphas, against the submitted Power Pool of its own region."""

    alpha_id: str
    region: str
    universe: str | None
    delay: int | None
    sharpe: float | None
    #: Its highest correlation with a pool member; null when none shared enough days.
    correlation: float | None
    #: Pool members it was compared against.
    against: int
    #: The member it correlates most with, and that member's Sharpe.
    closest: str | None
    closest_sharpe: float | None
    #: The Sharpe it would need to clear *every* collision at or above the ceiling, which is
    #: not always the closest one's. Null below the ceiling, where there is nothing to beat.
    needed: float | None
    #: ``clear`` below the ceiling, ``beats`` above it but past the Sharpe rule, ``blocked``
    #: above it without the Sharpe, ``unmeasured`` when no pair shared enough days.
    verdict: Literal["clear", "beats", "blocked", "unmeasured"]


class PowerPoolScope(Out):
    """One region: the pool its Alphas are measured against."""

    region: str
    #: The submitted Power Pool Alphas of this region, whatever their delay or universe.
    pool: list[str]
    #: Of those, with no stored series, so they could not be measured. A sync downloads them.
    pool_without_pnl: list[str]


class PowerPoolCorrelation(Out):
    """What the Power Pool panel shows for one task."""

    ceiling: float
    sharpe_edge: float
    #: Submitted Power Pool Alphas held locally, across every market.
    pool_size: int
    #: The task's Alphas BRAIN treats as Power Pool eligible, and the rest, which this
    #: measurement has nothing to say about.
    power_pool_alphas: int
    other_alphas: int
    #: Of the Power Pool ones, those with no stored series yet.
    without_pnl: list[str]
    #: One entry per measured Alpha, flat: the table that shows these is a list of Alphas,
    #: not a list of regions, and the region is on the row already.
    rows: list[PowerPoolRow]
    scopes: list[PowerPoolScope]


async def _rows(state: Any) -> list[Study]:
    async with state.db.session() as session:
        return list(
            (
                await session.scalars(
                    select(Study).where(Study.sampler.in_(TASK_SAMPLERS)).order_by(Study.id.desc())
                )
            ).all()
        )


async def _one(state: Any, task_id: int) -> Study:
    row = await state.optimizer.get(task_id)
    if row is None or row.sampler not in TASK_SAMPLERS:
        raise StudyNotFoundError(task_id)
    return row


async def _progress(state: Any, ids: list[int]) -> dict[int, dict[str, Any]]:
    """Trials by state, trials answered for free, and the best value per task, counted in SQL.

    A long task holds tens of thousands of trials; reading them all to count them would
    slow the list down with every day the task runs.
    """
    out: dict[int, dict[str, Any]] = {
        i: {"states": {}, "free": 0, "fullRuns": 0, "best": None, "failures": {}} for i in ids
    }
    if not ids:
        return out
    value = func.json_extract(Trial.values, "$[0]")
    async with state.db.session() as session:
        states = await session.execute(
            select(Trial.study_id, Trial.state, func.count())
            .where(Trial.study_id.in_(ids))
            .group_by(Trial.study_id, Trial.state)
        )
        for study_id, trial_state, n in states.all():
            out[study_id]["states"][str(trial_state)] = int(n)
        free = await session.execute(
            select(Trial.study_id, func.count())
            .where(
                Trial.study_id.in_(ids),
                Trial.state.in_([TrialState.COMPLETE, TrialState.FAIL]),
                Trial.message == scheduler.FREE,
            )
            .group_by(Trial.study_id)
        )
        for study_id, n in free.all():
            out[study_id]["free"] = int(n)
        failures = await session.execute(
            select(Trial.study_id, Trial.message, func.count())
            .where(Trial.study_id.in_(ids), Trial.state == TrialState.FAIL)
            .group_by(Trial.study_id, Trial.message)
        )
        for study_id, message, n in failures.all():
            found = out[study_id]["failures"]
            found[_reason(message)] = found.get(_reason(message), 0) + int(n)
        full_runs = await session.execute(
            select(Trial.study_id, func.count())
            .where(
                Trial.study_id.in_(ids),
                Trial.state.in_([TrialState.COMPLETE, TrialState.FAIL]),
                or_(Trial.message.is_(None), Trial.message != scheduler.FREE),
                scheduler.full_run(),
            )
            .group_by(Trial.study_id)
        )
        for study_id, n in full_runs.all():
            out[study_id]["fullRuns"] = int(n)
        # An Alpha that returned no value is scored at the failure value, so it is not a best;
        # nor is a seed, which was scored before the task began.
        best = await session.execute(
            select(Trial.study_id, func.max(value))
            .where(
                Trial.study_id.in_(ids),
                Trial.state == TrialState.COMPLETE,
                value > FAILURE,
                or_(Trial.generation.is_(None), Trial.generation != 0),
            )
            .group_by(Trial.study_id)
        )
        for study_id, value in best.all():
            out[study_id]["best"] = None if value is None else float(value)
    return out


def _fields(space: Any) -> int:
    """Fields a task searches: its pool, or each variable's for a typed Template Lab template."""
    if not isinstance(space, dict):
        return 0
    pooled = len(space.get("fields") or {})
    variables = space.get("variables")
    if pooled or not isinstance(variables, list):
        return pooled
    return sum(len(v.get("fields") or {}) for v in variables if isinstance(v, dict))


def _task(row: Study, progress: dict[str, Any], names: dict[str, str] | None = None) -> LabTask:
    # Read raw on purpose: this only displays, and one malformed old row must not take the
    # whole Tasks list down with a validation error.
    params = row.sampler_params or {}
    states = progress["states"]
    told = states.get(TrialState.COMPLETE, 0) + states.get(TrialState.FAIL, 0)
    template = row.sampler == TEMPLATE_SAMPLER
    objective = OBJECTIVES.get((row.objectives or ["sharpe"])[0], OBJECTIVES["sharpe"])
    return LabTask.model_validate(
        {
            "id": row.id,
            "name": row.label,
            "lab": row.sampler,
            "labName": TASK_SAMPLERS.get(row.sampler, row.sampler),
            "templateName": row.template_name if template else None,
            "template": row.template_source if template else None,
            "status": row.status,
            "stopping": bool(params.get("stopping")),
            "message": row.message,
            "region": params.get("region"),
            "delay": params.get("delay"),
            "universe": params.get("universe"),
            "seeds": len(params.get("seeds") or []),
            "population": params.get("population"),
            "mutationRate": params.get("mutationRate"),
            "decay": params.get("decay"),
            "alphaId": params.get("alphaId"),
            "markets": params.get("markets"),
            "truncation": params.get("truncation"),
            "truncationAgent": bool(params.get("truncationAgent")),
            "nanHandling": params.get("nanHandling"),
            "testPeriod": params.get("testPeriod"),
            "expression": row.template_source if row.sampler in ONE_EXPRESSION else None,
            "cores": scheduler.cores_of(row),
            "datasetIds": params.get("datasetIds") or [],
            "datasetNames": [(names or {}).get(d, d) for d in params.get("datasetIds") or []],
            "chosenFields": len(params.get("fieldIds") or []),
            "promptName": (params.get("promptName") or "Built-in")
            if row.sampler in LLM_SAMPLERS
            else None,
            "model": params.get("model") or None,
            "fields": _fields(params.get("space")),
            "target": row.max_trials,
            # What spent quota, as the scheduler counts toward the target.
            "simulated": told - progress["free"] - progress["fullRuns"],
            "cached": progress["free"],
            "fullRuns": progress["fullRuns"],
            "queued": states.get(TrialState.QUEUED, 0),
            "running": states.get(TrialState.RUNNING, 0),
            "failed": states.get(TrialState.FAIL, 0),
            "failures": [
                {"reason": reason, "count": n}
                for reason, n in sorted(progress["failures"].items(), key=lambda kv: -kv[1])[
                    :MAX_REASONS
                ]
            ],
            "best": progress["best"],
            "objectiveLabel": objective.label,
            "createdAt": row.created_at.isoformat() if row.created_at else None,
            "queuedAt": params.get("queuedAt"),
            "startedAt": row.started_at.isoformat() if row.started_at else None,
            "finishedAt": row.finished_at.isoformat() if row.finished_at else None,
        }
    )


async def _dataset_names(state: Any, rows: list[Study]) -> dict[str, str]:
    """Every dataset the tasks name, by id, as the catalog calls them. Empty when unsynced."""
    ids = sorted({d for r in rows for d in (r.sampler_params or {}).get("datasetIds") or []})
    if not ids:
        return {}
    found = await state.catalog.query(
        f"""
        SELECT dataset_id, any_value(name) AS name FROM data_set
        WHERE dataset_id IN ({", ".join("?" for _ in ids)}) GROUP BY dataset_id
        """,  # noqa: S608
        ids,
    )
    return {str(f["dataset_id"]): str(f["name"] or f["dataset_id"]) for f in found}


async def _payload(state: Any, task_id: int) -> LabTask:
    row = await _one(state, task_id)
    names = await _dataset_names(state, [row])
    return _task(row, (await _progress(state, [row.id]))[row.id], names)


@router.get("")
async def list_tasks(state: State) -> LabTasks:
    """Every task, newest first, and the slots they share."""
    rows = await _rows(state)
    progress = await _progress(state, [r.id for r in rows])
    names = await _dataset_names(state, rows)
    return LabTasks(slots=state.engine.slots, tasks=[_task(r, progress[r.id], names) for r in rows])


async def _queue(state: Any, ids: list[int]) -> None:
    """Hand tasks to the scheduler. Each starts as soon as its cores fit."""
    queued_at = utcnow().isoformat()
    # The same lock ``remove`` holds, so a task cannot be queued while it is being deleted.
    async with contextlib.AsyncExitStack() as stack:
        for task_id in sorted(ids):
            await stack.enter_async_context(state.optimizer.lock(task_id))
        async with state.db.session() as session:
            for task_id in ids:
                row = await session.get(Study, task_id)
                if row is not None and row.status in RUNNABLE:
                    # Whatever paused it is behind it now.
                    row.message = None
                    row.finished_at = None
                    row.status = StudyStatus.QUEUED
                    params = task_params(row)
                    params.queued_at = queued_at
                    params.stopping = False
                    if isinstance(params, PowerPoolParams):
                        # Its call allowance and empty-answer count start again, or the check
                        # that paused it would pause it again on the first tick.
                        llm = dict(params.llm)
                        llm |= {"empty": 0, "failed": 0, "capFrom": int(llm.get("calls") or 0)}
                        params.llm = llm
                        pool_lab.forget_retry(task_id)
                    row.sampler_params = params.dump()
            await session.commit()
    await scheduler.start_waiting(state.optimizer)
    await state.optimizer.notify()


@router.post("/run-all")
async def run_all(state: State) -> LabTasks:
    """Run every task not started yet, oldest first. What does not fit waits its turn."""
    fresh = sorted(r.id for r in await _rows(state) if r.status == StudyStatus.IDLE)
    await _queue(state, fresh)
    return await list_tasks(state)


# Registered before the ``/{task_id}`` routes: that path matches any single segment, so a
# literal one declared after it is never reached and answers 405 instead.
@router.post("/power-pool-workflow", status_code=202)
async def power_pool_workflow(body: AlphaIds, state: State) -> WorkflowStarted:
    """Narrow these Alphas in three steps, each on what the one before kept: unsubmitted, then
    no check FAIL or ERROR, then Power Pool Correlation passed. The survivors get After-Cost
    Sharpe.

    PnL alone decides Power Pool Correlation, so it comes first: two requests per Alpha (BRAIN
    answers the first with a Retry-After while it builds the series), and only in a region
    where a submitted Power Pool Alpha exists to collide with. Turnover and yearly-stats, one
    each, follow only for the Alphas that pass: After-Cost Sharpe on one the Power Pool
    refuses is requests spent on nothing. None of it spends quota.
    """
    alpha_ids = list(dict.fromkeys(body.alpha_ids))

    async def work(task: Task) -> str:
        stored = await state.alphas.by_ids(alpha_ids)
        unsubmitted = [a for a in alpha_ids if a in stored and not is_submitted(stored[a])]
        survivors = [a for a in unsubmitted if clean(checks_of(stored[a].get("checks")))]
        # Only an Alpha BRAIN judges as Power Pool is measured, so only its PnL is fetched.
        judged = [a for a in survivors if power_pool.is_power_pool(stored[a])]
        pool = await _pool_members(state)
        wanted = {power_pool.scope_of(stored[a]) for a in judged}
        contested = ({power_pool.scope_of(r) for r in pool.values()} & wanted) - {None}
        need = [a for a in judged if power_pool.scope_of(stored[a]) in contested] + [
            a for a, r in pool.items() if power_pool.scope_of(r) in contested
        ]
        failed = await state.backfill.fetch_each(
            task, await state.alphas.lacking_pnl(need), state.backfill.fetch_pnl, what="PnL"
        )
        found = await _power_pool(state, judged)
        passed = [r.alpha_id for r in found.rows if r.verdict in SATISFIED]
        failed += await state.backfill.fetch_each(
            task,
            await state.alphas.lacking_series(passed),
            state.backfill.fetch_returns,
            what="Turnover",
        )
        detail = (
            f"{len(unsubmitted)} Unsubmitted Alphas → {len(survivors)} with no Checks FAIL or "
            f"ERROR → {len(passed)} Pass Power Pool Correlation"
        )
        return detail + (f". {len(failed)} could not be downloaded" if failed else "")

    try:
        started = await state.backfill.start_job("power-pool-workflow", "Power Pool Workflow", work)
    except RuntimeError as exc:
        raise refuse(409, "already_running", str(exc)) from exc
    return WorkflowStarted(task_id=started)


#: The least to wait once BRAIN still refuses a correlation check after the client's own
#: retries: its limit is per hour, so asking again a minute later only spends another refusal.
PROD_WAIT = 300.0


def _prod_key(alpha_id: str) -> str:
    # The key the Alpha page keeps BRAIN's answer under, so either one fills the other.
    return f"correlation:prod:{alpha_id}"


async def _prod_correlations(
    state: State, alpha_ids: list[str], checks: dict[str, list[dict[str, Any]]]
) -> dict[str, float]:
    """Production Correlation where BRAIN has given one: a kept answer from its correlation
    check, else the PROD_CORRELATION submission check."""
    known: dict[str, float] = {}
    for alpha_id, rows in checks.items():
        for check in rows:
            value = check.get("value")
            if check.get("name") == "PROD_CORRELATION" and isinstance(value, int | float):
                known[alpha_id] = float(value)
    async with state.db.session() as session:
        for chunk in itertools.batched(alpha_ids, TRIAL_CHUNK, strict=False):
            kept = await session.execute(
                select(BrainCache.key, func.json_extract(BrainCache.body, "$.max")).where(
                    BrainCache.key.in_([_prod_key(a) for a in chunk])
                )
            )
            for key, value in kept.tuples():
                if isinstance(value, int | float):
                    known[key.removeprefix(_prod_key(""))] = float(value)
    return known


@router.post("/prod-correlation", status_code=202)
async def check_prod_correlation(body: AlphaIds, state: State) -> WorkflowStarted:
    """Ask BRAIN for the Production Correlation of each unsubmitted Alpha here it has not given
    one for, in the order sent. BRAIN limits these checks per hour, so a long list waits that
    out and takes hours. No simulation quota."""
    alpha_ids = list(dict.fromkeys(body.alpha_ids))

    async def work(task: Task) -> str:
        stored = await state.alphas.by_ids(alpha_ids)
        checks = {a: checks_of(r.get("checks")) for a, r in stored.items()}
        known = await _prod_correlations(state, list(stored), checks)
        wanted = [
            a for a in alpha_ids if a in stored and not is_submitted(stored[a]) and a not in known
        ]
        failed = 0
        for done, alpha_id in enumerate(wanted):
            progress = f"{done} of {len(wanted)}"
            await state.tasks.update(task, progress=done / len(wanted), detail=progress)
            while True:
                try:
                    answer = await state.endpoints.correlations(alpha_id, "prod")
                except BrainRateLimited as exc:
                    if not exc.retryable:
                        raise
                    wait = min(max(exc.retry_after or 0.0, PROD_WAIT), 3600.0)
                    minutes = math.ceil(wait / 60)
                    await state.tasks.update(
                        task, detail=f"{progress} · BRAIN's hourly limit, next in {minutes} min"
                    )
                    await asyncio.sleep(wait)
                    continue
                # One Alpha BRAIN cannot check must not abandon the rest.
                except BrainError as exc:
                    log.warning("prod_correlation.failed", alpha_id=alpha_id, error=exc.message)
                    failed += 1
                    break
                async with state.db.session() as session:
                    await session.merge(
                        BrainCache(key=_prod_key(alpha_id), body=answer, fetched_at=utcnow())
                    )
                break
        detail = f"Production Correlation for {len(wanted) - failed} Alphas"
        return detail + (f". BRAIN could not check {failed}" if failed else "")

    try:
        started = await state.backfill.start_job("prod-correlation", "Production Correlation", work)
    except RuntimeError as exc:
        raise refuse(409, "already_running", str(exc)) from exc
    return WorkflowStarted(task_id=started)


@router.post("/prod-correlation/stop", status_code=204)
async def stop_prod_correlation(state: State) -> None:
    """Stop asking BRAIN. Every answer already back is kept."""
    state.backfill.cancel_job("prod-correlation")


@router.post("/power-pool-correlation")
async def power_pool_correlation_for(body: AlphaIds, state: State) -> PowerPoolCorrelation:
    """The same measurement over an explicit set of Alphas, for the panes that span tasks."""
    return await _power_pool(state, body.alpha_ids)


@router.post("/{task_id}/run")
async def run(task_id: int, state: State) -> LabTask:
    """Run a task, resume a paused one, or retry a failed one. It starts once its cores fit."""
    row = await _one(state, task_id)
    if row.status not in RUNNABLE:
        raise refuse(409, "not_runnable", "Only a task not started, paused or failed can run.")
    await _queue(state, [task_id])
    return await _payload(state, task_id)


@router.post("/{task_id}/pause")
async def pause(task_id: int, state: State) -> LabTask:
    """Queue nothing more for now. Simulations already sent finish; the rest come off."""
    # Read under the lock: checked outside it, a task that finished its last trial in the
    # meantime was written back to PAUSED, and a finished task holds no cores.
    async with state.optimizer.lock(task_id):
        row = await _one(state, task_id)
        if row.status not in (StudyStatus.RUNNING, StudyStatus.QUEUED):
            raise refuse(409, "not_running", "Only a running or waiting task can pause.")
        await state.optimizer.set_status(task_id, StudyStatus.PAUSED)
        await state.engine.drop_queued(row.task)
        await scheduler.prune_unsent(state.optimizer, task_id)
    await scheduler.start_waiting(state.optimizer)
    return await _payload(state, task_id)


class ContinueTask(BaseModel):
    #: Simulations to add to its target. Zero carries on towards the target it has.
    simulations: int = Field(default=0, ge=0, le=search.MAX_SIMULATIONS)


@router.post("/{task_id}/continue")
async def continue_task(task_id: int, body: ContinueTask, state: State) -> LabTask:
    """Carry any task on from where it left off, with more simulations if it met its target.

    A paused, waiting-to-start or finished task all continue the same way: its trials, its
    memory and what it learnt stay, and it runs until the new target is met.
    """
    async with state.optimizer.lock(task_id):
        row = await _one(state, task_id)
        if row.status in (StudyStatus.RUNNING, StudyStatus.QUEUED):
            raise refuse(409, "running", "It is already running.")
        target = row.max_trials + body.simulations
        if target > search.MAX_SIMULATIONS:
            raise refuse(
                422, "too_many", f"A task takes at most {search.MAX_SIMULATIONS:,} simulations."
            )
        progress = (await _progress(state, [task_id]))[task_id]
        simulated = _task(row, progress).simulated
        if row.sampler in ONE_EXPRESSION:
            async with state.db.session() as session:
                parked = await session.scalar(
                    select(func.count()).where(
                        Trial.study_id == task_id,
                        Trial.state == TrialState.PRUNED,
                        Trial.message == PENDING_SEND,
                    )
                )
            if not parked and not (progress["states"].get(TrialState.QUEUED) or 0):
                raise refuse(
                    422,
                    "nothing_left",
                    "Every simulation it was written with has been sent. Clone it to run "
                    "them again.",
                )
        elif simulated >= target:
            raise refuse(
                422,
                "target_met",
                f"It has simulated its {row.max_trials:,}. Add simulations to continue it.",
            )
        async with state.db.session() as session:
            stored = await session.get(Study, task_id)
            if stored is not None:
                stored.max_trials = target
                # Runnable again, however it ended: _queue takes it from here.
                stored.status = StudyStatus.PAUSED
                await session.commit()
    await _queue(state, [task_id])
    return await _payload(state, task_id)


class CloneTask(BaseModel):
    #: Its target; omitted copies the original's.
    simulations: int | None = Field(default=None, ge=1, le=search.MAX_SIMULATIONS)
    #: LLM Power Pool Lab only: send another prompt. With ``prompt_id`` null, the built-in.
    change_prompt: bool = False
    prompt_id: int | None = None
    #: Queue it at once rather than leaving it Not Started.
    run: bool = False


@router.post("/{task_id}/clone", status_code=201)
async def clone(task_id: int, body: CloneTask, state: State) -> AddedTask:
    """A fresh task with this one's market, datasets, fields and settings, and nothing it did.

    Labs that write their simulations when added (Settings Sampler, Correlation Breaker) get
    them all again; Evolution Lab gets its seeds.
    """
    row = await _one(state, task_id)
    params = BY_SAMPLER[row.sampler].model_validate(row.sampler_params or {})
    params.queued_at = None
    params.stopping = False
    if isinstance(params, PowerPoolParams):
        params.llm = {"calls": 0}
        params.calls = []
        if body.change_prompt:
            name, system = await chosen(state, pool_lab.kind_of(params), body.prompt_id)
            params.prompt_id = body.prompt_id
            params.prompt_name = name if body.prompt_id is not None else None
            params.system = system if body.prompt_id is not None else None
    elif body.change_prompt:
        raise refuse(422, "no_prompt", "Only a task an LLM writes for sends a prompt.")

    async with state.db.session() as session:
        if row.sampler in ONE_EXPRESSION:
            written = (
                await session.scalars(
                    select(Trial)
                    .where(Trial.study_id == task_id, Trial.expression.is_not(None))
                    .order_by(Trial.number)
                )
            ).all()
        elif row.sampler == GA_SAMPLER:
            written = (
                await session.scalars(
                    select(Trial)
                    .where(Trial.study_id == task_id, Trial.generation == 0)
                    .order_by(Trial.number)
                )
            ).all()
        else:
            written = []
    now = utcnow()

    def seeds(study_id: int) -> list[Trial]:
        if row.sampler == GA_SAMPLER:
            return [
                Trial(
                    study_id=study_id,
                    number=i,
                    params=dict(t.params or {}),
                    distributions={},
                    expression=t.expression,
                    settings=dict(t.settings or {}),
                    state=t.state,
                    values=t.values,
                    result=t.result,
                    alpha_id=t.alpha_id,
                    generation=0,
                    message=t.message,
                    finished_at=now,
                )
                for i, t in enumerate(written)
            ]
        return [
            Trial(
                study_id=study_id,
                number=i,
                params={k: v for k, v in (t.params or {}).items() if k == "source"},
                distributions={},
                expression=t.expression,
                settings=dict(t.settings or {}),
                state=TrialState.PRUNED,
                message=PENDING_SEND,
            )
            for i, t in enumerate(written)
        ]

    return await add_study(
        state,
        now=now,
        sampler=row.sampler,
        params=params,
        simulations=body.simulations
        or (len(written) if row.sampler in ONE_EXPRESSION else 0)
        or row.max_trials,
        batch_size=row.batch_size,
        template_source=row.template_source,
        template_name=row.template_name,
        run=body.run,
        seeds=seeds if written else None,
    )


@router.patch("/{task_id}")
async def change(task_id: int, body: TaskChange, state: State) -> LabTask:
    """Change a task's cores or simulations. A running task takes them from its next round."""
    row = await _one(state, task_id)
    if row.status in FINISHED:
        raise refuse(409, "finished", "A finished task can't change.")
    if body.cores and body.cores > state.engine.slots:
        raise refuse(
            422,
            "too_many_cores",
            f"The engine has {state.engine.slots} slots, so a task cannot hold {body.cores}.",
        )
    cores = body.cores or scheduler.cores_of(row)
    free = await scheduler.resize_task(state.optimizer, row, cores, body.simulations)
    if free is not None:
        raise refuse(409, "no_cores", f"Only {free} more cores are free right now.")
    return await _payload(state, task_id)


@router.put("/{task_id}/name")
async def rename(task_id: int, body: TaskName, state: State) -> LabTask:
    """Name a task, whatever its state. Nothing it runs changes."""
    await _one(state, task_id)  # 404 for an unknown task
    async with state.db.session() as session:
        await session.execute(
            update(Study).where(Study.id == task_id).values(label=(body.name or "").strip() or None)
        )
    return await _payload(state, task_id)


@router.delete("/{task_id}")
async def remove(task_id: int, state: State, force: bool = False) -> TaskRemoved:
    """Remove a task. The Alphas it found stay in Alphas.

    ``force``, from a held Delete, takes a running task down as well: it is paused, what it
    has out on BRAIN is cancelled where BRAIN allows, and then it goes.
    """
    # Held across the check and the delete: a ``run`` landing between them queued work for a
    # task whose rows were then deleted under it, leaving simulations nothing would score.
    async with state.optimizer.lock(task_id):
        row = await _one(state, task_id)
        # Asks the simulation rows, not the trials: a task that failed mid-round never scores
        # its last trials, so they read RUNNING forever after their simulations finished.
        async with state.db.session() as session:
            out = await session.scalar(
                select(func.count())
                .select_from(SimulationRecord)
                .where(
                    SimulationRecord.task == row.task,
                    SimulationRecord.status.in_([SimStatus.PENDING, SimStatus.RUNNING]),
                )
            )
        if (row.status in (StudyStatus.RUNNING, StudyStatus.QUEUED) or out) and not force:
            raise refuse(409, "running", "Pause the task first: simulations are still out.")
        if force:
            await state.optimizer.set_status(task_id, StudyStatus.PAUSED)
            await state.engine.drop_queued(row.task)
            await state.engine.abandon(row.task)
            await scheduler.prune_unsent(state.optimizer, task_id, everything=True)
        await state.engine.drop_queued(row.task)
        await state.optimizer.delete(task_id)
        await state.engine.set_quota(row.task, 0, enabled=False)
    await state.optimizer.notify()
    return TaskRemoved(removed=task_id)


class TaskSetting(Out):
    label: str
    value: str


class TaskDataset(Out):
    id: str
    name: str
    #: Trials that spent a simulation on it, and how they ended.
    simulated: int
    complete: int
    failed: int
    best: float | None


class TaskCall(Out):
    """One LLM call of an LLM Power Pool Lab task."""

    at: str | None
    dataset: str | None
    prompt: str | None
    model: str | None
    fields: int | None
    valid: int | None
    rejected: int | None
    tokens: int | None
    error: str | None


class TaskInfo(Out):
    id: int
    name: str
    lab_name: str
    status: StudyStatus
    message: str | None
    settings: list[TaskSetting]
    datasets: list[TaskDataset]
    #: The single fields it was told to use, in rank order, and how they were ranked.
    fields: list[str]
    rank_by: str | None
    llm_calls: int
    recent_calls: list[TaskCall]
    created_at: str | None
    queued_at: str | None
    started_at: str | None
    finished_at: str | None
    updated_at: str | None


#: Stored run state and bulk that the info view lists elsewhere or not at all.
HIDDEN = frozenset(
    {
        "space",
        "tree",
        "calls",
        "llm",
        "system",
        "fieldIds",
        "datasetIds",
        "stopping",
        # Listed from the task itself, which a resize keeps current.
        "cores",
        "queuedAt",
        "nStartupTrials",
        "n_startup_trials",
        "promptId",
        "rankBy",
    }
)
LABELS = {
    "nanHandling": "NaN handling",
    "testPeriod": "Test period",
    "mutationRate": "Mutation rate",
    "alphaId": "Source Alpha",
    "promptName": "Prompt",
    "multivariate": "Multivariate sampler",
}


def _label(key: str) -> str:
    return LABELS.get(key) or re.sub(r"(?<!^)(?=[A-Z])", " ", key).replace("_", " ").capitalize()


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        items: list[Any] = list(value)  # pyright: ignore[reportUnknownArgumentType]
        return ", ".join(str(v) for v in items) or "None"
    if isinstance(value, dict):
        entries: dict[Any, Any] = dict(value)  # pyright: ignore[reportUnknownArgumentType]
        # Short maps read as they are, e.g. each region's universe; long ones are counted.
        if len(entries) <= 8 and all(not isinstance(v, (dict, list)) for v in entries.values()):
            return ", ".join(f"{k} {v}" for k, v in entries.items())
        return f"{len(entries)} entries"
    return str(value)


@router.get("/{task_id}/info")
async def info(task_id: int, state: State) -> TaskInfo:
    """Everything known about a task: how it was set up, where its simulations went, and, for
    the LLM lab, its prompt, model and recent calls."""
    row = await _one(state, task_id)
    params: dict[str, Any] = dict(row.sampler_params or {})
    names = await _dataset_names(state, [row])
    settings = [
        TaskSetting(label=_label(k), value=_value(v))
        for k, v in params.items()
        if k not in HIDDEN and v not in (None, "", [])
    ]
    if row.sampler in LLM_SAMPLERS and "promptName" not in params:
        settings.append(TaskSetting(label="Prompt", value="Built-in"))
    space = params.get("space") or {}
    if space.get("fields"):
        settings.append(TaskSetting(label="Fields searched", value=f"{len(space['fields']):,}"))
    settings += [
        TaskSetting(label="Cores", value=str(scheduler.cores_of(row))),
        TaskSetting(label="Target", value=f"{row.max_trials:,} simulations"),
        TaskSetting(label="Batch size", value=str(row.batch_size)),
        TaskSetting(label="Task key", value=row.task),
    ]

    # Where the simulations went: by dataset for the LLM lab, by field for the searches.
    key = func.coalesce(
        func.json_extract(Trial.params, "$.dataset"), func.json_extract(Trial.params, "$.field")
    )
    value = func.json_extract(Trial.values, "$[0]")
    async with state.db.session() as session:
        grouped = (
            await session.execute(
                select(key, Trial.state, func.count(), func.max(value))
                .where(
                    Trial.study_id == task_id,
                    Trial.state.in_(
                        [
                            TrialState.COMPLETE,
                            TrialState.FAIL,
                            TrialState.RUNNING,
                            TrialState.QUEUED,
                        ]
                    ),
                )
                .group_by(key, Trial.state)
            )
        ).all()
    ids = set(params.get("datasetIds") or [])
    unmapped = [str(k) for k, *_ in grouped if k is not None and str(k) not in ids]
    field_to_dataset: dict[str, str] = {}
    if unmapped:
        found = await state.catalog.query(
            f"""
            SELECT DISTINCT field_id, dataset_id FROM data_field
            WHERE region = ? AND delay = ? AND field_id IN ({", ".join("?" for _ in unmapped)})
            """,  # noqa: S608
            [params.get("region"), params.get("delay"), *unmapped],
        )
        field_to_dataset = {str(f["field_id"]): str(f["dataset_id"]) for f in found}
    per: dict[str, dict[str, Any]] = {}
    for k, trial_state, n, best in grouped:
        dataset = (
            None
            if k is None
            else str(k)
            if str(k) in ids or str(k) == pool_lab.CHOSEN
            else field_to_dataset.get(str(k))
        )
        entry = per.setdefault(
            dataset or "", {"simulated": 0, "complete": 0, "failed": 0, "best": None}
        )
        entry["simulated"] += int(n)
        if trial_state == TrialState.COMPLETE:
            entry["complete"] += int(n)
            if best is not None and float(best) > FAILURE:
                entry["best"] = max(entry["best"] or float(best), float(best))
        elif trial_state == TrialState.FAIL:
            entry["failed"] += int(n)
    order: list[str] = [str(d) for d in params.get("datasetIds") or []]
    order += [d for d in per if d not in order]
    datasets = [
        TaskDataset(
            id=d or "—",
            name=("Chosen fields" if d == pool_lab.CHOSEN else names.get(d, d) or "Not recorded"),
            **per.get(d, {"simulated": 0, "complete": 0, "failed": 0, "best": None}),
        )
        for d in order
        if d in per or d in ids
    ]

    llm = params.get("llm") or {}
    calls = [c for c in (params.get("calls") or []) if isinstance(c, dict)][-20:][::-1]
    return TaskInfo(
        id=row.id,
        name=row.name,
        lab_name=TASK_SAMPLERS.get(row.sampler, row.sampler),
        status=StudyStatus(row.status),
        message=row.message,
        settings=settings,
        datasets=datasets,
        fields=list(params.get("fieldIds") or []),
        rank_by=params.get("rankBy"),
        llm_calls=int(llm.get("calls") or 0),
        recent_calls=[
            TaskCall(
                at=c.get("at"),
                dataset=c.get("dataset"),
                prompt=c.get("prompt"),
                model=c.get("model"),
                fields=c.get("fields"),
                valid=c.get("valid"),
                rejected=c.get("rejected"),
                tokens=c.get("tokens"),
                error=c.get("error"),
            )
            for c in calls
        ],
        created_at=row.created_at.isoformat() if row.created_at else None,
        queued_at=params.get("queuedAt"),
        started_at=row.started_at.isoformat() if row.started_at else None,
        finished_at=row.finished_at.isoformat() if row.finished_at else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


# --- Region Agnostic Lab: one Alpha, a run per region ----------------------------------

#: Checks that say where an Alpha belongs rather than whether it is fit to submit.
NOT_FITNESS = ("THEME", "CLUSTER", "PYRAMID")

#: Tasks being calibrated now, so the view can say so and wait.
_calibrating: set[int] = set()


class GroupAlpha(Out):
    trial_id: int
    region: str | None
    universe: str | None
    #: Where its simulation is: waiting for cores, out on BRAIN, back, or failed.
    state: Literal["waiting", "running", "complete", "failed"]
    alpha_id: str | None
    sharpe: float | None
    fitness: float | None
    turnover: float | None
    returns: float | None
    #: Submission checks, leaving out theme, cluster and pyramid ones.
    passed: int
    failed: int
    pending: int
    failing: list[str]


class AlphaGroup(Out):
    #: The first of its trials' numbers, which the rest share.
    group: int
    expression: str | None
    neutralization: str | None
    decay: int | None
    truncation: float | None
    alphas: list[GroupAlpha]
    passed: int
    failed: int
    pending: int
    best_sharpe: float | None


class AlphaGroups(Out):
    groups: list[AlphaGroup]
    calibrating: bool
    #: Alphas back from BRAIN whose checks were ever read, of those back.
    calibrated: int
    complete: int


def _check_counts(raw: Any) -> tuple[int, int, int, list[str]]:
    """Passed, failed and pending checks, and the names that failed, fitness checks only."""
    try:
        checks = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except ValueError:
        checks = []
    passed = failed = pending = 0
    failing: list[str] = []
    for c in checks if isinstance(checks, list) else []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "")
        if any(word in name.upper() for word in NOT_FITNESS):
            continue
        result = str(c.get("result") or "").upper()
        if result == "PASS":
            passed += 1
        elif result in ("FAIL", "ERROR"):
            failed += 1
            failing.append(name)
        elif result == "PENDING":
            pending += 1
    return passed, failed, pending, failing


async def _group_rows(state: Any, task_id: int) -> list[Trial]:
    async with state.db.session() as session:
        return list(
            (
                await session.scalars(
                    select(Trial)
                    .where(
                        Trial.study_id == task_id,
                        func.json_extract(Trial.params, "$.group").is_not(None),
                        or_(Trial.state != TrialState.PRUNED, Trial.message == pool_lab.PROPOSED),
                    )
                    .order_by(Trial.number)
                )
            ).all()
        )


@router.get("/{task_id}/groups")
async def groups(task_id: int, state: State) -> AlphaGroups:
    """Region Agnostic Lab's results: each expression once, with its run in every region.

    Runs come back at different times; each lands in its expression's group as it does.
    Groups are ranked by the share of their Alphas' submission checks that pass, then by how
    many pass, then best Sharpe — so after Calibrate, the most submittable come first.
    """
    row = await _one(state, task_id)
    if row.sampler != REGION_AGNOSTIC_SAMPLER:
        raise refuse(422, "not_region_agnostic", "Only a Region Agnostic Lab task has groups.")
    trials = await _group_rows(state, task_id)
    stored = await state.alphas.by_ids([t.alpha_id for t in trials if t.alpha_id])
    by_group: dict[int, list[Trial]] = {}
    for t in trials:
        # ``is None``, not ``or``: the first Alpha a task writes is group 0.
        group = (t.params or {}).get("group")
        by_group.setdefault(t.number if group is None else int(group), []).append(t)
    out: list[AlphaGroup] = []
    complete = calibrated = 0
    for group, members in by_group.items():
        alphas: list[GroupAlpha] = []
        for t in members:
            a = stored.get(t.alpha_id or "") or {}
            passed, failed, pending, failing = _check_counts(a.get("checks"))
            state_ = (
                "complete"
                if t.state == TrialState.COMPLETE
                else "failed"
                if t.state == TrialState.FAIL
                else "running"
                if t.state in (TrialState.QUEUED, TrialState.RUNNING)
                else "waiting"
            )
            if state_ == "complete":
                complete += 1
                calibrated += bool(passed + failed) and not pending
            settings = t.settings or {}
            alphas.append(
                GroupAlpha(
                    trial_id=t.id,
                    region=settings.get("region") or (t.params or {}).get("region"),
                    universe=settings.get("universe"),
                    state=state_,
                    alpha_id=t.alpha_id,
                    sharpe=a.get("sharpe"),
                    fitness=a.get("fitness"),
                    turnover=a.get("turnover"),
                    returns=a.get("returns"),
                    passed=passed,
                    failed=failed,
                    pending=pending,
                    failing=failing,
                )
            )
        first = (members[0].settings or {}) if members else {}
        sharpes = [a.sharpe for a in alphas if a.sharpe is not None]
        out.append(
            AlphaGroup(
                group=group,
                expression=members[0].expression if members else None,
                neutralization=first.get("neutralization"),
                decay=first.get("decay"),
                truncation=first.get("truncation"),
                alphas=alphas,
                passed=sum(a.passed for a in alphas),
                failed=sum(a.failed for a in alphas),
                pending=sum(a.pending for a in alphas),
                best_sharpe=max(sharpes) if sharpes else None,
            )
        )

    # By the share of its checks passed, not the count: an Alpha that ran in four regions
    # would otherwise outrank a cleaner one that ran in three.
    def score(g: AlphaGroup) -> tuple[float, int, float, int]:
        judged = g.passed + g.failed
        return (
            -(g.passed / judged) if judged else 1.0,
            -g.passed,
            -(g.best_sharpe or -99.0),
            g.group,
        )

    out.sort(key=score)
    return AlphaGroups(
        groups=out,
        calibrating=task_id in _calibrating,
        calibrated=int(calibrated),
        complete=complete,
    )


@router.post("/{task_id}/calibrate", status_code=202)
async def calibrate(task_id: int, state: State) -> WorkflowStarted:
    """Run BRAIN's submission checks on every Alpha the task has back, without submitting.

    It spends no simulation quota; each check is a request BRAIN answers in its own time,
    so this runs in the background and the groups rank themselves as the answers land.
    """
    row = await _one(state, task_id)
    if row.sampler != REGION_AGNOSTIC_SAMPLER:
        raise refuse(422, "not_region_agnostic", "Only a Region Agnostic Lab task calibrates.")
    ids = list(
        dict.fromkeys(
            t.alpha_id
            for t in await _group_rows(state, task_id)
            if t.alpha_id and t.state == TrialState.COMPLETE
        )
    )
    if not ids:
        raise refuse(409, "nothing_back", "No Alpha of this task is back from BRAIN yet.")

    async def work(task: Task) -> str:
        _calibrating.add(task_id)
        failed = 0
        try:
            for done, alpha_id in enumerate(ids, start=1):
                try:
                    body = await state.endpoints.check_alpha(alpha_id)
                    checks = ((body.get("is") or {}).get("checks")) or []
                    if checks:
                        await state.alphas.save_checks(alpha_id, checks)
                except Exception:  # noqa: BLE001
                    failed += 1
                await state.tasks.update(
                    task, progress=done / len(ids), detail=f"Checked {done} of {len(ids)} Alphas"
                )
        finally:
            _calibrating.discard(task_id)
        return f"Checked {len(ids) - failed} of {len(ids)} Alphas" + (
            f"; {failed} could not be read" if failed else ""
        )

    try:
        started = await state.backfill.start_job(
            "ra-calibrate", f"Calibrating task {task_id}", work
        )
    except RuntimeError as exc:
        raise refuse(409, "already_running", str(exc)) from exc
    return WorkflowStarted(task_id=started)


@router.get("/{task_id}/top")
async def top(
    task_id: int,
    state: State,
    # A sweep's whole result set is worth scrolling, so the ceiling is a day's
    # simulations rather than a page; the caller asks for what it means to show.
    limit: Annotated[int, Query(ge=1, le=5000)] = 20,
) -> list[RankedAlpha]:
    """The task's best Alphas on what it searches for. Seeds are not among them."""
    await _one(state, task_id)  # 404 for an unknown task
    async with state.db.session() as session:
        best = list(
            (
                await session.scalars(
                    select(Trial)
                    .where(
                        Trial.study_id == task_id,
                        Trial.state == TrialState.COMPLETE,
                        or_(Trial.generation.is_(None), Trial.generation != 0),
                    )
                    .order_by(func.json_extract(Trial.values, "$[0]").desc())
                    .limit(limit)
                )
            ).all()
        )
    stored = await state.alphas.by_ids([t.alpha_id for t in best if t.alpha_id])
    current = {a: checks_of(r.get("checks")) for a, r in stored.items() if r.get("checks")}
    prod = await _prod_correlations(state, list(stored), current)
    # No download is started from here. Reading a page is not asking for one, and these are
    # three BRAIN requests per Alpha: enough of them earns a 429, which pauses *every* caller
    # of the shared client -- the simulation engine's dispatch and polling included. Calculate
    # After-Cost Sharpe is the way to ask, and it says how many it will fetch before it does.
    return [
        RankedAlpha.model_validate(
            r
            | {
                "longCount": (stored.get(r["alphaId"]) or {}).get("long_count"),
                "shortCount": (stored.get(r["alphaId"]) or {}).get("short_count"),
                "afterCostSharpe": (stored.get(r["alphaId"]) or {}).get("after_cost_t10"),
                "testSharpe": (stored.get(r["alphaId"]) or {}).get("test_sharpe"),
                "testFitness": (stored.get(r["alphaId"]) or {}).get("test_fitness"),
                "prodCorrelation": prod.get(r["alphaId"]),
                "submitted": is_submitted(stored.get(r["alphaId"])),
            }
        )
        for r in ranked(best, current)
    ]


#: Verdicts that let an Alpha into the Power Pool on correlation.
SATISFIED = frozenset({"clear", "beats"})


async def _pool_members(state: State) -> dict[str, dict[str, Any]]:
    """Your submitted Power Pool Alphas, by id."""
    submitted = await state.alphas.by_ids(
        [str(r["alpha_id"]) for r in await state.alphas.submitted_members()]
    )
    return {a: r for a, r in submitted.items() if power_pool.is_power_pool(r)}


async def _power_pool(state: State, alpha_ids: list[str]) -> PowerPoolCorrelation:
    """Correlate these Alphas against the submitted Power Pool, region by region."""
    pool_meta = await _pool_members(state)
    stored = await state.alphas.by_ids(list(dict.fromkeys(alpha_ids)))
    # BRAIN attaches POWER_POOL_CORRELATION only to Alphas it would judge this way, so the
    # rest of the sweep is nothing to do with the Power Pool and is counted, not measured.
    candidates = {a: r for a, r in stored.items() if power_pool.is_power_pool(r)}
    grid = await state.alphas.pnl_grid([*candidates, *pool_meta])
    has_pnl = set(grid[0])

    by_region: dict[str, list[str]] = {}
    for alpha_id, r in pool_meta.items():
        region = power_pool.scope_of(r)
        if region is not None:
            by_region.setdefault(region, []).append(alpha_id)

    meta = candidates | pool_meta
    without_pnl: list[str] = []
    wanted: dict[str, list[str]] = {}
    # A region with nothing submitted in it needs no series and no arithmetic: there is
    # nothing for its Alphas to be correlated against, so each one is the first of its region
    # and clear on this rule. Leaving them out read as a refusal, which is the opposite.
    unopposed: dict[str, list[str]] = {}
    for alpha_id in dict.fromkeys(alpha_ids):
        row = candidates.get(alpha_id)
        region = power_pool.scope_of(row) if row else None
        if region is None:
            continue
        if region not in by_region:
            unopposed.setdefault(region, []).append(alpha_id)
            continue
        # The region is listed even when none of its Alphas can be measured yet, so its
        # scope still names the pool members a Portfolio sync has not downloaded.
        measurable = wanted.setdefault(region, [])
        if alpha_id in has_pnl:
            measurable.append(alpha_id)
        else:
            without_pnl.append(alpha_id)

    # Nothing is downloaded here: an Alpha with no series is reported in ``without_pnl`` and
    # left out, and the Power Pool Workflow fetches it when the reader wants it.

    scopes: list[PowerPoolScope] = []
    measured: list[PowerPoolRow] = [
        PowerPoolRow.model_validate(r | {"region": region})
        for region, ids in unopposed.items()
        for r in power_pool.correlate(grid, meta, ids, [])
    ]
    scopes.extend(
        PowerPoolScope(region=region, pool=[], pool_without_pnl=[]) for region in unopposed
    )
    for region in sorted(wanted, key=lambda r: (-len(wanted[r]), r)):
        members = by_region[region]
        # Every member, synced or not: a member without PnL is a collision nobody measured,
        # and an unsynced pool must read as unmeasured, not as an empty one.
        found = await asyncio.to_thread(power_pool.correlate, grid, meta, wanted[region], members)
        measured.extend(PowerPoolRow.model_validate(r | {"region": region}) for r in found)
        scopes.append(
            PowerPoolScope(
                region=region,
                pool=members,
                pool_without_pnl=[m for m in members if m not in has_pnl],
            )
        )

    return PowerPoolCorrelation(
        ceiling=power_pool.CEILING,
        sharpe_edge=ESCAPE,
        pool_size=len(pool_meta),
        power_pool_alphas=len(candidates),
        other_alphas=len(stored) - len(candidates),
        without_pnl=without_pnl,
        rows=measured,
        scopes=scopes,
    )


class TaskAlpha(RankedAlpha):
    """A submittable Alpha, with the task that found it."""

    task_id: int
    task_name: str


#: Ids per ``IN`` list, under SQLite's cap on bound variables.
TRIAL_CHUNK = 900

#: (Trial id, its Alpha) -> refused when it was simulated. A finished trial's own checks never
#: change, and judging every one again on each refresh decoded tens of thousands of check
#: lists. The Alpha is in the key because SQLite can give a deleted trial's id to a new one.
_REFUSED: dict[tuple[int, str | None], bool] = {}


async def _not_refused(session: Any, ids: list[tuple[int, str | None]]) -> list[int]:
    """The trials of ``ids`` BRAIN did not refuse when they ran, judged once per trial."""
    unknown = [i for i, alpha_id in ids if (i, alpha_id) not in _REFUSED]
    raw: list[tuple[int, str | None, str | None]] = []
    for chunk in itertools.batched(unknown, TRIAL_CHUNK, strict=False):
        raw += (
            await session.execute(
                select(Trial.id, Trial.alpha_id, func.json_extract(Trial.result, "$.checks")).where(
                    Trial.id.in_(chunk)
                )
            )
        ).tuples()

    def judge() -> None:
        for trial_id, alpha_id, checks in raw:
            refused = verdict(json.loads(checks) if checks else []) == "refused"
            _REFUSED[trial_id, alpha_id] = refused

    # Off the event loop: the first refresh after a start judges every finished trial.
    await asyncio.to_thread(judge)
    return [i for i, alpha_id in ids if not _REFUSED.get((i, alpha_id), False)]


@router.get("/submittable")
async def submittable_alphas(state: State) -> list[TaskAlpha]:
    """Every Alpha from every task that nothing BRAIN reports refuses: each check PASS, WARNING
    or PENDING, apart from the ones that never gate (``vault.yields.IGNORED_CHECKS``).

    Judged on the vault's checks where it has them, as a task's own list is. Each Alpha once,
    from the task that found it first.
    """
    tasks = {row.id: row for row in await _rows(state)}
    async with state.db.session() as session:
        ids = list(
            (
                await session.execute(
                    select(Trial.id, Trial.alpha_id)
                    .where(
                        Trial.study_id.in_(list(tasks)),
                        Trial.state == TrialState.COMPLETE,
                        Trial.alpha_id.is_not(None),
                        or_(Trial.generation.is_(None), Trial.generation != 0),
                    )
                    .order_by(Trial.study_id, Trial.number)
                )
            ).tuples()
        )
        # A check that FAILed at simulation time stays failed: BRAIN's later checks only
        # settle what was PENDING. Only the rest are read whole, about one in ten measured.
        kept = await _not_refused(session, ids)
        trials: list[Trial] = []
        for chunk in itertools.batched(kept, TRIAL_CHUNK, strict=False):
            trials += (await session.scalars(select(Trial).where(Trial.id.in_(chunk)))).all()
    trials.sort(key=lambda t: (t.study_id, t.number))
    stored = await state.alphas.by_ids(list({str(t.alpha_id) for t in trials}))
    current = {a: checks_of(r.get("checks")) for a, r in stored.items() if r.get("checks")}
    prod = await _prod_correlations(state, list(stored), current)
    by_trial = {t.id: t.study_id for t in trials}
    out: list[TaskAlpha] = []
    seen: set[str] = set()
    for r in ranked(trials, current):
        alpha_id = str(r["alphaId"])
        if not r["submittable"] or alpha_id in seen:
            continue
        seen.add(alpha_id)
        task = tasks[by_trial[r["trialId"]]]
        vault = stored.get(alpha_id) or {}
        out.append(
            TaskAlpha.model_validate(
                r
                | {
                    "longCount": vault.get("long_count"),
                    "shortCount": vault.get("short_count"),
                    "afterCostSharpe": vault.get("after_cost_t10"),
                    "testSharpe": vault.get("test_sharpe"),
                    "testFitness": vault.get("test_fitness"),
                    "prodCorrelation": prod.get(alpha_id),
                    "submitted": is_submitted(vault),
                    "taskId": task.id,
                    "taskName": task.label
                    or task.template_name
                    or TASK_SAMPLERS.get(task.sampler, task.sampler),
                }
            )
        )
    return out
