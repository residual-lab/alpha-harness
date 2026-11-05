"""Tasks: what the labs added, run from one place.

A lab only adds a task. Here a task runs: it waits until its cores fit in the free slots,
runs until its simulations are spent, across days if it has to, and can be paused,
stopped, changed or removed. Search Lab, Template Lab and Evolution Lab add scheduler.
"""

import asyncio
import contextlib
import itertools
import json
import math
from collections import Counter
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update

from ..brain.errors import BrainError, BrainRateLimited
from ..brain.platform import (
    POWER_POOL_CORRELATION_CEILING,
    POWER_POOL_ESCAPE,
    REGION_AGNOSTIC_REGION,
    SIMULATION_SLOTS,
    SUPER_ALPHA_SLOTS,
)
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
from ..labs import scheduler, search
from ..labs.objectives import FAILURE, OBJECTIVES, StudyNotFoundError
from ..labs.params import (
    CORRELATION_BREAKER,
    FIELDS_SAMPLER,
    SETTINGS_SAMPLER,
    SUPER_LAB,
    TASK_SAMPLERS,
    TEMPLATE_SAMPLER,
    task_params,
)
from ..labs.study import ranked
from ..schemas import Out
from ..tasks import Task
from ..tools import power_pool
from ..vault.yields import checks_of, clean, is_submitted, verdict
from .deps import State, refuse

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/lab-tasks", tags=["lab-tasks"])

#: Labs that vary settings around one fixed expression, so ``template_source`` is that
#: expression rather than a skeleton to search.
ONE_EXPRESSION = frozenset({SETTINGS_SAMPLER, CORRELATION_BREAKER, FIELDS_SAMPLER})

#: Where a task can be run from: never started, paused, or failed. A failed task may have
#: sent simulations that finished after it stopped; running it again scores them.
RUNNABLE = (StudyStatus.IDLE, StudyStatus.PAUSED, StudyStatus.FAILED)
FINISHED = (StudyStatus.COMPLETE, StudyStatus.FAILED)


class TaskChange(BaseModel):
    #: Up to the engine's slots, or BRAIN's SuperAlpha limit for a Super Lab task (checked in
    #: the route, which knows the task).
    cores: int | None = Field(default=None, ge=1, le=SIMULATION_SLOTS)
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
    #: LLM Power Pool Lab only: answered calls by the model that answered them.
    llm_models: dict[str, int] = Field(default_factory=dict[str, int])


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
    #: The Sharpe once 5 bps is charged against each day's own turnover. Null until the daily
    #: PnL *and* turnover are stored.
    after_cost_sharpe: float | None = None
    #: The held-out test years, from the vault: where an Alpha that only fits its train years
    #: shows it. Shown, never searched on, so they stay out of sample. Null without a test period.
    test_sharpe: float | None = None
    test_fitness: float | None = None
    #: The highest correlation with any consultant Alpha in production, which BRAIN wants
    #: below 0.7. Null until BRAIN has been asked: it limits these checks per hour.
    prod_correlation: float | None = None
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
    #: Submittable only because a check has not answered yet: not a promise to submit on.
    pending: bool = False
    #: Simulated in Quick mode: the figures stand, but BRAIN submits it only once it is
    #: simulated again in Full. The task does that for every one that passes, and the Full
    #: Alpha replaces it here once it is back.
    quick: bool = False
    #: A check that decides submission came back ERROR: BRAIN could not judge it, which is
    #: told apart from an Alpha a check refused.
    errored: bool = False
    #: The limit each of BRAIN's figure checks names for this Alpha, by check name.
    limits: dict[str, float] = Field(default_factory=dict)


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


#: Progress of complete tasks, which nothing changes once complete: by task, with the status,
#: target and update time it was counted at, so a task extended or edited is counted again.
_COMPLETE_PROGRESS: dict[int, tuple[tuple[Any, ...], dict[str, Any]]] = {}


