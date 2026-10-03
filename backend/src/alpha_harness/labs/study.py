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
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import func, select

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
from ..vault.yields import IGNORED_CHECKS, QUOTA_CHECKS, checks_of, clean, gating_results, verdict
from . import objectives as obj
from . import scheduler
from .objectives import StudyNotFoundError
from .params import SEARCH_SAMPLER, TASK_SAMPLERS, TEMPLATE_SAMPLER, SearchParams, params_of

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Awaitable, Callable

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

#: Statistics the local Alpha store keeps, so objectives on them need no second BRAIN read.
VAULT_STATS = frozenset(
    {"sharpe", "fitness", "turnover", "returns", "drawdown", "margin"}
    | {"train_sharpe", "train_fitness"}
)
#: How long a finished Alpha may take to reach the local store before BRAIN is asked.
VAULT_WAIT = timedelta(minutes=5)


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
        self._task: asyncio.Task[None] | None = None

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="optimizer")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        log.info("optimize.stopped")

    async def _run(self) -> None:
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
        results: dict[int, dict[str, int]] = {}
        for study_id in ids:
            try:
                results[study_id] = await self.advance(study_id)
            except Exception as exc:
                # Paused, never failed: whatever went wrong, the task picks up where it left off
                # once resumed, and a fresh clone is always there.
                log.exception("optimize.study_failed", study_id=study_id)
                await scheduler.finish(
                    self,
                    study_id,
                    StudyStatus.PAUSED,
                    f"Paused after an error: {exc}. Resume to carry on, or clone it fresh.",
                )
        return results

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
            row = await self.get(study_id)
            if row is None:
                return {"told": 0, "asked": 0}
            if row.sampler not in TASK_SAMPLERS:
                raise ValueError(f"No lab runs studies with the {row.sampler!r} sampler.")

            # A cancel in the Simulation Matrix takes a batch back, not the task.
            await scheduler.requeue_cancelled(self, row)
            # Evolution Lab breeds its own children: there is no sampler to tell.
            told = await self._harvest(
                study_id, tell=row.sampler in (SEARCH_SAMPLER, TEMPLATE_SAMPLER)
            )
            asked = await scheduler.advance(self, study_id)
        if told or asked:
            await self.notify()
        return {"told": told, "asked": asked}

    async def _harvest(self, study_id: int, *, tell: bool) -> int:
        """Score every trial whose simulation reached a terminal state.

        ``tell=False`` stores the scores without reporting them to Optuna, for GA studies.
        The Alpha the tracker already stored locally is read first instead of asking BRAIN
        for it again, which saves one request per simulation.
        """
        async with self.db.session() as session:
            row = await session.get(Study, study_id)
            if row is None:
                return 0
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
            # A lookup that fails for any reason fails that trial, not the whole study.
            except Exception as exc:  # noqa: BLE001
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
        if not finished:
            return 0

        if tell:
            study = await self.optuna_study(study_id, row)
            # Only a trial asked before a restart is replayed, and only it needs what it was
            # asked over; the column is deferred, so it is read for those alone.
            replayed = [t.id for asked, t, _, _ in finished if asked is None]
            asked_over = await self._distributions(replayed) if replayed else {}
            await asyncio.to_thread(_tell_many, study, finished, asked_over)

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
            for _optuna_trial, trial, values, summary in finished:
                live.pop(trial.number, None)
                stored = await session.get(Trial, trial.id)
                if stored is None:
                    continue
                stored.finished_at = utcnow()
                if values is None:
                    stored.state = TrialState.FAIL
                    stored.message = summary.get("error")
                else:
                    stored.state = TrialState.COMPLETE
                    stored.values = values
                    stored.constraint = summary.get("constraint")
                    stored.feasible = summary.get("feasible")
                    stored.result = summary
                    stored.alpha_id = summary.get("alphaId")
            await session.commit()

        log.info("optimize.told", study_id=study_id, trials=len(finished))
        return len(finished)

    async def _distributions(self, trial_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Each trial's stored distributions, by trial id."""
        async with self.db.session() as session:
            rows = await session.execute(
                select(Trial.id, Trial.distributions).where(Trial.id.in_(trial_ids))
            )
            return {trial_id: raw or {} for trial_id, raw in rows.tuples()}

    async def _mark_running(self, trial_ids: list[int]) -> None:
        async with self.db.session() as session:
            for trial_id in trial_ids:
                stored = await session.get(Trial, trial_id)
                if stored is not None:
                    stored.state = TrialState.RUNNING
            await session.commit()

    # -- optuna ----------------------------------------------------------

    async def optuna_study(self, study_id: int, row: Study) -> optuna.Study:
        """The Optuna study, rebuilt from the trial rows on first use."""
        cached = self.studies.get(study_id)
        if cached is not None:
            return cached

        async with self.db.session() as session:
            history = [
                {
                    "params": params or {},
                    "distributions": distributions or {},
                    "values": values,
                    "state": state,
                    "constraint": constraint or {},
                }
                for params, distributions, values, state, constraint in (
                    await session.execute(
                        select(
                            Trial.params,
                            Trial.distributions,
                            Trial.values,
                            Trial.state,
                            Trial.constraint,
                        )
                        .where(Trial.study_id == study_id)
                        .order_by(Trial.number)
                    )
                ).tuples()
            ]

        from optuna.samplers import TPESampler

        sampler = TPESampler(
            n_startup_trials=params_of(row, SearchParams).n_startup_trials,
            multivariate=True,
            group=True,
            constant_liar=row.batch_size > 1,
        )
        study = await asyncio.to_thread(_rebuild, sampler, history)
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


def _tell_many(
    study: optuna.Study,
    finished: list[tuple[Any, Any, list[float] | None, dict[str, Any]]],
    asked_over: dict[int, dict[str, Any]],
) -> None:
    """Report results.

    Two routes, because a trial asked before a restart no longer has a live object: a live
    one is told directly, and one without is added as an already-finished trial so the same
    information reaches the sampler without relying on Optuna's numbering matching ours.
    """
    from optuna.trial import TrialState as OptunaState
    from optuna.trial import create_trial

    for optuna_trial, stored, values, summary in finished:
        constraint = {str(k): float(v) for k, v in (summary.get("constraint") or {}).items()}
        try:
            if optuna_trial is not None:
                if values is None:
                    study.tell(optuna_trial, state=OptunaState.FAIL, skip_if_finished=True)
                else:
                    for key, violation in constraint.items():
                        optuna_trial.set_constraint(key, violation)
                    study.tell(optuna_trial, values, skip_if_finished=True)
                continue

            raw = asked_over.get(stored.id) or {}
            if values is None or not raw:
                # A failed replayed trial teaches the sampler nothing, and nor does a Full
                # re-run: the search never asked for it.
                continue
            distributions = _load_distributions(raw)
            study.add_trial(
                create_trial(
                    params=_optuna_params(stored.params or {}, distributions),
                    distributions=distributions,
                    values=[float(v) for v in values],
                    state=OptunaState.COMPLETE,
                    constraints=constraint or None,
                )
            )
        except Exception:
            log.warning("optimize.tell_failed", number=stored.number, exc_info=True)


def _load_distributions(raw: dict[str, Any]) -> dict[str, Any]:
    from optuna.distributions import json_to_distribution

    return {name: json_to_distribution(value) for name, value in raw.items()}


def _rebuild(sampler: Any, history: list[dict[str, Any]]) -> optuna.Study:
    """Recreate a study from its finished trials.

    Only terminal trials are replayed. Open ones are told later as replayed trials, so
    recreating them here would only reserve numbers the application does not use.
    """
    import optuna
    from optuna.trial import TrialState as OptunaState
    from optuna.trial import create_trial

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(direction="maximize", sampler=sampler)

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
        distributions = _load_distributions(entry["distributions"] or {})
        params = _optuna_params(entry["params"] or {}, distributions)
        values = (
            [float(v) for v in (entry["values"] or [])] if state is OptunaState.COMPLETE else None
        )
        if state is OptunaState.COMPLETE and not values:
            continue
        constraints = {str(k): float(v) for k, v in (entry["constraint"] or {}).items()}
        try:
            study.add_trial(
                create_trial(
                    params=params,
                    distributions=distributions,
                    values=values,
                    state=state,
                    constraints=constraints or None,
                )
            )
        except ValueError:
            # One unreadable row costs the sampler a point of history; raising would fail
            # the whole task and leave its sent simulations unscored.
            log.warning("optimize.replay_skipped", params=sorted(params), exc_info=True)
    return study


def _optuna_params(params: dict[str, Any], distributions: dict[str, Any]) -> dict[str, Any]:
    """A stored trial's values under the names Optuna asked them by.

    The labs store what a point *means* (``field``), while Optuna names the slot it was
    drawn from (``field@TOP3000``: fields are scoped by universe). Replaying by the stored
    names drops every scoped value, and ``create_trial`` then refuses the row as
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

    Exactly the Alphas :func:`submittable` shows and the Submission Planner does not, so a green
    row on the Tasks screen never promises a candidate the Planner will then refuse.
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
            result = {**result, "checks": current[t.alpha_id]}
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
                "feasible": t.feasible,
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
                "source": bool((t.params or {}).get("source")),
                "quick": bool(result.get("quick")),
                "errored": "ERROR" in gating_results(result.get("checks") or []),
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
        "feasible": not failed if checks else None,
        "failedChecks": failed,
        "quick": saved.get("simulation_mode") == QUICK_MODE,
    }
