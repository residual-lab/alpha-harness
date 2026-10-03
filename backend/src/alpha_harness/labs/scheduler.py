"""Tasks: what every research lab shares once its work is added.

A lab adds a task and the Tasks tab runs it. Running is the same for every lab: waiting for
cores, keeping them full as batches come back, and taking unsent work off the queue. A lab
only decides how one point of its search is drawn (its ``draw``).
"""

import asyncio
import json
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import String, func, not_, or_, select, type_coerce, update

from ..brain.schemas import SimulationRequest, SimulationSettings, SimulationType
from ..brain.settings_schema import validate_settings
from ..db.models import SimStatus, SimulationRecord, Study, StudyStatus, Trial, TrialState, utcnow
from ..engine.packer import MAX_BATCH
from . import ga, search, template, template_v1
from .objectives import StudyNotFoundError
from .params import (
    CORRELATION_BREAKER,
    GA_SAMPLER,
    POWER_POOL_SAMPLER,
    REGION_AGNOSTIC_SAMPLER,
    SEARCH_SAMPLER,
    SETTINGS_SAMPLER,
    SUPER_LAB,
    TASK_SAMPLERS,
    TEMPLATE_SAMPLER,
    SearchParams,
    TemplateParams,
    params_of,
    task_params,
)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

    import optuna

    from .study import Optimizer

log = structlog.get_logger(__name__)

#: Marks a trial answered from an Alpha already simulated: it spent no quota.
FREE = "Matched an Alpha already simulated; no quota spent."

#: Parks a written simulation outside every count until it is sent.
PENDING_SEND = "Waiting for cores."

#: Parks the Full re-run a Quick Alpha that passed is owed, until there is room to send it.
PENDING_FULL = "Waiting to run in Full mode."


def queued(outcome: dict[str, Any]) -> dict[str, Any]:
    """A trial's fields once the engine has taken its request, per ``enqueue``'s outcome."""
    return {
        "state": TrialState.QUEUED,
        "simulation_record_id": outcome.get("recordId"),
        "alpha_id": outcome.get("alphaId"),
        "message": FREE if outcome.get("status") == str(SimStatus.SKIPPED) else None,
    }


def request_of(trial: Trial) -> SimulationRequest:
    """The simulation a written trial stands for: a SuperAlpha keeps its selection in params."""
    settings = SimulationSettings.model_validate(trial.settings)
    params = trial.params or {}
    if params.get("selection"):
        return SimulationRequest(
            type=SimulationType.SUPER,
            settings=settings,
            selection=str(params["selection"]),
            combo=str(params.get("combo") or trial.expression or "1"),
        )
    return SimulationRequest(settings=settings, regular=trial.expression)


async def send_parked(optimizer: Optimizer, row: Study, batch: Sequence[Trial]) -> int:
    """Send trials written up front and parked until cores were free.

    ``batch`` must still be attached to the caller's session: the outcome is written by
    assigning to those rows, which is what saves a round trip per trial.
    """
    requests = [request_of(t) for t in batch]
    outcomes = (await optimizer.engine.enqueue(requests, task=row.task)).get("outcomes", [])
    for index, trial in enumerate(batch):
        for field, value in queued(outcomes[index] if index < len(outcomes) else {}).items():
            setattr(trial, field, value)
    return len(batch)


#: Held by anything that reads free cores and then hands some out, so two of them
#: cannot hand out the same free cores. Never held while taking a study's lock.
scheduling = asyncio.Lock()


def cores_of(row: Study) -> int:
    return task_params(row).cores


def to_start(waiting: list[tuple[int, int]], used: int, slots: int) -> list[int]:
    """Waiting ``(task, cores)`` pairs, oldest first, that fit in the cores left.

    A task too big for what is free does not hold back a smaller one queued after it.
    """
    started: list[int] = []
    for task_id, cores in waiting:
        if used + cores <= slots:
            started.append(task_id)
            used += cores
    return started


def to_ask(batch_size: int, in_flight: int, left: int) -> int:
    """Simulations to queue now: the room the task's cores have, in tens so batches stay full."""
    room = batch_size - in_flight
    return max(0, min(room - room % 10, left))