async def _progress(state: Any, rows: list[Study]) -> dict[int, dict[str, Any]]:
    """Trials by state, trials answered for free, and the best value per task, counted in SQL.

    A long task holds tens of thousands of trials; reading them all to count them would
    slow the list down with every day the task runs. A complete task is counted once.
    """
    out: dict[int, dict[str, Any]] = {}
    ids: list[int] = []
    for row in rows:
        kept = _COMPLETE_PROGRESS.get(row.id)
        if kept is not None and kept[0] == _settled_key(row):
            out[row.id] = kept[1]
        else:
            out[row.id] = {"states": {}, "free": 0, "fullRuns": 0, "best": None, "failures": {}}
            ids.append(row.id)
    if not ids:
        return out
    value = func.json_extract(Trial.values, "$[0]")
    answered = Trial.state.in_([TrialState.COMPLETE, TrialState.FAIL])
    async with state.db.session() as session:
        states = await session.execute(
            select(Trial.study_id, Trial.state, func.count())
            .where(Trial.study_id.in_(ids))
            .group_by(Trial.study_id, Trial.state)
        )
        for study_id, trial_state, n in states.all():
            out[study_id]["states"][str(trial_state)] = int(n)
        # Three counts in one pass over the trials rather than one pass each.
        counted = await session.execute(
            select(
                Trial.study_id,
                func.count().filter(answered, Trial.message == scheduler.FREE),
                func.count().filter(
                    answered,
                    or_(Trial.message.is_(None), Trial.message != scheduler.FREE),
                    scheduler.full_run(),
                ),
                # An Alpha that returned no value is scored at the failure value, so it is not
                # a best; nor is a seed, which was scored before the task began.
                func.max(value).filter(
                    Trial.state == TrialState.COMPLETE,
                    value > FAILURE,
                    or_(Trial.generation.is_(None), Trial.generation != 0),
                ),
            )
            .where(Trial.study_id.in_(ids))
            .group_by(Trial.study_id)
        )
        for study_id, free, full_runs, best in counted.all():
            out[study_id]["free"] = int(free or 0)
            out[study_id]["fullRuns"] = int(full_runs or 0)
            out[study_id]["best"] = None if best is None else float(best)
        failures = await session.execute(
            select(Trial.study_id, Trial.message, func.count())
            .where(Trial.study_id.in_(ids), Trial.state == TrialState.FAIL)
            .group_by(Trial.study_id, Trial.message)
        )
        for study_id, message, n in failures.all():
            found = out[study_id]["failures"]
            found[_reason(message)] = found.get(_reason(message), 0) + int(n)
    for row in rows:
        if row.id in ids and row.status == StudyStatus.COMPLETE:
            _COMPLETE_PROGRESS[row.id] = (_settled_key(row), out[row.id])
    return out


def _settled_key(row: Study) -> tuple[Any, ...]:
    return (row.status, row.max_trials, row.updated_at)


def _fields(space: Any) -> int:
    """Fields a task searches: its pool, or each variable's for a typed Template Lab template."""
    if not isinstance(space, dict):
        return 0
    pooled = len(space.get("fields") or {})
    variables = space.get("variables")
    if pooled or not isinstance(variables, list):
        return pooled
    return sum(len(v.get("fields") or {}) for v in variables if isinstance(v, dict))


def _task(row: Study, progress: dict[str, Any]) -> LabTask:
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
            "fields": _fields(params.get("space")),
            "target": row.max_trials,
            "llmModels": (params.get("llm") or {}).get("byModel") or {},
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


async def _payload(state: Any, task_id: int) -> LabTask:
    row = await _one(state, task_id)
    return _task(row, (await _progress(state, [row]))[row.id])


@router.get("")
async def list_tasks(state: State) -> LabTasks:
    """Every task, newest first, and the slots they share."""
    rows = await _rows(state)
    progress = await _progress(state, rows)
    return LabTasks(slots=state.engine.slots, tasks=[_task(r, progress[r.id]) for r in rows])


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
                    if row.status == StudyStatus.FAILED:
                        row.message = None
                        row.finished_at = None
                    row.status = StudyStatus.QUEUED
                    params = task_params(row)
                    params.queued_at = queued_at
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


