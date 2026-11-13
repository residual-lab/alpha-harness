"""Tasks: what every research lab shares once its work is added.

A lab adds a task and the Tasks tab runs it. Running is the same for every lab: waiting for
cores, keeping them full as batches come back, and taking unsent work off the queue. A lab
only decides how one point of its search is drawn (its ``draw``).
"""

import asyncio
import functools
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import String, func, not_, or_, select, type_coerce, update

from ..brain.platform import MULTI_SIMULATION_SIZE
from ..brain.schemas import SimulationRequest, SimulationSettings, SimulationType
from ..brain.settings_schema import validate_settings
from ..db.models import SimStatus, SimulationRecord, Study, StudyStatus, Trial, TrialState, utcnow
from ..db.sqlite import loads
from . import ga, refusals, search, template
from .objectives import StudyNotFoundError
from .params import (
    CORRELATION_BREAKER,
    FIELDS_SAMPLER,
    GA_SAMPLER,
    PASTE_ALPHAS,
    POWER_POOL_SAMPLER,
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

#: Stored for a trial's field slot in place of its distribution, which :func:`field_slots`
#: rebuilds from the task's own space. Each copy was every field of a universe: 127 KB a trial.
FROM_SPACE = "space"


def field_slots(row: Study) -> dict[str, tuple[str, ...]]:
    """A Search or Template Lab task's field slots with their choices, fixed with the task.

    Empty for a lab without them, whose trials store their distributions whole.
    """
    if row.sampler == SEARCH_SAMPLER:
        return search.slots(params_of(row, SearchParams).space)
    if row.sampler == TEMPLATE_SAMPLER:
        return template.slots(params_of(row, TemplateParams).space)
    return {}


def queued(outcome: dict[str, Any]) -> dict[str, Any]:
    """A trial's fields once the engine has taken its request, per ``enqueue``'s outcome."""
    return {
        "state": TrialState.QUEUED,
        "simulation_record_id": outcome.get("recordId"),
        "alpha_id": outcome.get("alphaId"),
        "message": FREE if outcome.get("status") == str(SimStatus.SKIPPED) else None,
    }


def repark(trial: Trial, sampler: str) -> None:
    """Put a trial whose simulation never left the queue back where it waited to be sent.

    It spent nothing, so it is neither a result nor a failure, and a lab that wrote its work
    up front has no other copy of it to send.
    """
    if (trial.params or {}).get("fullOf") is not None:
        message = PENDING_FULL
    elif sampler == POWER_POOL_SAMPLER:
        from . import power_pool  # imported here: labs.power_pool builds on this module

        message = power_pool.PROPOSED
    else:
        message = PENDING_SEND
    trial.state = TrialState.PRUNED
    trial.message = message
    trial.simulation_record_id = None
    trial.alpha_id = None
    trial.finished_at = None


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


async def _still_sending(optimizer: Optimizer, rows: Sequence[Any]) -> set[int]:
    """The running tasks with simulations left to send: short of their target, holding parked
    work, or with work queued in the engine. The rest only wait on what is out.

    ``rows`` are :func:`start_waiting`'s."""
    ids = [r.id for r in rows]
    tasks = [r.task for r in rows]
    async with optimizer.db.session() as session:
        spent = dict(
            (
                await session.execute(
                    select(
                        Trial.study_id,
                        func.count().filter(
                            Trial.state != TrialState.PRUNED, _not_free(), not_(full_run())
                        ),
                    )
                    .where(Trial.study_id.in_(ids))
                    .group_by(Trial.study_id)
                )
            )
            .tuples()
            .all()
        )
        parked = set(
            await session.scalars(
                select(Trial.study_id)
                .where(
                    Trial.study_id.in_(ids),
                    Trial.state == TrialState.PRUNED,
                    Trial.message.in_([PENDING_SEND, PENDING_FULL]),
                )
                .distinct()
            )
        )
        queued = set(
            await session.scalars(
                select(SimulationRecord.task)
                .where(
                    SimulationRecord.task.in_(tasks), SimulationRecord.status == SimStatus.QUEUED
                )
                .distinct()
            )
        )
    return {
        r.id
        for r in rows
        if not r.stopping
        and (spent.get(r.id, 0) < r.max_trials or r.id in parked or r.task in queued)
    }


def _param(key: str) -> Any:
    """SQL for one of a task's stored parameters."""
    return func.json_extract(Study.sampler_params, f"$.{key}")


async def start_waiting(optimizer: Optimizer) -> int:
    """Start queued tasks while their cores fit in the engine's slots. Returns how many."""
    async with scheduling:
        async with optimizer.db.session() as session:
            # Only what scheduling reads: whole, a Search Lab task's parameters are megabytes,
            # decoded every few seconds.
            rows = (
                await session.execute(
                    select(
                        Study.id,
                        Study.task,
                        Study.status,
                        Study.max_trials,
                        func.coalesce(_param("cores"), 1).label("cores"),
                        _param("queuedAt").label("queued_at"),
                        _param("stopping").label("stopping"),
                    ).where(
                        Study.sampler.in_(TASK_SAMPLERS),
                        Study.status.in_([StudyStatus.RUNNING, StudyStatus.QUEUED]),
                    )
                )
            ).all()
        running = [r for r in rows if r.status == StudyStatus.RUNNING]
        if optimizer.engine.lend_idle_cores and running:
            # Lending: a task that has sent everything holds no cores, only the batches still
            # out, which the engine never takes back. Otherwise a task waiting for its cores
            # stands idle beside them until the last of those batches returns.
            sending = await _still_sending(optimizer, running)
            running = [r for r in running if r.id in sending]
        used = sum(r.cores for r in running)
        waiting = sorted(
            (r for r in rows if r.status == StudyStatus.QUEUED),
            key=lambda r: (r.queued_at or "", r.id),
        )
        by_id = {r.id: r for r in waiting}
        started = to_start([(r.id, r.cores) for r in waiting], used, optimizer.engine.slots)
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
            await optimizer.engine.set_quota(by_id[task_id].task, by_id[task_id].cores)
            used += by_id[task_id].cores
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
    refused: set[refusals.Refusal] | None = None,
) -> tuple[list[tuple[Any, dict[str, Any], SimulationRequest]], str | None]:
    """Ask for ``want`` new simulations. Runs off the event loop.

    A point this task already scored is answered with its score, one that failed is told as
    failed, and a repeat, or a point its lab cannot write, is pruned; each time another
    point is asked, so quota is only spent on Alphas the task has not seen. So is a point
    repeating a setting BRAIN ``refused`` in its market earlier in the task: Optuna cannot drop
    a choice from a running study, so the refusal is honoured here instead.

    Also returns the first setting BRAIN refused, so a task left with nothing to ask can say
    whether BRAIN withdrew what it searches or the search ran out of new points.
    """
    from optuna.trial import TrialState as OptunaState

    space = run.space
    if cover and not study.get_trials(deepcopy=False, states=(OptunaState.WAITING,)):
        for field_id in cover:
            study.enqueue_trial(lab.first_pass(space, field_id))

    choices = lab.field_choices(space)
    picked: list[tuple[Any, dict[str, Any], SimulationRequest]] = []
    keys: set[tuple[str, str]] = set()
    withdrawn: str | None = None
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
            withdrawn = withdrawn or problems[0]
            study.tell(trial, state=OptunaState.PRUNED)
            continue
        wire = None if request is None else _wire(request)
        if wire is not None and (hit := refusals.repeated(wire, refused or set())) is not None:
            withdrawn = withdrawn or refusals.describe(hit)
            study.tell(trial, state=OptunaState.PRUNED)
            continue
        key = None if request is None or wire is None else search.identity_of(request.regular, wire)
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
    return picked, withdrawn