async def start_waiting(optimizer: Optimizer) -> int:
    """Start queued tasks while their cores fit in the engine's slots. Returns how many."""
    async with scheduling:
        async with optimizer.db.session() as session:
            rows = list(
                (
                    await session.scalars(
                        select(Study).where(
                            Study.sampler.in_(TASK_SAMPLERS),
                            Study.status.in_([StudyStatus.RUNNING, StudyStatus.QUEUED]),
                        )
                    )
                ).all()
            )
        used = sum(cores_of(r) for r in rows if r.status == StudyStatus.RUNNING)
        waiting = sorted(
            (r for r in rows if r.status == StudyStatus.QUEUED),
            key=lambda r: (task_params(r).queued_at or "", r.id),
        )
        by_id = {r.id: r for r in waiting}
        started = to_start([(r.id, cores_of(r)) for r in waiting], used, optimizer.engine.slots)
        for task_id in list(started):
            # Compare-and-set from QUEUED, so a pause or stop since the read above wins. That
            # press holds the task's lock, which cannot be taken here: `resize_task` takes it
            # before `scheduling`.
            async with optimizer.db.session() as session:
                moved = await session.scalar(
                    update(Study)
                    .where(Study.id == task_id, Study.status == StudyStatus.QUEUED)
                    .values(status=StudyStatus.RUNNING)
                    .returning(Study.id)
                )
                if moved is not None:
                    # Only the first start: resuming a paused task continues the same run.
                    await session.execute(
                        update(Study)
                        .where(Study.id == task_id, Study.started_at.is_(None))
                        .values(started_at=utcnow())
                    )
            if moved is None:
                started.remove(task_id)
                continue
            await optimizer.engine.set_quota(by_id[task_id].task, cores_of(by_id[task_id]))
            used += cores_of(by_id[task_id])
        if started:
            await optimizer.notify()
        optimizer.lendable_cores = (
            max(0, optimizer.engine.slots - used) if optimizer.engine.lend_idle_cores else 0
        )
    if started:
        log.info("tasks.started", tasks=started)
    return len(started)


def ask_points(
    study: optuna.Study,
    lab: Any,
    run: SearchParams,
    want: int,
    seen: dict[tuple[str, str], float | bool],
    cover: list[str],
    schema: dict[str, Any] | None = None,
) -> list[tuple[Any, dict[str, Any], SimulationRequest]]:
    """Ask for ``want`` new simulations. Runs off the event loop.

    A point this task already scored is answered with its score, one that failed is told as
    failed, and a repeat, or a point its lab cannot write, is pruned; each time another
    point is asked, so quota is only spent on Alphas the task has not seen.
    """
    from optuna.trial import TrialState as OptunaState

    space = run.space
    if cover and not study.get_trials(deepcopy=False, states=(OptunaState.WAITING,)):
        for field_id in cover:
            study.enqueue_trial(lab.first_pass(space, field_id))

    choices = lab.field_choices(space)
    picked: list[tuple[Any, dict[str, Any], SimulationRequest]] = []
    keys: set[tuple[str, str]] = set()
    for _ in range(want * 5):
        if len(picked) >= want:
            break
        trial = study.ask()
        params, request = lab.draw(trial, run, choices)
        if (
            request is not None
            and schema
            and (problems := validate_settings(schema, request.to_wire()["settings"]))
        ):
            # A task's space is fixed when it is added, but BRAIN withdraws settings. Sent,
            # the point is only rejected; pruned, it costs nothing and the search asks again.
            log.warning("tasks.point_unavailable", problem=problems[0])
            study.tell(trial, state=OptunaState.PRUNED)
            continue
        key = (
            None
            if request is None
            else search.identity_of(
                request.regular, request.settings.model_dump(by_alias=True, exclude_none=True)
            )
        )
        if request is None or key is None or key in keys or key in seen:
            known = None if key is None else seen.get(key)
            if known is False:
                study.tell(trial, state=OptunaState.FAIL)
            elif isinstance(known, float):
                study.tell(trial, known)
            else:
                study.tell(trial, state=OptunaState.PRUNED)
            continue
        keys.add(key)
        picked.append((trial, params, request))
    return picked