@router.post("/power-pool-workflow/stop", status_code=204)
async def stop_power_pool_workflow(state: State) -> None:
    """Stop the workflow. PnL and turnover already downloaded stay."""
    state.backfill.cancel_job("power-pool-workflow")


@router.post("/prod-correlation/stop", status_code=204)
async def stop_prod_correlation(state: State) -> None:
    """Stop asking BRAIN. Every answer already back is kept."""
    state.backfill.cancel_job("prod-correlation")


@router.post("/power-pool-correlation")
async def power_pool_correlation_for(body: AlphaIds, state: State) -> PowerPoolCorrelation:
    """The same measurement over an explicit set of Alphas, for the panes that span tasks."""
    return await _power_pool(state, body.alpha_ids)


class SubmissionPick(Out):
    """One Alpha of the simulated order, as it stood when its turn came."""

    alpha_id: str
    region: str | None
    sharpe: float | None
    after_cost_sharpe: float | None
    #: Its highest Power Pool Correlation with the pool and every earlier pick.
    correlation: float | None
    closest: str | None
    verdict: str
    #: Candidates its submission would block.
    blocks: int


class Blocked(Out):
    alpha_id: str
    #: The pick whose submission blocks it.
    by: str


class SubmissionOrder(Out):
    """Greedy submission, best After-Cost Sharpe first, worked out without submitting."""

    candidates: int
    picks: list[SubmissionPick]
    blocked: list[Blocked]
    #: Could not be measured against an earlier pick: no stored daily PnL.
    unmeasured: list[str]


@router.post("/power-pool-order")
async def power_pool_order(body: AlphaIds, state: State) -> SubmissionOrder:
    """How many of these Alphas could be submitted one after another: each pick joins its
    region's Power Pool, and the rest are judged again against it, as BRAIN would after a
    submission and a Portfolio sync. Nothing is submitted and nothing is downloaded."""
    pool_meta = await _pool_members(state)
    stored = await state.alphas.by_ids(list(dict.fromkeys(body.alpha_ids)))
    candidates = {
        a: r for a, r in stored.items() if power_pool.is_power_pool(r) and not is_submitted(r)
    }
    grid = await state.alphas.pnl_grid([*candidates, *pool_meta])
    pools: dict[str, list[str]] = {}
    for alpha_id, row in pool_meta.items():
        if (region := power_pool.scope_of(row)) is not None:
            pools.setdefault(region, []).append(alpha_id)
    meta = candidates | pool_meta
    picks, blocked, unmeasured = await asyncio.to_thread(
        power_pool.submission_order, grid, meta, list(candidates), pools
    )
    blocks = Counter(blocked.values())
    return SubmissionOrder(
        candidates=len(candidates),
        picks=[
            SubmissionPick(
                alpha_id=row["alphaId"],
                region=power_pool.scope_of(meta[row["alphaId"]]),
                sharpe=row.get("sharpe"),
                after_cost_sharpe=meta[row["alphaId"]].get("after_cost_5bps"),
                correlation=row.get("correlation"),
                closest=row.get("closest"),
                verdict=row.get("verdict", "clear"),
                blocks=blocks[row["alphaId"]],
            )
            for row in picks
        ],
        blocked=[Blocked(alpha_id=a, by=by) for a, by in blocked.items()],
        unmeasured=unmeasured,
    )


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
    """Queue nothing more for now. Simulations already sent finish; the rest wait for Run."""
    await _one(state, task_id)  # 404 for an unknown task
    if await scheduler.pause(state.optimizer, task_id) is None:
        raise refuse(409, "not_running", "Only a running or waiting task can pause.")
    return await _payload(state, task_id)


@router.post("/{task_id}/stop")
async def stop(task_id: int, state: State) -> LabTask:
    """Finish a task early. Simulations already sent still finish and are scored.

    Pressed on a task that is *already* stopping, it forces: what is out on BRAIN is
    cancelled where it can be, the trials close whatever their simulations are doing, and
    the cores come back. There is no separate button because there is no separate
    intention — the second press means the first one did not work.
    """
    row = await _one(state, task_id)
    if row.status not in (StudyStatus.RUNNING, StudyStatus.PAUSED, StudyStatus.QUEUED):
        raise refuse(409, "not_started", "Only a task that has been run can stop.")
    await scheduler.stop_task(state.optimizer, row, force=task_params(row).stopping)
    return await _payload(state, task_id)