async def advance(optimizer: Optimizer, row: Study) -> int:
    """Refill the task's cores as its batches come back. Returns how many were queued.

    A task keeps ten simulations in flight per core. Room freed by a returning batch is
    filled at once, in tens so every batch stays full, rather than once the slowest batch
    of a round is back. ``row`` is the task as this tick read it.
    """
    if row.status != StudyStatus.RUNNING:
        return 0
    study_id = row.id
    async with optimizer.db.session() as session:
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
    room = row.batch_size + optimizer.lendable_cores * MULTI_SIMULATION_SIZE
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
            await finish(optimizer, study_id, StudyStatus.COMPLETE, None)
        return 0
    want = to_ask(room, int(in_flight or 0), row.max_trials - committed)
    if row.sampler in (GA_SAMPLER, SEARCH_SAMPLER, TEMPLATE_SAMPLER) and want > 0:
        # Put back by a pause (`repark`), or left by a crash between writing a round's
        # trials and sending them (`_queue`).
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
    if row.sampler == POWER_POOL_SAMPLER:
        from . import power_pool  # imported here: labs.power_pool builds on this module

        return await power_pool.refill(optimizer, row, want, waiting)
    if row.sampler == FIELDS_SAMPLER:
        from ..tools import fields_sampler  # same cycle as settings_sampler below

        return await fields_sampler.refill(optimizer, row, want, waiting)
    # These write every simulation up front, so all are drained the same way.
    if row.sampler in (
        SETTINGS_SAMPLER,
        CORRELATION_BREAKER,
        SUPER_LAB,
        PASTE_ALPHAS,
    ):
        from ..tools import settings_sampler  # same cycle: it builds on this module

        return await settings_sampler.refill(optimizer, row, want, waiting)
    if want <= 0:
        return 0

    if row.sampler == TEMPLATE_SAMPLER:
        lab = template
        run: SearchParams = params_of(row, TemplateParams)
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
                    Trial.score,
                    Trial.message,
                ).where(Trial.study_id == study_id, Trial.state != TrialState.PRUNED)
            )
        ).all()
    tried, seen, refused = await asyncio.to_thread(_search_memory, counted, key)
    limit = row.max_trials // 2
    cover: list[str] = []
    if len(tried) < limit:
        untried = [f for f in coverable if f not in tried]
        cover = untried[: min(want, limit - len(tried))]

    study = await optimizer.optuna_study(study_id, row, trim=True)
    schema = await optimizer.metadata.cached_settings_schema()
    picked, withdrawn = await asyncio.to_thread(
        ask_points, study, lab, run, want, seen, cover, schema, refused
    )
    if not picked:
        # Nothing new left to ask: the task is done once what is out has come back.
        if not waiting:
            await finish(
                optimizer,
                study_id,
                StudyStatus.COMPLETE,
                f"Stopped early: BRAIN no longer accepts a setting this task searches "
                f"({withdrawn})."
                if withdrawn
                else "Stopped early: every point the search drew had been simulated already.",
            )
        return 0

    return await _queue(optimizer, row, last_number, picked)