async def advance(optimizer: Optimizer, study_id: int) -> int:
    """Refill the task's cores as its batches come back. Returns how many were queued.

    A task keeps ten simulations in flight per core. Room freed by a returning batch is
    filled at once, in tens so every batch stays full, rather than once the slowest batch
    of a round is back.
    """
    async with optimizer.db.session() as session:
        row = await session.get(Study, study_id)
        if row is None or row.status != StudyStatus.RUNNING:
            return 0
        # Counted in SQL, never by loading the trials: a task may run to 100,000 of them, and
        # building every row each tick held the event loop for seconds.
        open_count, committed, last = (
            await session.execute(
                select(
                    func.count().filter(Trial.state.in_([TrialState.QUEUED, TrialState.RUNNING])),
                    # Every trial not pruned or answered for free has spent, or is spending,
                    # a simulation. A Full run is spent on top of the target, so a tool that
                    # wrote its simulations up front still sends every one.
                    func.count().filter(
                        Trial.state != TrialState.PRUNED, _not_free(), not_(full_run())
                    ),
                    func.max(Trial.number),
                ).where(Trial.study_id == study_id)
            )
        ).one()
        in_flight = await session.scalar(
            select(func.count())
            .select_from(SimulationRecord)
            .where(
                SimulationRecord.task == row.task,
                SimulationRecord.is_batch.is_(False),
                SimulationRecord.status.in_(
                    [SimStatus.QUEUED, SimStatus.PENDING, SimStatus.RUNNING]
                ),
            )
        )

    stopping = task_params(row).stopping
    waiting = bool(open_count)
    last_number = -1 if last is None else int(last)
    # Borrowed cores only run what is queued, so a task queues for them too; the engine then
    # hands them to whichever tasks have work beyond their own cores.
    room = row.batch_size + optimizer.lendable_cores * MAX_BATCH
    space = room - int(in_flight or 0)
    if not stopping and space > 0:
        # Ahead of the task's own budget: a passing Quick Alpha is only submittable once it is
        # run in Full, and a task that had spent its simulations would otherwise strand it.
        async with optimizer.db.session() as session:
            owed = (
                await session.scalars(
                    select(Trial)
                    .where(
                        Trial.study_id == row.id,
                        Trial.state == TrialState.PRUNED,
                        Trial.message == PENDING_FULL,
                    )
                    .order_by(Trial.number)
                    .limit(space)
                )
            ).all()
            if owed:
                return await send_parked(optimizer, row, owed)
    if stopping or committed >= row.max_trials:
        if not waiting:
            await optimizer.set_status(study_id, StudyStatus.COMPLETE)
        return 0
    want = to_ask(room, int(in_flight or 0), row.max_trials - committed)
    if row.sampler in (GA_SAMPLER, SEARCH_SAMPLER, TEMPLATE_SAMPLER) and want > 0:
        # Only after a crash between writing a round's trials and sending them (`_queue`).
        async with optimizer.db.session() as session:
            leftovers = (
                await session.scalars(
                    select(Trial)
                    .where(
                        Trial.study_id == row.id,
                        Trial.state == TrialState.PRUNED,
                        Trial.message == PENDING_SEND,
                    )
                    .order_by(Trial.number)
                    .limit(want)
                )
            ).all()
            if leftovers:
                return await send_parked(optimizer, row, leftovers)
    if row.sampler == GA_SAMPLER:
        return await _breed(optimizer, row, last_number, want, waiting)
    if row.sampler in (POWER_POOL_SAMPLER, REGION_AGNOSTIC_SAMPLER):
        from . import power_pool  # imported here: labs.power_pool builds on this module

        return await power_pool.refill(optimizer, row, want, waiting)
    # Both write every simulation up front, so both are drained the same way.
    if row.sampler in (SETTINGS_SAMPLER, CORRELATION_BREAKER, SUPER_LAB):
        from ..tools import settings_sampler  # same cycle: it builds on this module

        return await settings_sampler.refill(optimizer, row, want, waiting)
    if want <= 0:
        return 0

    if row.sampler == TEMPLATE_SAMPLER:
        typed = params_of(row, TemplateParams)
        lab = template_v1 if typed.tree else template
        run: SearchParams = typed
    else:
        lab = search
        run = params_of(row, SearchParams)

    space = run.space
    key, coverable = lab.coverage(space)
    async with optimizer.db.session() as session:
        counted = (
            await session.execute(
                select(
                    Trial.state,
                    raw_json(Trial.params),
                    Trial.expression,
                    raw_json(Trial.settings),
                    raw_json(Trial.values),
                ).where(Trial.study_id == study_id, Trial.state != TrialState.PRUNED)
            )
        ).all()
    tried, seen = await asyncio.to_thread(_search_memory, counted, key)
    limit = row.max_trials // 2
    cover: list[str] = []
    if len(tried) < limit:
        untried = [f for f in coverable if f not in tried]
        cover = untried[: min(want, limit - len(tried))]

    study = await optimizer.optuna_study(study_id, row)
    schema = await optimizer.metadata.cached_settings_schema()
    picked = await asyncio.to_thread(ask_points, study, lab, run, want, seen, cover, schema)
    if not picked:
        # Nothing new left to ask: the task is done once what is out has come back.
        if not waiting:
            await optimizer.set_status(study_id, StudyStatus.COMPLETE)
        return 0

    return await _queue(optimizer, row, last_number, picked)