@router.patch("/{task_id}")
async def change(task_id: int, body: TaskChange, state: State) -> LabTask:
    """Change a task's cores or simulations. A running task takes them from its next round."""
    row = await _one(state, task_id)
    if row.status in FINISHED:
        raise refuse(409, "finished", "A finished task can't change.")
    if body.cores and row.sampler == SUPER_LAB and body.cores > SUPER_ALPHA_SLOTS:
        raise refuse(
            422,
            "too_many_cores",
            f"BRAIN runs {SUPER_ALPHA_SLOTS} SuperAlpha simulations at once, so a Super Lab "
            f"task cannot hold {body.cores} cores.",
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
async def remove(task_id: int, state: State) -> TaskRemoved:
    """Remove a task that is not running. The Alphas it found stay in Alphas."""
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
        if row.status in (StudyStatus.RUNNING, StudyStatus.QUEUED) or out:
            raise refuse(409, "running", "Pause or stop the task first: simulations are still out.")
        await state.engine.drop_queued(row.task)
        await state.optimizer.delete(task_id)
        await state.engine.set_quota(row.task, 0, enabled=False)
    _COMPLETE_PROGRESS.pop(task_id, None)
    await state.optimizer.notify()
    return TaskRemoved(removed=task_id)


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
    # A region-agnostic parent is judged on its children, as BRAIN judges it.
    current |= await state.alphas.family_checks(stored)
    prod = await _prod_correlations(state, list(stored), current)
    # No download is started from here. Reading a page is not asking for one, and these are
    # three BRAIN requests per Alpha: enough of them earns a 429, which holds up every read of
    # an Alpha, captures included. Calculate After-Cost Sharpe is the way to ask, and it says
    # how many it will fetch before it does.
    return [
        RankedAlpha.model_validate(r | _from_vault(r["alphaId"], stored, prod))
        for r in ranked(best, current)
    ]


def _from_vault(
    alpha_id: str, stored: dict[str, dict[str, Any]], prod: dict[str, float]
) -> dict[str, Any]:
    """What a ranked row adds from the vault and from BRAIN's correlation check."""
    vault = stored.get(alpha_id) or {}
    return {
        "longCount": vault.get("long_count"),
        "shortCount": vault.get("short_count"),
        "afterCostSharpe": vault.get("after_cost_5bps"),
        "testSharpe": vault.get("test_sharpe"),
        "testFitness": vault.get("test_fitness"),
        "prodCorrelation": prod.get(alpha_id),
        "submitted": is_submitted(vault),
    }


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
        ceiling=POWER_POOL_CORRELATION_CEILING,
        sharpe_edge=POWER_POOL_ESCAPE,
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
    raw: list[tuple[int, str | None, str | None, str | None]] = []
    for chunk in itertools.batched(unknown, TRIAL_CHUNK, strict=False):
        raw += (
            await session.execute(
                select(
                    Trial.id,
                    Trial.alpha_id,
                    func.json_extract(Trial.result, "$.checks"),
                    func.json_extract(Trial.settings, "$.region"),
                ).where(Trial.id.in_(chunk))
            )
        ).tuples()

    def judge() -> None:
        for trial_id, alpha_id, checks, region in raw:
            # A region-agnostic trial ran with one child's checks standing in; the family is
            # judged on all its children once read, so it is never dropped here.
            refused = region != REGION_AGNOSTIC_REGION and (
                verdict(json.loads(checks) if checks else []) == "refused"
            )
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
    # A region-agnostic parent is judged on its children, as BRAIN judges it.
    current |= await state.alphas.family_checks(stored)
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
        out.append(
            TaskAlpha.model_validate(
                r
                | _from_vault(alpha_id, stored, prod)
                | {
                    "taskId": task.id,
                    "taskName": task.label
                    or task.template_name
                    or TASK_SAMPLERS.get(task.sampler, task.sampler),
                }
            )
        )
    return out