def _wire(request: SimulationRequest) -> dict[str, Any]:
    """A request's settings as BRAIN reads them, the shape trials store them in."""
    return request.settings.model_dump(by_alias=True, exclude_none=True)


def raw_json(column: Any) -> Any:
    """A JSON column read back as its stored text, to be decoded off the event loop."""
    return type_coerce(column, String)


def _not_free() -> Any:
    """SQL for ``message != FREE`` as Python means it: a trial with no message counts too."""
    return or_(Trial.message.is_(None), Trial.message != FREE)


def full_run() -> Any:
    """SQL for a trial that runs a passing Quick Alpha again in Full mode."""
    return Trial.full_of.is_not(None)


def _search_memory(
    rows: Sequence[Any], covers: str
) -> tuple[set[Any], dict[tuple[str, str], float | bool], set[refusals.Refusal]]:
    """The fields tried as ``covers``, every point already scored, and every setting BRAIN
    refused in a market (:mod:`.refusals`), from unpruned trials.

    Runs in a worker thread: identity keys validate each trial's settings, which costs
    seconds at the largest task sizes. A task reuses a few settings many times over, so each
    is validated once: measured, a seventh of the time.
    """

    @functools.cache
    def settings_of(raw: str | None) -> str:
        return search.settings_identity(loads(raw or "null"))

    tried: set[Any] = set()
    seen: dict[tuple[str, str], float | bool] = {}
    refused: set[refusals.Refusal] = set()
    for state, params, expression, settings, score, message in rows:
        tried.add((loads(params or "null") or {}).get(covers))
        key = (expression or "", settings_of(settings))
        if state == TrialState.COMPLETE and score is not None:
            seen[key] = float(score)
        elif state == TrialState.FAIL:
            seen[key] = False
            refused |= refusals.from_message(message, loads(settings or "null"))
    return tried, seen, refused


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
    slots = field_slots(row)
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
                        name: FROM_SPACE if name in slots else distribution_to_json(distribution)
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
        # Only what the stall check reads: spent children's scores, in the order asked. Sorted
        # here: ordered in SQL, the read left the index for every child's row, 120 ms against 10.
        spent_rows = sorted(
            (
                await session.execute(
                    select(Trial.number, Trial.state, Trial.score, Trial.implausible).where(
                        Trial.study_id == row.id,
                        Trial.generation > 0,
                        _not_free(),
                        Trial.state.in_([TrialState.COMPLETE, TrialState.FAIL]),
                    )
                )
            ).tuples(),
            key=lambda spent: spent[0],
        )
    if await asyncio.to_thread(lambda: ga.stalled(_spent(spent_rows))):
        if not waiting:
            await finish(optimizer, row.id, StudyStatus.COMPLETE, ga.STALLED)
        return 0
    if want <= 0:
        return 0
    picked = await ga.breed(optimizer, row, want)
    if picked and (known := await known_refusals(optimizer, row.id)):
        # A child keeps its parent's settings, so it would repeat its parent's refusal.
        picked = [
            (params, request)
            for params, request in picked
            if refusals.repeated(_wire(request), known) is None
        ]
    if not picked:
        if not waiting:
            async with optimizer.db.session() as session:
                bred = bool(
                    await session.scalar(
                        select(func.count()).where(Trial.study_id == row.id, Trial.generation > 0)
                    )
                )
            await finish(
                optimizer,
                row.id,
                StudyStatus.COMPLETE if bred else StudyStatus.FAILED,
                "No new child could be bred: every child of these parents was already "
                "simulated or is not a valid Alpha here.",
            )
        return 0
    return await _queue(
        optimizer, row, last_number, [(None, params, req) for params, req in picked]
    )