def raw_json(column: Any) -> Any:
    """A JSON column read back as its stored text, to be decoded off the event loop."""
    return type_coerce(column, String)


def _not_free() -> Any:
    """SQL for ``message != FREE`` as Python means it: a trial with no message counts too."""
    return or_(Trial.message.is_(None), Trial.message != FREE)


def full_run() -> Any:
    """SQL for a trial that runs a passing Quick Alpha again in Full mode."""
    return func.json_extract(Trial.params, "$.fullOf").is_not(None)


def _search_memory(
    rows: Sequence[Any], covers: str
) -> tuple[set[Any], dict[tuple[str, str], float | bool]]:
    """The fields tried as ``covers`` and every point already scored, from unpruned trials.

    Runs in a worker thread: identity keys validate each trial's settings, which costs
    seconds at the largest task sizes.
    """
    tried: set[Any] = set()
    seen: dict[tuple[str, str], float | bool] = {}
    for state, params, expression, settings, values in rows:
        tried.add((json.loads(params or "null") or {}).get(covers))
        key = search.identity_of(expression, json.loads(settings or "null"))
        scores = json.loads(values or "null")
        if state == TrialState.COMPLETE and scores:
            seen[key] = float(scores[0])
        elif state == TrialState.FAIL:
            seen[key] = False
    return tried, seen


async def _queue(
    optimizer: Optimizer,
    row: Study,
    last_number: int,
    picked: list[tuple[Any, dict[str, Any], SimulationRequest]],
) -> int:
    """Record new points as trials, then send them to the engine.

    A point asked of the search carries its live trial, told when its simulation returns; a
    bred child carries none.
    """
    from optuna.distributions import distribution_to_json

    live = optimizer.open_trials.setdefault(row.id, {})
    number = last_number
    async with optimizer.db.session() as session:
        batch: list[Trial] = []
        for asked, params, request in picked:
            number += 1
            batch.append(
                Trial(
                    study_id=row.id,
                    number=number,
                    params=params,
                    distributions={
                        name: distribution_to_json(distribution)
                        for name, distribution in (asked.distributions.items() if asked else ())
                    },
                    expression=request.regular,
                    settings=request.settings.model_dump(by_alias=True, exclude_none=True),
                    generation=params.get("generation"),
                    state=TrialState.PRUNED,
                    message=PENDING_SEND,
                )
            )
            if asked is not None:
                live[number] = asked
        session.add_all(batch)
        # Written before anything is queued: queued work outlives a crash and spends quota,
        # so it must never exist without the trial that scores it. Resending a parked trial
        # is free, because the engine shares a row with an identical queued request.
        await session.commit()
        await send_parked(optimizer, row, batch)

    log.info("tasks.asked", study_id=row.id, trials=len(picked), task=row.task)
    return len(picked)


