"""Optuna, driven in rounds that fill whole batches.

``study.optimize`` owns the thread and evaluates one trial at a time; here a trial takes
minutes on someone else's machine and eighty run at once, so the study is driven by
ask/tell:

    ask N points  ->  build N simulations  ->  enqueue  ->  ... wait ...
    -> harvest finished alphas -> tell -> ask the next N

**N is a multiple of ten.** A multi-simulation carries ten children across eight concurrent
slots, so a round of eighty fills the platform exactly; asking for 47 leaves part-empty
batches occupying whole slots for the length of their run. Batch-splitting settings —
region, delay, instrument type, language — are pinned by default, because varying region
across a round of eighty produces eighty batch keys and turns eighty concurrent simulations
into eight.

Restart safety: the Optuna study is rebuilt from the trial rows, never persisted
separately, so trials stay joinable to the simulations that produced them.
"""

import asyncio
import contextlib
import sqlite3
import time
import warnings
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import duckdb
import structlog
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from ..brain.connection import background
from ..brain.errors import (
    BrainAuthError,
    BrainBusy,
    BrainDailyLimitReached,
    BrainError,
    BrainPaused,
    BrainPollTimeout,
    BrainRateLimited,
    BrainServerError,
    BrainServiceUnavailable,
    BrainTransportError,
)
from ..brain.platform import IGNORED_CHECKS, QUOTA_CHECKS
from ..brain.schemas import FULL_MODE, QUICK_MODE
from ..db.models import (
    SimStatus,
    SimulationRecord,
    Study,
    StudyStatus,
    Trial,
    TrialState,
    utcnow,
)
from ..db.sqlite import loads
from ..vault.yields import checks_of, clean, gating_results, implausible, limits_of, verdict
from . import objectives as obj
from . import scheduler
from .objectives import StudyNotFoundError
from .params import (
    PASTE_ALPHAS,
    SEARCH_SAMPLER,
    TASK_SAMPLERS,
    TEMPLATE_SAMPLER,
    SearchParams,
    params_of,
)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Awaitable, Callable, Sequence

    import optuna

    from ..account import PlatformMetadata
    from ..brain.endpoints import BrainEndpoints
    from ..db.sqlite import Database
    from ..engine.slots import BatchEngine
    from ..llm.service import LLMService
    from ..vault.backfill import Backfill
    from ..vault.store import AlphaVault

log = structlog.get_logger(__name__)

#: How often a running study looks for finished trials.
POLL_SECONDS = 5.0
#: How long a study may keep failing for a reason that passes by itself before it is failed.
TRANSIENT_FOR_SECONDS = 30 * 60
#: Ticks in a row a study may fail for any other reason: one glitch is not a broken task.
FAULTS_ALLOWED = 3


def _failure_kind(exc: BaseException) -> str:
    """``wait`` when BRAIN cannot be asked at all, which the connection banner already says;
    ``transient`` when it passes by itself; ``fault`` for anything that may not."""
    if isinstance(exc, BrainPaused | BrainAuthError | BrainDailyLimitReached):
        return "wait"
    if isinstance(
        exc,
        BrainRateLimited
        | BrainBusy
        | BrainServiceUnavailable
        | BrainServerError
        | BrainTransportError
        | BrainPollTimeout
        | TimeoutError
        | duckdb.TransactionException,
    ):
        return "transient"
    # Only a lock: the same class also carries a missing table or a full disk.
    if isinstance(exc, OperationalError) and isinstance(exc.orig, sqlite3.OperationalError):
        reason = str(exc.orig).lower()
        if "locked" in reason or "busy" in reason:
            return "transient"
    return "fault"


#: Statistics the local Alpha store keeps, so objectives on them need no second BRAIN read.
VAULT_STATS = frozenset(
    {"sharpe", "fitness", "turnover", "returns", "drawdown", "margin"}
    | {"train_sharpe", "train_fitness"}
)
#: How long a finished Alpha may take to reach the local store before BRAIN is asked.
VAULT_WAIT = timedelta(minutes=5)
#: How long after its simulation finished a trial keeps waiting for BRAIN to hand its Alpha
#: back through an outage or a closed session, before it fails for good.
READ_BACK_PATIENCE = timedelta(hours=1)