def _spent(rows: Sequence[Any]) -> list[float | None]:
    """Each spent child's score, ``None`` for one that failed. An implausible one failed too:
    counted as found, a straight line would keep a stalled search breeding."""
    return [
        float(score) if state == TrialState.COMPLETE and score is not None and not flagged else None
        for _number, state, score, flagged in rows
    ]


async def stop_task(optimizer: Optimizer, row: Study, *, force: bool = False) -> None:
    """Finish a task early. Simulations already sent still finish and are scored.

    ``force`` is the second press, for a task that has been stopping and has not stopped.
    An ordinary stop is cooperative: it waits for what is already out on BRAIN, which is
    right, because that quota is spent either way. But it relies on every outstanding
    simulation reaching a terminal state, and one that never does — BRAIN loses it, a poll
    never resolves — holds the task RUNNING and its cores for good, with nothing on screen
    to press but the button that already did nothing.

    So a forced stop cancels what it can on BRAIN, closes the trials whatever their
    simulations are doing, and ends the task. Anything BRAIN keeps running still lands in
    the vault; it simply stops holding a task open.
    """
    async with optimizer.lock(row.id):
        await optimizer.engine.drop_queued(row.task)
        if force:
            await optimizer.engine.abandon(row.task)
        await prune_unsent(optimizer, row.id, everything=force)
        counts = await optimizer.counts(row.id)
        out = counts.get(TrialState.QUEUED, 0) + counts.get(TrialState.RUNNING, 0)
        async with optimizer.db.session() as session:
            stored = await session.get(Study, row.id)
            if stored is not None:
                params = task_params(stored)
                params.stopping = True
                stored.sampler_params = params.dump()
                # With simulations still out it runs on only to score them.
                if out:
                    stored.status = StudyStatus.RUNNING
                else:
                    stored.status = StudyStatus.COMPLETE
                    stored.finished_at = utcnow()
                await session.commit()
    await start_waiting(optimizer)
    await optimizer.notify()


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
            # By the change in cores, so a task created with a spare batch keeps it.
            stored.batch_size += (cores - task_params(stored).cores) * MULTI_SIMULATION_SIZE
            if simulations is not None:
                stored.max_trials = simulations
            params = task_params(stored)
            params.cores = cores
            stored.sampler_params = params.dump()
            await session.commit()
        await optimizer.engine.set_quota(row.task, cores)
    await optimizer.notify()
    return None