async def _breed(
    optimizer: Optimizer, row: Study, last_number: int, want: int, waiting: bool
) -> int:
    """Evolution Lab's refill: children of the best Alphas so far, until the search stalls."""
    async with optimizer.db.session() as session:
        # Only what the stall check reads: spent children's scores, in the order asked.
        spent_rows = (
            await session.execute(
                select(Trial.state, raw_json(Trial.values))
                .where(
                    Trial.study_id == row.id,
                    Trial.generation > 0,
                    _not_free(),
                    Trial.state.in_([TrialState.COMPLETE, TrialState.FAIL]),
                )
                .order_by(Trial.number)
            )
        ).all()
    if await asyncio.to_thread(lambda: ga.stalled(_spent(spent_rows))):
        if not waiting:
            await finish(optimizer, row.id, StudyStatus.COMPLETE, ga.STALLED)
        return 0
    if want <= 0:
        return 0
    picked = await ga.breed(optimizer, row, want)
    if not picked:
        if not waiting:
            async with optimizer.db.session() as session:
                bred = bool(
                    await session.scalar(
                        select(func.count()).where(Trial.study_id == row.id, Trial.generation > 0)
                    )
                )
            # Paused, not failed, when nothing was ever bred: the seeds may breed once the
            # market's operators or data change, and a fresh clone is always there.
            await finish(
                optimizer,
                row.id,
                StudyStatus.COMPLETE if bred else StudyStatus.PAUSED,
                "No new child could be bred: every child of these parents was already "
                "simulated or is not a valid Alpha here.",
            )
        return 0
    return await _queue(
        optimizer, row, last_number, [(None, params, req) for params, req in picked]
    )


def _spent(rows: Sequence[Any]) -> list[float | None]:
    out: list[float | None] = []
    for state, values in rows:
        scores = json.loads(values or "null")
        out.append(float(scores[0]) if state == TrialState.COMPLETE and scores else None)
    return out


async def resize_task(
    optimizer: Optimizer, row: Study, cores: int, simulations: int | None
) -> int | None:
    """Give a task ``cores``, and ``simulations`` when given, from its next round.

    Returns the free cores instead, changing nothing, when a running task cannot grow that far.
    """
    before = cores_of(row)
    async with optimizer.lock(row.id), scheduling:
        if row.status == StudyStatus.RUNNING and cores > before:
            async with optimizer.db.session() as session:
                running = (
                    await session.scalars(
                        select(Study).where(
                            Study.sampler.in_(TASK_SAMPLERS),
                            Study.status == StudyStatus.RUNNING,
                        )
                    )
                ).all()
            free = max(0, optimizer.engine.slots - sum(cores_of(r) for r in running))
            if cores - before > free:
                return free
        async with optimizer.db.session() as session:
            stored = await session.get(Study, row.id)
            if stored is None:
                raise StudyNotFoundError(row.id)
            # The Settings Sampler keeps its spare batch (see api/tools.py).
            spare = 1 if stored.sampler == SETTINGS_SAMPLER else 0
            stored.batch_size = (cores + spare) * 10
            if simulations is not None:
                stored.max_trials = simulations
            params = task_params(stored)
            params.cores = cores
            stored.sampler_params = params.dump()
            await session.commit()
        await optimizer.engine.set_quota(row.task, cores)
    await optimizer.notify()
    return None


async def finish(optimizer: Optimizer, study_id: int, status: StudyStatus, message: str) -> None:
    """End a task, and stop it spending: its unsent simulations leave the queue.

    Dropping the queue matters as much as the status, or the engine goes on sending work
    nobody will score. Simulations already sent finish; running the task again scores them.
    """
    async with optimizer.db.session() as session:
        stored = await session.get(Study, study_id)
        if stored is not None:
            stored.status = status
            stored.message = message
            stored.finished_at = utcnow()
            task = stored.task
        else:
            task = None
    if task is not None:
        await optimizer.engine.drop_queued(task)
        await prune_unsent(optimizer, study_id)
        await optimizer.engine.set_quota(task, 0)
    await optimizer.notify()


def park_message(sampler: str) -> str | None:
    """How a lab that writes its simulations before sending them marks one waiting to go, so a
    trial taken back can wait again instead of being lost; ``None`` for a lab that asks anew."""
    # Imported here: both modules build on this one.
    from ..tools.settings_sampler import PENDING_SEND
    from .power_pool import PROPOSED

    return {
        SETTINGS_SAMPLER: PENDING_SEND,
        CORRELATION_BREAKER: PENDING_SEND,
        POWER_POOL_SAMPLER: PROPOSED,
        REGION_AGNOSTIC_SAMPLER: PROPOSED,
    }.get(sampler)