class Optimizer:
    """Owns every running study."""

    #: Set by ``AppState`` once the assistant exists; LLM Power Pool Lab tasks call it.
    llm: LLMService

    def __init__(
        self,
        db: Database,
        engine: BatchEngine,
        endpoints: BrainEndpoints,
        metadata: PlatformMetadata,
        *,
        on_change: Callable[[dict[str, Any]], Awaitable[None]],
        alphas: AlphaVault,
        backfill: Backfill | None = None,
    ) -> None:
        self.db = db
        self.engine = engine
        self.endpoints = endpoints
        self.metadata = metadata
        self._on_change = on_change
        #: Stored alphas: Evolution Lab parents, Power Pool Lab context.
        self.alphas = alphas
        self.backfill = backfill
        self.studies: dict[int, optuna.Study] = {}
        #: Live Optuna trial objects for the round in flight, keyed study -> our trial
        #: number. Lost on restart, which :func:`_tell_many` handles by replaying.
        self.open_trials: dict[int, dict[int, Any]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        #: Cores no running task holds, while Settings lends them; 0 otherwise. Set each
        #: tick by :func:`scheduler.start_waiting`, read by :func:`scheduler.advance`.
        self.lendable_cores = 0
        #: Study id -> ticks in a row it failed, and when the first of them did.
        self._failing: dict[int, tuple[int, float]] = {}
        self._task: asyncio.Task[None] | None = None

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        await scheduler.retire_block_templates(self)
        self._task = asyncio.create_task(self._run(), name="optimizer")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        log.info("optimize.stopped")

    async def _run(self) -> None:
        background()
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("optimize.tick_failed")
            await asyncio.sleep(POLL_SECONDS)

    async def tick(self) -> dict[int, dict[str, int]]:
        """Start waiting tasks that fit, then advance every running study by one round."""

        try:
            await scheduler.start_waiting(self)
        except Exception:
            log.exception("optimize.schedule_failed")
        async with self.db.session() as session:
            ids = list(
                (
                    await session.scalars(
                        select(Study.id).where(Study.status == StudyStatus.RUNNING)
                    )
                ).all()
            )
        # A study is rebuilt from its rows when its task runs again; kept, every task that ever
        # ran held its whole search in memory for the life of the process.
        for study_id in self.studies.keys() - set(ids):
            del self.studies[study_id]
            self.open_trials.pop(study_id, None)
        results: dict[int, dict[str, int]] = {}
        for study_id in ids:
            try:
                results[study_id] = await self.advance(study_id)
            except Exception as exc:  # noqa: BLE001 - _give_up sorts and logs every error
                reason = self._give_up(study_id, exc)
                if reason is not None:
                    await scheduler.finish(self, study_id, StudyStatus.FAILED, reason)
            else:
                self._failing.pop(study_id, None)
        return results

    def _give_up(self, study_id: int, exc: Exception) -> str | None:
        """Why ``study_id`` should fail now, or ``None`` to try it again next tick."""
        kind = _failure_kind(exc)
        if kind == "wait":
            log.info("optimize.study_waiting", study_id=study_id, error=str(exc))
            return None
        now = time.monotonic()
        count, since = self._failing.get(study_id, (0, now))
        self._failing[study_id] = (count + 1, since)
        if count == 0:
            log.exception("optimize.study_error", study_id=study_id, kind=kind)
        else:
            log.warning("optimize.study_error", study_id=study_id, kind=kind, error=str(exc))
        if kind == "transient" and now - since < TRANSIENT_FOR_SECONDS:
            return None
        if kind == "fault" and count + 1 < FAULTS_ALLOWED:
            return None
        del self._failing[study_id]
        log.error("optimize.study_failed", study_id=study_id, kind=kind)
        reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        return reason if kind == "fault" else f"Kept failing for 30 minutes. {reason}"

    def lock(self, study_id: int) -> asyncio.Lock:
        return self._locks.setdefault(study_id, asyncio.Lock())

    # -- control ---------------------------------------------------------

    async def set_status(self, study_id: int, status: StudyStatus) -> Study:
        async with self.db.session() as session:
            row = await session.get(Study, study_id)
            if row is None:
                raise StudyNotFoundError(study_id)
            row.status = status
            # Only the first start: resuming a paused task continues the same run.
            if status == StudyStatus.RUNNING and row.started_at is None:
                row.started_at = utcnow()
            if status in (StudyStatus.COMPLETE, StudyStatus.FAILED):
                row.finished_at = utcnow()
            await session.commit()
            await session.refresh(row)
        if status in (StudyStatus.COMPLETE, StudyStatus.FAILED):
            # A finished task holds no cores; running it again hands them back.
            await self.engine.set_quota(row.task, 0)
        await self.notify()
        return row

    # -- the round -------------------------------------------------------

    async def advance(self, study_id: int) -> dict[str, int]:
        """Harvest what finished, then ask for the next round if there is room."""
        async with self.lock(study_id):
            # Read once for the whole tick, a Search Lab space being megabytes: the lock holds off
            # every change to the row that harvesting and asking read.
            row = await self.get(study_id)
            if row is None:
                return {"told": 0, "asked": 0}
            if row.sampler not in TASK_SAMPLERS:
                raise ValueError(f"No lab runs studies with the {row.sampler!r} sampler.")

            # Evolution Lab breeds its own children: there is no sampler to tell.
            told = await self._harvest(row, tell=row.sampler in (SEARCH_SAMPLER, TEMPLATE_SAMPLER))
            asked = await scheduler.advance(self, row)
        if told or asked:
            await self.notify()
        return {"told": told, "asked": asked}

    async def _harvest(self, row: Study, *, tell: bool) -> int:
        """Score every trial whose simulation reached a terminal state.

        ``tell=False`` stores the scores without reporting them to Optuna, for GA studies.
        The Alpha the tracker already stored locally is read first instead of asking BRAIN
        for it again, which saves one request per simulation.
        """
        study_id = row.id
        async with self.db.session() as session:
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
            records = {
                r.id: r
                for r in (
                    await session.scalars(
                        select(SimulationRecord).where(
                            SimulationRecord.id.in_(
                                [
                                    t.simulation_record_id
                                    for t in open_trials
                                    if t.simulation_record_id
                                ]
                            )
                        )
                    )
                ).all()
            }

        objective_list = obj.resolve(list(row.objectives or []))
        saved: dict[str, dict[str, Any]] = {}
        if all(o.key in VAULT_STATS for o in objective_list):
            wanted = [
                (
                    records[t.simulation_record_id].alpha_id
                    if t.simulation_record_id in records
                    else None
                )
                or t.alpha_id
                for t in open_trials
            ]
            # A region-agnostic result is scored as its family: it has no statistics of its own.
            saved = await self.alphas.with_families(
                await self.alphas.by_ids([a for a in wanted if a])
            )
        #: (live optuna trial | None, stored row, values | None, summary)
        finished: list[tuple[Any, Trial, list[float] | None, dict[str, Any]]] = []
        started: list[int] = []
        #: Cancelled before they were sent, so they spent nothing and score nothing; each with
        #: whether its row was another task's, shared because the two asked the same thing.
        unsent: list[tuple[Trial, bool]] = []
        live = self.open_trials.get(study_id, {})

        for trial in open_trials:
            record = records.get(trial.simulation_record_id or -1)
            if record is None:
                finished.append(
                    (
                        live.get(trial.number),
                        trial,
                        None,
                        {"error": "The simulation row for this trial is gone."},
                    )
                )
                continue

            status = SimStatus(record.status)
            if not status.terminal:
                if status == SimStatus.RUNNING and trial.state != TrialState.RUNNING:
                    started.append(trial.id)
                continue
            if status == SimStatus.CANCELLED and record.sent_at is None:
                unsent.append((trial, record.task != row.task))
                continue

            alpha_id = record.alpha_id or trial.alpha_id
            if not alpha_id:
                finished.append(
                    (
                        live.get(trial.number),
                        trial,
                        None,
                        {
                            "error": record.message
                            or f"The simulation ended {record.status} with no alpha."
                        },
                    )
                )
                continue

            stored_alpha = saved.get(alpha_id)
            # Every objective, not just Sharpe: a row stored without its train block has
            # Sharpe but no Train Fitness, and scoring it would fail the trial for good.
            if stored_alpha is not None and all(
                stored_alpha.get(o.key) is not None for o in objective_list
            ):
                values = [float(stored_alpha[o.key]) for o in objective_list]
                summary = vault_summary(stored_alpha)
                finished.append((live.get(trial.number), trial, values, summary))
                continue
            if record.finished_at is not None and utcnow() - record.finished_at < VAULT_WAIT:
                continue  # The tracker stores it moments after it finishes.
            if stored_alpha is not None and stored_alpha.get("sim_type") == "RA_PARENT":
                # Asking BRAIN would not help: a parent has no statistics, and its children
                # have had the wait above to arrive.
                finished.append(
                    (
                        live.get(trial.number),
                        trial,
                        None,
                        {"error": "Fewer than two of its regions came back with scores."},
                    )
                )
                continue

            try:
                alpha = await self.endpoints.get_alpha(alpha_id)
            # A lookup that fails for any reason fails that trial, not the whole study. Unless
            # it failed only because BRAIN could not be asked: the Alpha is still there, and
            # failing it for good lost every result that finished during an outage.
            except Exception as exc:  # noqa: BLE001
                passing = isinstance(exc, BrainAuthError) or (
                    isinstance(exc, BrainError) and exc.retryable
                )
                finished_at = record.finished_at or utcnow()
                if passing and utcnow() - finished_at < READ_BACK_PATIENCE:
                    continue
                finished.append(
                    (
                        live.get(trial.number),
                        trial,
                        None,
                        {"error": f"Could not read the alpha back: {exc}"},
                    )
                )
                continue

            extra = obj.train_extra(alpha, objective_list)
            values = obj.extract(alpha.in_sample, objective_list, extra)
            summary = obj.summarise(alpha)
            finished.append((live.get(trial.number), trial, values, summary))

        if started:
            await self._mark_running(started)
        if unsent:
            await self._close_unsent(study_id, row.sampler, unsent)
        if not finished:
            return 0

        if tell:
            study = await self.optuna_study(study_id, row)
            # Only a trial asked before a restart is replayed, and only it needs what it was
            # asked over; the column is deferred, so it is read for those alone.
            replayed = [t.id for asked, t, _, _ in finished if asked is None]
            asked_over = await self._distributions(replayed) if replayed else {}
            loader = _Distributions(scheduler.field_slots(row) if replayed else {})
            await asyncio.to_thread(_tell_many, study, finished, asked_over, loader)

        # A Quick Alpha that passed every check BRAIN ran is owed a run in Full mode, which is
        # what BRAIN submits. Parked here; the scheduler sends it ahead of the search.
        owed = [
            t for _, t, values, summary in finished if values is not None and passed_quick(summary)
        ]
        async with self.db.session() as session:
            if owed:
                last = await session.scalar(
                    select(func.max(Trial.number)).where(Trial.study_id == study_id)
                )
                for number, quick in enumerate(owed, start=int(last or 0) + 1):
                    session.add(
                        Trial(
                            study_id=study_id,
                            number=number,
                            params={"fullOf": quick.number},
                            distributions={},
                            expression=quick.expression,
                            settings={**(quick.settings or {}), "simulationMode": FULL_MODE},
                            state=TrialState.PRUNED,
                            message=scheduler.PENDING_FULL,
                        )
                    )
            # One read for the lot rather than one per trial.
            stored_by_id = {
                t.id: t
                for t in await session.scalars(
                    select(Trial).where(Trial.id.in_([t.id for _, t, _, _ in finished]))
                )
            }
            for _optuna_trial, trial, values, summary in finished:
                live.pop(trial.number, None)
                stored = stored_by_id.get(trial.id)
                if stored is None:
                    continue
                stored.finished_at = utcnow()
                if values is None:
                    stored.state = TrialState.FAIL
                    stored.message = summary.get("error")
                else:
                    stored.state = TrialState.COMPLETE
                    stored.values = values
                    stored.result = summary
                    stored.alpha_id = summary.get("alphaId")
            await session.commit()

        if row.sampler == PASTE_ALPHAS and self.backfill is not None:
            # Copied back with figures worked out from the PnL, so every result's is wanted,
            # not only those nothing refused.
            self.backfill.schedule_returns(
                [str(s["alphaId"]) for _, _, _, s in finished if s.get("alphaId")]
            )

        log.info("optimize.told", study_id=study_id, trials=len(finished))
        return len(finished)

    async def _distributions(self, trial_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Each trial's stored distributions, by trial id."""
        async with self.db.session() as session:
            rows = await session.execute(
                select(Trial.id, Trial.distributions).where(Trial.id.in_(trial_ids))
            )
            return {trial_id: raw or {} for trial_id, raw in rows.tuples()}

    async def _close_unsent(
        self, study_id: int, sampler: str, unsent: list[tuple[Trial, bool]]
    ) -> None:
        """Settle trials whose simulation was cancelled before it was sent.

        One that shared another task's queued row waits to be sent again for itself, since
        that task, not this one, took the row off the queue. One whose own row was cancelled
        stays out, as asked, without counting as a failure.
        """
        from optuna.trial import TrialState as OptunaState

        live = self.open_trials.get(study_id, {})
        study = self.studies.get(study_id)
        shared = {trial.id: other for trial, other in unsent}
        async with self.db.session() as session:
            for stored in await session.scalars(select(Trial).where(Trial.id.in_(shared))):
                if shared[stored.id]:
                    scheduler.repark(stored, sampler)
                    continue
                stored.state = TrialState.PRUNED
                stored.message = "Cancelled before it was sent."
                stored.finished_at = utcnow()
                optuna_trial = live.pop(stored.number, None)
                if study is not None and optuna_trial is not None:
                    study.tell(optuna_trial, state=OptunaState.PRUNED, skip_if_finished=True)

    async def _mark_running(self, trial_ids: list[int]) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(Trial).where(Trial.id.in_(trial_ids)).values(state=TrialState.RUNNING)
            )
            await session.commit()

    # -- optuna ----------------------------------------------------------

    async def optuna_study(self, study_id: int, row: Study, *, trim: bool = False) -> optuna.Study:
        """The Optuna study, rebuilt from the trial rows on first use.

        With ``trim``, one grown past :data:`TRIM_AT` is cut to what :func:`kept` keeps. Its live
        trials are forgotten and replayed once they finish, so only a caller holding none of
        them may ask: the scheduler, before it asks for a round.
        """
        cached = self.studies.get(study_id)
        if cached is not None and not (trim and _outgrown(cached)):
            return cached

        from optuna.exceptions import ExperimentalWarning
        from optuna.samplers import TPESampler

        startup = params_of(row, SearchParams).n_startup_trials

        def sampler(dropped: int) -> Any:
            # Optuna still labels these arguments experimental and says so on every task start,
            # which only buried real warnings in the log.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ExperimentalWarning)
                return TPESampler(
                    # What a trim dropped still counts toward the random start.
                    n_startup_trials=max(0, startup - dropped),
                    multivariate=True,
                    group=True,
                    constant_liar=row.batch_size > 1,
                )

        if cached is not None:
            study = await asyncio.to_thread(_trimmed, cached, sampler)
            self.studies[study_id] = study
            self.open_trials.pop(study_id, None)
            return study

        async with self.db.session() as session:
            rows = (
                (
                    await session.execute(
                        select(
                            scheduler.raw_json(Trial.params),
                            scheduler.raw_json(Trial.distributions),
                            scheduler.raw_json(Trial.values),
                            Trial.penalty,
                            Trial.implausible,
                            Trial.state,
                        )
                        .where(Trial.study_id == study_id)
                        .order_by(Trial.number)
                    )
                )
                .tuples()
                .all()
            )

        loader = _Distributions(scheduler.field_slots(row))
        # Decoded in a worker too: on the event loop, 100,000 trials held it for seconds. The raw
        # text goes before the rebuild, which otherwise peaked 90 MB higher holding both.
        history = await asyncio.to_thread(_history, rows)
        del rows
        study = await asyncio.to_thread(_rebuild, sampler, history, loader)
        self.studies[study_id] = study
        return study

    def forget(self, study_id: int) -> None:
        """Drop cached state — after deletion, or when a study is reset."""
        self.studies.pop(study_id, None)
        self._locks.pop(study_id, None)
        self.open_trials.pop(study_id, None)

    # -- reading ---------------------------------------------------------

    async def counts(self, study_id: int) -> dict[str, int]:
        async with self.db.session() as session:
            rows = await session.execute(
                select(Trial.state, func.count())
                .where(Trial.study_id == study_id)
                .group_by(Trial.state)
            )
            return {str(state): int(n) for state, n in rows.all()}

    async def get(self, study_id: int) -> Study | None:
        async with self.db.session() as session:
            return await session.get(Study, study_id)

    async def delete(self, study_id: int) -> None:
        async with self.db.session() as session:
            row = await session.get(Study, study_id)
            if row is None:
                raise StudyNotFoundError(study_id)
            await session.delete(row)
            await session.commit()
        self.forget(study_id)

    async def notify(self) -> None:
        await self._on_change({"kind": "studies"})


# --- optuna plumbing, all synchronous ------------------------------------


def _history(rows: Sequence[Any]) -> list[dict[str, Any]]:
    """The trial rows :func:`_rebuild` replays, decoded from their stored JSON."""
    return [
        {
            "params": loads(params or "null") or {},
            "distributions": loads(distributions or "null") or {},
            "values": loads(values or "null"),
            "penalty": penalty,
            "implausible": flagged,
            "state": state,
        }
        for params, distributions, values, penalty, flagged, state in rows
    ]


def _tell_many(
    study: optuna.Study,
    finished: list[tuple[Any, Any, list[float] | None, dict[str, Any]]],
    asked_over: dict[int, dict[str, Any]],
    loader: _Distributions,
) -> None:
    """Report results.

    Two routes, because a trial asked before a restart no longer has a live object: a live
    one is told directly, and one without is added as an already-finished trial so the same
    information reaches the sampler without relying on Optuna's numbering matching ours.
    """
    from optuna.trial import TrialState as OptunaState

    for optuna_trial, stored, values, summary in finished:
        try:
            if optuna_trial is not None:
                if values is None:
                    study.tell(optuna_trial, state=OptunaState.FAIL, skip_if_finished=True)
                else:
                    told = obj.searched(values, summary.get("penalty"), summary.get("implausible"))
                    study.tell(optuna_trial, told, skip_if_finished=True)
                continue

            raw = asked_over.get(stored.id) or {}
            if values is None or not raw:
                # A failed replayed trial teaches the sampler nothing, and nor does a Full
                # re-run: the search never asked for it.
                continue
            distributions = loader.load(raw)
            if distributions is None:
                continue
            study.add_trial(
                _finished(
                    OptunaState.COMPLETE,
                    _optuna_params(stored.params or {}, distributions),
                    distributions,
                    obj.searched(values, summary.get("penalty"), summary.get("implausible")),
                )
            )
        except Exception:
            log.warning("optimize.tell_failed", number=stored.number, exc_info=True)


class _Shared(dict[str, Any]):
    """A replayed trial's distributions. Optuna deep-copies every trial added to a study, and a
    field slot's distribution holds thousands of choices: they never change, so the copy shares
    them."""

    def __deepcopy__(self, _memo: dict[int, Any]) -> dict[str, Any]:
        return dict(self)


def _finished(
    state: Any, params: dict[str, Any], distributions: dict[str, Any], values: list[float] | None
) -> optuna.trial.FrozenTrial:
    """A trial already told, to add to a study. ``create_trial`` makes the same one, but first
    compares each distribution with itself choice by choice: 9 ms a trial for a field slot."""
    from optuna.trial import FrozenTrial

    now = datetime.now()  # noqa: DTZ005 - naive, as Optuna dates its own trials
    return FrozenTrial(
        number=-1,
        state=state,
        value=None,
        values=values,
        datetime_start=now,
        datetime_complete=now,
        params=params,
        distributions=_Shared(distributions),
        user_attrs={},
        system_attrs={},
        intermediate_values={},
        trial_id=-1,
    )


class _Distributions:
    """Stored distributions back as Optuna's, each one made once and shared.

    A task's trials draw from a handful of distributions, so a copy per row is waste: a field
    slot stored as :data:`scheduler.FROM_SPACE` is rebuilt from the task's own space, and any
    other is decoded once. ``None`` for a row naming a slot the space does not have.
    """

    def __init__(self, slots: dict[str, tuple[str, ...]]) -> None:
        self._slots = slots
        self._made: dict[str, Any] = {}

    def load(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        from optuna.distributions import CategoricalDistribution, json_to_distribution

        out: dict[str, Any] = {}
        for name, value in raw.items():
            spaced = value == scheduler.FROM_SPACE
            if spaced and name not in self._slots:
                return None
            # A stored distribution is JSON text, so it cannot begin with "@".
            key = f"@{name}" if spaced else str(value)
            if key not in self._made:
                self._made[key] = (
                    CategoricalDistribution(self._slots[name])
                    if spaced
                    else json_to_distribution(value)
                )
            out[name] = self._made[key]
        return out


#: Past ``TRIM_AT`` trials a search keeps ``KEEP_BEST`` best, all of TPE's good group (Optuna's
#: default gamma stops at 25), and the ``KEEP_LATEST`` latest. Each trial more costs every
#: suggestion time, and memory per choice of every field slot; TPE weighs older ones down anyway.
KEEP_BEST = 25
KEEP_LATEST = 5_000
TRIM_AT = 6_000
#: The states TPE learns from, and counts toward its random start.
LEARNED = frozenset({"COMPLETE", "PRUNED"})
#: The study attribute counting the learned trials trims have dropped.
DROPPED = "dropped"


def kept(trials: Sequence[tuple[str, float | None]]) -> list[int]:
    """Which of a study's trials, given as (state, value told) in order, a trim keeps.

    The :data:`KEEP_LATEST` latest TPE learns from, and the :data:`KEEP_BEST` best Complete
    ranked as TPE ranks them: highest first, the earlier on a tie. A FAILURE is never a best,
    and a Failed trial teaches TPE nothing.
    """
    learned = [i for i, (state, _) in enumerate(trials) if state in LEARNED]
    scored = [
        (value, i)
        for i, (state, value) in enumerate(trials)
        if state == "COMPLETE" and value is not None and value > obj.FAILURE
    ]
    best = [i for _, i in sorted(scored, key=lambda pair: -pair[0])[:KEEP_BEST]]
    return sorted({*learned[-KEEP_LATEST:], *best})


def _outgrown(study: optuna.Study) -> bool:
    return len(study.get_trials(deepcopy=False)) > TRIM_AT


def _trimmed(study: optuna.Study, sampler: Callable[[int], Any]) -> optuna.Study:
    """``study`` cut to what :func:`kept` keeps. ``sampler(dropped)`` makes its sampler, given
    how many learned trials every trim so far has dropped."""
    import optuna

    trials = study.get_trials(deepcopy=False)
    keep = kept([(t.state.name, t.value) for t in trials])
    learned = sum(t.state.name in LEARNED for t in trials)
    dropped = int(study.user_attrs.get(DROPPED, 0)) + learned - len(keep)
    trimmed = optuna.create_study(direction="maximize", sampler=sampler(dropped))
    trimmed.set_user_attr(DROPPED, dropped)
    for i in keep:
        trial = trials[i]
        trimmed.add_trial(_finished(trial.state, trial.params, trial.distributions, trial.values))
    return trimmed


def _rebuild(
    sampler: Callable[[int], Any], history: list[dict[str, Any]], loader: _Distributions
) -> optuna.Study:
    """Recreate a study from its finished trials, trimmed as :func:`_trimmed` trims.

    Only terminal trials are replayed. Open ones are told later as replayed trials, so
    recreating them here would only reserve numbers the application does not use.
    """
    import optuna
    from optuna.trial import TrialState as OptunaState

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(direction="maximize", sampler=sampler(0))

    state_map = {
        "COMPLETE": OptunaState.COMPLETE,
        "FAIL": OptunaState.FAIL,
        "PRUNED": OptunaState.PRUNED,
    }

    for entry in history:
        state = state_map.get(str(entry["state"]))
        # A Full re-run of a Quick Alpha asked nothing of the search.
        if state is None or not entry["distributions"]:
            continue
        distributions = loader.load(entry["distributions"])
        if distributions is None:
            log.warning("optimize.replay_skipped", slots=sorted(entry["distributions"]))
            continue
        params = _optuna_params(entry["params"] or {}, distributions)
        values = (
            obj.searched(
                [float(v) for v in (entry["values"] or [])], entry["penalty"], entry["implausible"]
            )
            if state is OptunaState.COMPLETE
            else None
        )
        if state is OptunaState.COMPLETE and not values:
            continue
        try:
            study.add_trial(_finished(state, params, distributions, values))
        except ValueError:
            # One unreadable row costs the sampler a point of history; raising would fail
            # the whole task and leave its sent simulations unscored.
            log.warning("optimize.replay_skipped", params=sorted(params), exc_info=True)
    return _trimmed(study, sampler) if _outgrown(study) else study


def _optuna_params(params: dict[str, Any], distributions: dict[str, Any]) -> dict[str, Any]:
    """A stored trial's values under the names Optuna asked them by.

    The labs store what a point *means* (``field``), while Optuna names the slot it was
    drawn from (``field@TOP3000``: fields are scoped by universe). Replaying by the stored
    names drops every scoped value, and ``add_trial`` then refuses the row as
    inconsistent.
    """
    found: dict[str, Any] = {}
    for name in distributions:
        key = name if name in params else name.rsplit("@", 1)[0]
        if key in params:
            found[name] = params[key]
    return found


def passed_quick(result: dict[str, Any]) -> bool:
    """A Quick Alpha that passed every check BRAIN ran on it: owed a run in Full mode."""
    results = gating_results(result.get("checks") or [])
    return bool(result.get("quick")) and bool(results) and results <= {"PASS", "WARNING"}


def submittable(result: dict[str, Any]) -> bool:
    """Whether anything BRAIN has reported so far refuses this Alpha.

    Looser than a ``submittable`` verdict on purpose: an Alpha still being judged
    shows on the Tasks list, which fills in while BRAIN works. An Alpha with no gating checks
    is not submittable -- the usual reason is erroring out before BRAIN judged anything.
    """
    return not result.get("quick") and clean(result.get("checks") or [])


def still_judging(result: dict[str, Any]) -> bool:
    """Whether BRAIN has yet to finish checking an Alpha that nothing has refused.

    The Alphas :func:`submittable` shows that a strict ``verdict`` does not call submittable
    yet, so a green row on the Tasks screen can say it is still being judged.
    """
    mode = QUICK_MODE if result.get("quick") else None
    return verdict(result.get("checks") or [], mode) == "pending"


def ranked(
    trials: list[Trial], current: dict[str, list[dict[str, Any]]] | None = None
) -> list[dict[str, Any]]:
    """Finished trials, best first on the first objective, which is always maximised.

    ``current`` holds the checks BRAIN has since finished, by alpha id; a trial's own copy is
    frozen at simulation time and only stands in for an Alpha the vault does not hold.
    """
    done = [(t, t.values[0]) for t in trials if t.state == TrialState.COMPLETE and t.values]
    done.sort(key=lambda pair: float(pair[1]), reverse=True)
    # Once its Full run is back, that stands in for a Quick Alpha: the same figures, judged in
    # full. Keyed by task, because a trial's number is only its place in its own sweep.
    superseded = {
        (t.study_id, full_of)
        for t, _ in done
        if (full_of := (t.params or {}).get("fullOf")) is not None
    }
    rows = []
    for t, value in done:
        if (t.study_id, t.number) in superseded:
            continue
        result: dict[str, Any] = t.result or {}
        if current and t.alpha_id in current:
            checks = current[t.alpha_id]
            # The names follow the checks they come from, or a check BRAIN has since settled, or
            # a region-agnostic parent now judged on its children, keeps its old refusal.
            failed_now = [str(c.get("name")) for c in checks if c.get("result") == "FAIL"]
            result = {**result, "checks": checks, "failedChecks": failed_now}
        stats = result.get("stats") or {}
        rows.append(
            {
                "trialId": t.id,
                # Its place in the sweep, 0-based, as the task queued it.
                "number": t.number,
                "alphaId": t.alpha_id,
                "expression": t.expression,
                "settings": t.settings,
                "value": value,
                "sharpe": stats.get("sharpe"),
                "fitness": stats.get("fitness"),
                "turnover": stats.get("turnover"),
                "returns": stats.get("returns"),
                "drawdown": stats.get("drawdown"),
                "margin": stats.get("margin"),
                # A trial's own list is frozen at simulation time and predates the rule that
                # a quota check is not the Alpha's business, so it is filtered on the way out
                # as well as on the way in.
                "failedChecks": (failed := without_quota_checks_by_name(result)),
                # Of those, the ones that actually refuse the Alpha. A check in
                # ``IGNORED_CHECKS`` can FAIL without meaning anything about the Alpha --
                # ``REGULAR_SUBMISSION`` is your submission quota -- so ``verdict`` skips it
                # and anything counting refusals has to skip it too, or the two disagree.
                "refusedBy": [c for c in failed if str(c).upper() not in IGNORED_CHECKS],
                "submittable": submittable(result),
                "pending": still_judging(result),
                "quick": bool(result.get("quick")),
                "errored": "ERROR" in gating_results(result.get("checks") or []),
                "limits": limits_of(result.get("checks") or []),
            }
        )
    return rows


def without_quota_checks_by_name(result: dict[str, Any]) -> list[str]:
    """A result's failed checks, less the ones that describe the day's submission quota."""
    failed = result.get("failedChecks") or []
    return [c for c in failed if str(c).upper() not in QUOTA_CHECKS]


def vault_summary(saved: dict[str, Any]) -> dict[str, Any]:
    """The result row for an Alpha read from the local store instead of from BRAIN."""
    checks = checks_of(saved.get("checks"))
    failed = [c.get("name") for c in checks if c.get("result") == "FAIL"]
    return {
        "alphaId": saved.get("alpha_id"),
        "grade": saved.get("grade"),
        "stats": {key: saved.get(key) for key in sorted(VAULT_STATS)},
        "checks": checks,
        "failedChecks": failed,
        "penalty": obj.STRUCTURAL_PENALTY * len(obj.STRUCTURAL_CHECKS.intersection(failed)),
        "quick": saved.get("simulation_mode") == QUICK_MODE,
        "implausible": implausible(
            obj.search_sharpe(saved.get("train_sharpe"), saved.get("sharpe"))
        ),
    }


if __name__ == "__main__":
    import random
    import sys

    import optuna as _optuna
    from optuna.distributions import CategoricalDistribution, distribution_to_json
    from optuna.exceptions import ExperimentalWarning
    from optuna.samplers import TPESampler
    from optuna.trial import TrialState as OptunaState
    from optuna.trial import create_trial

    fields, shapes = tuple(f"f{n}" for n in range(300)), ("cs", "ts", "group")
    rng = random.Random(5)

    def history_of(n: int) -> list[dict[str, Any]]:
        return [
            {
                "params": {"field": rng.choice(fields), "family": rng.choice(shapes)},
                "distributions": {
                    "field@TOP3000": scheduler.FROM_SPACE,
                    "family": distribution_to_json(CategoricalDistribution(shapes)),
                },
                "values": [rng.gauss(0, 1)],
                "penalty": None,
                "implausible": None,
                "state": rng.choice(["COMPLETE", "COMPLETE", "COMPLETE", "FAIL", "PRUNED"]),
            }
            for _ in range(n)
        ]

    def seeded(dropped: int = 0) -> Any:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ExperimentalWarning)
            return TPESampler(
                n_startup_trials=max(0, 10 - dropped),
                multivariate=True,
                group=True,
                constant_liar=True,
                seed=3,
            )

    history = history_of(80)
    loader = _Distributions({"field@TOP3000": fields})
    ours = _rebuild(seeded, history, loader)
    # The same history through Optuna's own create_trial, as it was replayed before.
    theirs = _optuna.create_study(direction="maximize", sampler=seeded())
    for entry in history:
        made = loader.load(entry["distributions"]) or {}
        done = entry["state"] == "COMPLETE"
        theirs.add_trial(
            create_trial(
                params=_optuna_params(entry["params"], made),
                distributions=made,
                values=obj.searched(entry["values"], None) if done else None,
                state=OptunaState[entry["state"]],
            )
        )

    def held(study: optuna.Study) -> list[tuple[Any, ...]]:
        return [
            (t.number, t.state, t.values, t.params, t.distributions)
            for t in study.get_trials(deepcopy=False)
        ]

    def asked(study: optuna.Study) -> list[dict[str, Any]]:
        points: list[dict[str, Any]] = []
        for n in range(8):
            trial = study.ask()
            trial.suggest_categorical("field@TOP3000", fields)
            trial.suggest_categorical("family", shapes)
            points.append(trial.params)
            study.tell(trial, n / 10)
        return points

    # 25 old bests, the last tied by a later trial, then a FAILURE and a thousand worse; the
    # latest learned trials come last, with Failed ones between them that never count.
    told: list[tuple[str, float | None]] = [("COMPLETE", 50.0 - n) for n in range(KEEP_BEST)]
    told += [("COMPLETE", 50.0 - KEEP_BEST + 1), ("COMPLETE", obj.FAILURE), ("FAIL", None)]
    told += [("COMPLETE", -5.0)] * 1_000
    told += [("PRUNED", None), ("COMPLETE", 0.0), ("FAIL", None)] * (KEEP_LATEST // 2 + 1)
    latest = [i for i, (state, _) in enumerate(told) if state != "FAIL"][-KEEP_LATEST:]
    scarce = [("COMPLETE", obj.FAILURE), *[("PRUNED", None)] * KEEP_LATEST]

    big = history_of(TRIM_AT + 1)
    trimmed = _rebuild(seeded, big, loader)
    scores = [(e["state"], e["values"][0] if e["state"] == "COMPLETE" else None) for e in big]
    learned = sum(state in LEARNED for state, _ in scores)
    slot = {id(t.distributions["field@TOP3000"]) for t in ours.get_trials(deepcopy=False)}
    checks = {
        "replayed as Optuna's own create_trial replays": held(ours) == held(theirs),
        "the same points are asked next": asked(ours) == asked(theirs),
        "replayed trials share their field slot's distribution": len(slot) == 1,
        "a trim keeps the 25 best and the latest learned": kept(told)
        == sorted({*range(KEEP_BEST), *latest}),
        "a FAILURE is never a best": kept(scarce) == list(range(1, KEEP_LATEST + 1)),
        "a study of TRIM_AT trials is not trimmed": len(
            _rebuild(seeded, big[:TRIM_AT], loader).get_trials(deepcopy=False)
        )
        == TRIM_AT,
        "past it, the trials kept, in order": [
            (t.state.name, t.value, t.params["field@TOP3000"])
            for t in trimmed.get_trials(deepcopy=False)
        ]
        == [(*scores[i], big[i]["params"]["field"]) for i in kept(scores)],
        "what it dropped still counts toward the random start": trimmed.user_attrs[DROPPED]
        == learned - len(kept(scores)),
        "stored JSON replays as the columns read it": _history(
            [
                ('{"field": "f1"}', None, "[1.5]", 1.0, 0, "COMPLETE"),
                ("null", "{}", "null", None, None, "FAIL"),
            ]
        )
        == [
            {"params": {"field": "f1"}, "distributions": {}, "values": [1.5], "penalty": 1.0}
            | {"implausible": 0, "state": "COMPLETE"},
            {"params": {}, "distributions": {}, "values": None, "penalty": None}
            | {"implausible": None, "state": "FAIL"},
        ],
    }
    if failed := [name for name, ok in checks.items() if not ok]:
        raise SystemExit(f"study self-check failed: {'; '.join(failed)}")
    sys.stdout.write(f"{len(checks)} cases as expected\n")