#: Why a Template Lab task added while templates were built from blocks was closed.
BLOCK_TEMPLATE = (
    "Added with an older Template Lab that no longer runs. Add the template again to carry on."
)


async def retire_block_templates(optimizer: Optimizer) -> int:
    """Close every unfinished Template Lab task added while templates were built from blocks:
    nothing can run one now. Their Alphas stay where they are."""
    async with optimizer.db.session() as session:
        old = list(
            (
                await session.scalars(
                    select(Study.id).where(
                        Study.sampler == TEMPLATE_SAMPLER,
                        Study.status != StudyStatus.COMPLETE,
                        func.json_extract(Study.sampler_params, "$.tree").is_not(None),
                    )
                )
            ).all()
        )
    for study_id in old:
        await finish(optimizer, study_id, StudyStatus.COMPLETE, BLOCK_TEMPLATE, judge=False)
    if old:
        log.info("tasks.block_templates_closed", count=len(old))
    return len(old)


async def finish(
    optimizer: Optimizer,
    study_id: int,
    status: StudyStatus,
    message: str | None,
    *,
    judge: bool = True,
) -> None:
    """End a task, and stop it spending: its unsent simulations leave the queue.

    Dropping the queue matters as much as the status, or the engine goes on sending work
    nobody will score. Simulations already sent finish; running the task again scores them.

    A task ending *Complete* with nothing scored and something failed ends *Failed* instead,
    with its commonest reason: "Complete" with no Alphas is how a whole run lost to one refused
    setting once looked. Not for a task the person stopped, nor where ``judge`` is off. A
    ``message`` of ``None`` keeps the task's own.
    """
    async with optimizer.db.session() as session:
        stored = await session.get(Study, study_id)
        if stored is not None:
            if status == StudyStatus.COMPLETE and judge and not task_params(stored).stopping:
                reason = await _nothing_scored(session, study_id)
                if reason is not None:
                    status, message = StudyStatus.FAILED, reason
            stored.status = status
            if message is not None:
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


async def known_refusals(optimizer: Optimizer, study_id: int) -> set[refusals.Refusal]:
    """The settings BRAIN refused this task, each in the market it refused it in."""
    async with optimizer.db.session() as session:
        rows = (
            await session.execute(
                select(Trial.message, Trial.settings).where(
                    Trial.study_id == study_id,
                    Trial.state == TrialState.FAIL,
                    Trial.message.startswith(refusals.REFUSED),
                )
            )
        ).tuples()
        return refusals.learned(rows)


#: How many refusals each task's parked trials were last checked against: they are read again
#: only once the task has learned another, not on every tick.
_checked_against: dict[int, int] = {}