#: Said on a trial whose simulation was cancelled, when its lab will ask for another.
CANCELLED_AGAIN = "Cancelled before it finished; the task asks for another in its place."


async def requeue_cancelled(optimizer: Optimizer, row: Study) -> int:
    """Put trials whose simulation was cancelled back where they came from. Returns how many.

    A cancel in the Simulation Matrix stops one batch, not the task. Counted as failed, those
    trials used up the task's target and could end it with work never done. Instead a
    simulation written up front is parked again to be sent once more, and a lab that writes its
    own asks for a new one in its place. A cancelled simulation that still left an Alpha is
    scored as usual.
    """
    from optuna.trial import TrialState as OptunaState

    park = park_message(row.sampler)
    live = optimizer.open_trials.get(row.id, {})
    study = optimizer.studies.get(row.id)
    async with optimizer.db.session() as session:
        cancelled = (
            await session.scalars(
                select(Trial)
                .join(SimulationRecord, SimulationRecord.id == Trial.simulation_record_id)
                .where(
                    Trial.study_id == row.id,
                    Trial.state.in_([TrialState.QUEUED, TrialState.RUNNING]),
                    SimulationRecord.status == SimStatus.CANCELLED,
                    SimulationRecord.alpha_id.is_(None),
                )
            )
        ).all()
        for trial in cancelled:
            trial.state = TrialState.PRUNED
            trial.simulation_record_id = None
            trial.alpha_id = None
            if park is not None:
                trial.message = park
                trial.finished_at = None
                continue
            trial.message = CANCELLED_AGAIN
            trial.finished_at = utcnow()
            optuna_trial = live.pop(trial.number, None)
            if study is not None and optuna_trial is not None:
                study.tell(optuna_trial, state=OptunaState.PRUNED, skip_if_finished=True)
        await session.commit()
    if cancelled:
        log.info("task.cancelled_requeued", study_id=row.id, trials=len(cancelled))
    return len(cancelled)


async def prune_unsent(optimizer: Optimizer, study_id: int, *, everything: bool = False) -> int:
    """Mark trials whose simulation was taken off the queue as never run.

    They spent nothing, so they are pruned rather than failed: failing them would teach the
    search that these points score badly.

    ``everything`` closes every open trial regardless of what its simulation is doing, for
    a forced stop. Pruned rather than failed for the same reason: an unscored point is not
    evidence that the point is bad.
    """
    from optuna.trial import TrialState as OptunaState

    live = optimizer.open_trials.get(study_id, {})
    study = optimizer.studies.get(study_id)
    pruned = 0
    async with optimizer.db.session() as session:
        stored = await session.get(Study, study_id)
        # Written up front: taken off the queue, it waits to be sent again on resume.
        park = park_message(stored.sampler) if stored is not None else None
        open_trials = list(
            (
                await session.scalars(
                    select(Trial).where(
                        Trial.study_id == study_id,
                        Trial.state.in_([TrialState.QUEUED, TrialState.RUNNING]),
                    )
                )
            ).all()
        )
        ids = [t.simulation_record_id for t in open_trials if t.simulation_record_id]
        cancelled = set(
            (
                await session.scalars(
                    select(SimulationRecord.id).where(
                        SimulationRecord.id.in_(ids),
                        SimulationRecord.status == SimStatus.CANCELLED,
                    )
                )
            ).all()
        )
        for trial in open_trials:
            unsent = not trial.simulation_record_id or trial.simulation_record_id in cancelled
            if not (unsent or everything):
                continue
            if unsent and park is not None:
                trial.state = TrialState.PRUNED
                trial.message = park
                trial.simulation_record_id = None
                trial.alpha_id = None
                trial.finished_at = None
                pruned += 1
                continue
            trial.state = TrialState.PRUNED
            trial.message = (
                "Taken off the queue before it was sent."
                if unsent
                else "The task was stopped before this finished."
            )
            trial.finished_at = utcnow()
            pruned += 1
            optuna_trial = live.pop(trial.number, None)
            if study is not None and optuna_trial is not None:
                study.tell(optuna_trial, state=OptunaState.PRUNED, skip_if_finished=True)
        await session.commit()
    return pruned