async def close_refused(optimizer: Optimizer, study_id: int, parked_as: str) -> int:
    """Fail the trials parked as ``parked_as`` that repeat a refusal BRAIN made earlier in the
    task, each naming it: sent, each would be refused again. Failed rather than pruned, so they
    count among the task's failures. Returns how many."""
    known = await known_refusals(optimizer, study_id)
    if len(known) <= _checked_against.get(study_id, 0):
        return 0
    _checked_against[study_id] = len(known)
    closed = 0
    async with optimizer.db.session() as session:
        parked = await session.scalars(
            select(Trial).where(
                Trial.study_id == study_id,
                Trial.state == TrialState.PRUNED,
                Trial.message == parked_as,
            )
        )
        for trial in parked:
            if (hit := refusals.repeated(trial.settings or {}, known)) is not None:
                trial.state = TrialState.FAIL
                trial.message = refusals.held_back(hit)
                trial.finished_at = utcnow()
                closed += 1
    if closed:
        log.warning("tasks.refusal_held_back", study_id=study_id, trials=closed)
    return closed


async def _nothing_scored(session: Any, study_id: int) -> str | None:
    """Why a task that scored nothing failed, or ``None`` when it scored something or nothing
    failed. Evolution's seeds were scored before the task began, so they do not count."""
    scored = await session.scalar(
        select(func.count()).where(
            Trial.study_id == study_id,
            Trial.state == TrialState.COMPLETE,
            or_(Trial.generation.is_(None), Trial.generation != 0),
        )
    )
    if scored:
        return None
    reasons = (
        await session.execute(
            select(Trial.message, func.count())
            .where(Trial.study_id == study_id, Trial.state == TrialState.FAIL)
            .group_by(Trial.message)
        )
    ).all()
    if not reasons:
        return None
    # BRAIN's own words first: one refused batch says more than the many held back after it.
    message, _ = max(reasons, key=lambda r: (str(r[0] or "").startswith(refusals.REFUSED), r[1]))
    total = sum(n for _, n in reasons)
    return f"None of its {total} simulations scored. {message or 'No reason was given.'}"


async def prune_unsent(optimizer: Optimizer, study_id: int, *, everything: bool = False) -> int:
    """Put back the trials whose simulation was taken off the queue before it was sent.

    They spent nothing, so each waits to be sent when the task next runs (:func:`repark`).
    Failing them would teach the search that these points score badly.

    ``everything`` also closes every trial already sent, for a forced stop. Pruned rather than
    failed for the same reason: an unscored point is not evidence that the point is bad.
    """
    from optuna.trial import TrialState as OptunaState

    live = optimizer.open_trials.get(study_id, {})
    study = optimizer.studies.get(study_id)
    closed = 0
    async with optimizer.db.session() as session:
        row = await session.get(Study, study_id)
        sampler = row.sampler if row is not None else ""
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
        never_sent = set(
            (
                await session.scalars(
                    select(SimulationRecord.id).where(
                        SimulationRecord.id.in_(ids),
                        SimulationRecord.status == SimStatus.CANCELLED,
                        SimulationRecord.sent_at.is_(None),
                    )
                )
            ).all()
        )
        for trial in open_trials:
            if not trial.simulation_record_id or trial.simulation_record_id in never_sent:
                # Its live Optuna trial stays open, to be told when the resend comes back.
                repark(trial, sampler)
            elif everything:
                trial.state = TrialState.PRUNED
                trial.message = "The task was stopped before this finished."
                trial.finished_at = utcnow()
                optuna_trial = live.pop(trial.number, None)
                if study is not None and optuna_trial is not None:
                    study.tell(optuna_trial, state=OptunaState.PRUNED, skip_if_finished=True)
            else:
                continue
            closed += 1
        await session.commit()
    return closed


async def pause(optimizer: Optimizer, study_id: int) -> int | None:
    """Queue nothing more for a task: what is out finishes, the rest waits for it to run again.

    Returns how many queued simulations came off the queue, or ``None`` when the task is not
    running or waiting, or is already stopping.
    """
    # Read under the lock: checked outside it, a task that finished its last trial in the
    # meantime was written back to PAUSED, and a finished task holds no cores.
    async with optimizer.lock(study_id):
        row = await optimizer.get(study_id)
        if (
            row is None
            or row.status not in (StudyStatus.RUNNING, StudyStatus.QUEUED)
            or task_params(row).stopping
        ):
            return None
        await optimizer.set_status(study_id, StudyStatus.PAUSED)
        dropped = await optimizer.engine.drop_queued(row.task)
        await prune_unsent(optimizer, study_id)
    await start_waiting(optimizer)
    return dropped


if __name__ == "__main__":
    import sys
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace

    import optuna as _optuna
    from sqlalchemy.dialects import sqlite

    from ..db.sqlite import Database
    from ..engine.slots import BatchEngine
    from .params import SETTINGS_SAMPLER

    usa = {"region": "USA", "delay": 1, "universe": "TOP3000", "testPeriod": "P6Y3M0D"}
    eur = {**usa, "region": "EUR", "universe": "TOP2500"}
    refused_once = f"{refusals.REFUSED} — settings.testPeriod: Must be at most P6Y0M0D"
    known = refusals.from_fields({"settings": {"testPeriod": ["too long"]}}, usa)

    async def nothing(*_args: Any) -> None:
        return None

    async def refusals_end_to_end() -> dict[str, bool]:
        found: dict[str, bool] = {}
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "harness.db")
            try:
                await db.create_all()
                engine = SimpleNamespace(drop_queued=nothing, set_quota=nothing)
                optimizer: Any = SimpleNamespace(
                    db=db, engine=engine, notify=nothing, open_trials={}, studies={}
                )

                async def task(name: str, status: str = StudyStatus.RUNNING, **params: Any) -> int:
                    async with db.session() as session:
                        row = Study(
                            name=name,
                            template_source="",
                            sampler=SETTINGS_SAMPLER,
                            task=name,
                            status=status,
                            sampler_params={"region": "USA", "delay": 1, **params},
                        )
                        session.add(row)
                        await session.flush()
                        session.add(
                            Trial(
                                study_id=row.id,
                                number=0,
                                settings=usa,
                                state=TrialState.FAIL,
                                message=refused_once,
                            )
                        )
                        return row.id

                async def status(study_id: int) -> str:
                    async with db.session() as session:
                        row = await session.get(Study, study_id)
                        return row.status if row else ""

                refused = await task("refused")
                await finish(optimizer, refused, StudyStatus.COMPLETE, "")
                found["refused throughout ends Failed"] = await status(refused) == "FAILED"
                stopped = await task("stopped", stopping=True)
                await finish(optimizer, stopped, StudyStatus.COMPLETE, "")
                found["a person's Stop stays Complete"] = await status(stopped) == "COMPLETE"

                sweep = await task("sweep")
                async with db.session() as session:
                    session.add_all(
                        Trial(
                            study_id=sweep,
                            number=n,
                            settings=usa if n % 2 else eur,
                            state=TrialState.PRUNED,
                            message=PENDING_SEND,
                        )
                        for n in range(1, 11)
                    )
                closed = await close_refused(optimizer, sweep, PENDING_SEND)
                found["only the refused market's parked trials close"] = closed == 5

                quotas: list[tuple[str, int]] = []

                async def quota(task: str, cores: int) -> None:
                    quotas.append((task, cores))

                # "sweep" runs on the one core a task without ``cores`` holds; three are free.
                engine.slots, engine.lend_idle_cores, engine.set_quota = 4, False, quota
                await task("late", StudyStatus.QUEUED, cores=2, queuedAt="2026-01-02")
                await task("early", StudyStatus.QUEUED, cores=2, queuedAt="2026-01-01")
                await task("small", StudyStatus.QUEUED, queuedAt="2026-01-03")
                await start_waiting(optimizer)
                found["queued tasks start oldest first while their cores fit"] = quotas == [
                    ("early", 2),
                    ("small", 1),
                ]

                async with db.session() as session:
                    for n, settings in enumerate([usa, eur]):
                        session.add(
                            SimulationRecord(
                                request_hash=f"h{n}",
                                payload={"settings": settings},
                                expression="rank(x)",
                                sim_type="REGULAR",
                                instrument_type="EQUITY",
                                region=settings["region"],
                                delay=1,
                                language="FASTEXPR",
                                task="queued",
                                status=SimStatus.QUEUED,
                            )
                        )
                engine_like: Any = SimpleNamespace(db=db, tracker=SimpleNamespace(notify=nothing))
                held = await BatchEngine.hold_back(engine_like, "queued", known)
                found["the engine holds back only the refused market"] = held == 1

                tally = await task("tally")
                async with db.session() as session:
                    session.add_all(
                        Trial(study_id=tally, number=n, state=s, values=v, params=p, message=m)
                        for n, (s, v, p, m) in enumerate(
                            [
                                (TrialState.COMPLETE, [1.5], {}, None),
                                (TrialState.COMPLETE, [2], {"fullOf": 0}, None),
                                (TrialState.COMPLETE, [], {"fullOf": None}, FREE),
                                (TrialState.PRUNED, None, {}, PENDING_SEND),
                            ],
                            start=1,
                        )
                    )
                committed = select(
                    func.count().filter(
                        Trial.state != TrialState.PRUNED, _not_free(), not_(full_run())
                    )
                ).where(Trial.study_id == tally)
                # Planned as sent, bound values and all: a bound JSON path misses the index.
                compiled = committed.compile(dialect=sqlite.dialect())
                bound = tuple(compiled.params[name] for name in compiled.positiontup or ())
                async with db.session() as session:
                    plan = await (await session.connection()).exec_driver_sql(
                        f"EXPLAIN QUERY PLAN {compiled}", bound
                    )
                    counted = await session.scalar(committed)
                    scores = await session.execute(
                        select(Trial.number, Trial.score).where(Trial.study_id == tally)
                    )
                    found["a tick's count reads the index alone"] = any(
                        "COVERING INDEX ix_trial_score" in str(row[3]) for row in plan
                    )
                    # The refused trial and number 1: neither a Full run, a free one nor pruned.
                    found["and counts what the JSON says"] = counted == 2 and dict(
                        scores.tuples().all()
                    ) == {0: None, 1: 1.5, 2: 2, 3: None, 4: None}
            finally:
                await db.dispose()
        return found

    class _Lab:
        @staticmethod
        def field_choices(_space: Any) -> dict[str, Any]:
            return {}

        @staticmethod
        def draw(trial: Any, _run: Any, _choices: Any) -> tuple[dict[str, Any], SimulationRequest]:
            n = trial.suggest_int("n", 0, 1000)
            settings = SimulationSettings.model_validate({**usa, "neutralization": "MARKET"})
            return {"n": n}, SimulationRequest(settings=settings, regular=f"rank(x{n})")

    _optuna.logging.set_verbosity(_optuna.logging.ERROR)
    run = SearchParams(region="USA", delay=1, space={})
    pruned, why = ask_points(_optuna.create_study(), _Lab, run, 10, {}, [], None, known)
    asked, _ = ask_points(_optuna.create_study(), _Lab, run, 10, {}, [], None, set())
    checks = asyncio.run(refusals_end_to_end())
    checks["a refused market is never asked again"] = pruned == [] and why is not None
    checks["an unrefused one is"] = len(asked) == 10
    if failed := [name for name, ok in checks.items() if not ok]:
        raise SystemExit(f"scheduler self-check failed: {'; '.join(failed)}")
    sys.stdout.write(f"{len(checks)} cases as expected\n")
