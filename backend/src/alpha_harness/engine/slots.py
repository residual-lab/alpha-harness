"""The batch engine: queue, pack, submit, expand.

Throughput on BRAIN is eight *concurrent simulations*, each of which may be a
multi-simulation carrying up to ten children — so eighty at once, but only when the work
packs, since children of one batch must share a 5-tuple (see :mod:`.packer`).

This module owns the queue, the slot accounting, and reading a batch's children to
completion; :mod:`.packer` decides what to send, :mod:`.lifecycle` owns the atomic write
of a platform id, and :class:`~alpha_harness.engine.tracker.SimulationTracker` polls the
parent until it lists its children.
"""

import asyncio
import contextlib
import dataclasses
import itertools
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import ValidationError
from sqlalchemy import delete, func, select, update

from ..brain.errors import (
    BrainAuthError,
    BrainDailyLimitReached,
    BrainError,
    BrainForbidden,
    BrainValidationError,
)
from ..brain.filters import platform_midnight
from ..brain.platform import MULTI_SIMULATION_SIZE, SIMULATION_SLOTS
from ..brain.schemas import QUICK_MODE, SimulationRequest
from ..db.models import ACTIVE, DedupEntry, SimStatus, SimulationRecord, Study, TaskQuota, utcnow
from ..tasks import spawn
from .awake import StayAwake
from .lifecycle import (
    SLOT_COST,
    Outcome,
    SubmissionFailed,
    cancel_after_lost_race,
    describe_fields,
    extract_simulation_id,
    finish,
    hash_payload,
    new_record,
    record_launch,
    request_hash,
    transition,
)
from .packer import Batch, WorkItem, allocate_slots, demand_by_task, key_of, match_children, pack

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ..brain.endpoints import BrainEndpoints
    from ..db.sqlite import Database
    from .tracker import SimulationTracker

log = structlog.get_logger(__name__)

#: Added to a batched simulation's outcome when BRAIN named no child, so it was matched by order.
POSITIONAL_NOTE = "Matched to this request by submission order."

#: How often the engine looks for work.
TICK_SECONDS = 2.0

#: Rounds between sleep-block checks (~30 s): pending work changes slowly.
AWAKE_TICKS = 15

#: A wait of TICK_SECONDS that took this long means the computer slept through it.
SLEEP_GAP = 60.0

#: The task work carries when nothing owns it — a one-off run from the UI rather than a
#: lab task. It has no study by design, so it is the one task exempt from the orphan sweep.
MANUAL_TASK = "manual"

#: Hashes per duplicate lookup in :meth:`BatchEngine.enqueue`.
ENQUEUE_CHUNK = 500

#: Recent completions the time-left estimate is measured over.
RATE_WINDOW = timedelta(minutes=15)

#: How often queued work with no task left is swept up.
ORPHAN_SWEEP_SECONDS = 60.0

#: Cancellations in flight at once when a task is forced to stop.
ABANDON_AT_ONCE = 8


class BatchEngine:
    """Keeps the concurrent slots full from a local queue."""

    def __init__(
        self,
        db: Database,
        endpoints: BrainEndpoints,
        tracker: SimulationTracker,
        *,
        on_unauthorized: Callable[[], Awaitable[bool]],
    ) -> None:
        self.db = db
        self.endpoints = endpoints
        self.tracker = tracker
        self.slots = SIMULATION_SLOTS
        #: Asked while BRAIN refuses the session; True once simulations may be sent again.
        self._on_unauthorized = on_unauthorized
        #: Set when BRAIN answers 401. Sending on stops until the session is usable: every
        #: send would fail the same way, and work must never be rejected for it.
        self._session_lost = False
        self._task: asyncio.Task[None] | None = None
        #: Lend cores no task holds to tasks with more work than their own (Settings).
        self.lend_idle_cores = False
        #: Ask the OS not to sleep while work is pending (Settings).
        self.keep_awake = True
        #: Regions whose simulations run in BRAIN's Quick mode unless one names a mode
        #: (Settings), and whether the account may run it at all.
        self.quick_regions: frozenset[str] = frozenset()
        self.quick_allowed = False
        #: Set when the daily cap is hit. Nothing is submitted until it clears, because
        #: retrying before the US-Eastern reset cannot succeed.
        self._daily_limit_hit = False
        #: Wall-clock time of BRAIN's quota reset; the flag clears once it passes. Not
        #: monotonic: that clock stops while the computer sleeps, and BRAIN's day does not.
        self._limit_resets_at: float | None = None
        self._enqueue_lock = asyncio.Lock()
        #: Reads finished batches back beside the scheduling round; at most one at a time.
        self._expansion: asyncio.Task[None] | None = None
        #: Child platform id -> (body, outcome) of a finished child not yet written to its
        #: row, because its batch still has children running.
        self._child_results: dict[str, tuple[dict[str, Any], Outcome]] = {}
        #: Batch size, reduced to 1 for accounts without MULTI_SIMULATION.
        self.max_batch = MULTI_SIMULATION_SIZE
        #: Set once BRAIN refuses a batch with 403; lasts for the process.
        self._batch_refused = False
        #: Blocks OS sleep while work is pending; checked every AWAKE_TICKS rounds.
        self._awake = StayAwake()
        #: Wall-clock start and end of the last sleep the engine noticed, for the Matrix.
        self._last_pause: tuple[datetime, datetime] | None = None
        #: When queued work with no task was last swept up. Zero so the first tick sweeps.
        self._last_orphan_sweep = 0.0

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="batch-engine")
        log.info("engine.started", slots=self.slots, max_batch=self.max_batch)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._awake.release()
        log.info("engine.stopped")

    def set_keep_awake(self, on: bool) -> None:
        """Turned off, the machine may sleep at once rather than at the next check."""
        self.keep_awake = on
        if not on:
            self._awake.release()

    async def idle(self) -> bool:
        """Nothing out on BRAIN and nothing about to be sent: safe to restart the app."""
        return not await self._busy()

    def configure_from_permissions(self, permissions: list[str]) -> None:
        """Batching needs MULTI_SIMULATION, and Quick mode QUICK_MODE; without the first every
        batch is a single run.

        A 403 on a batch outranks the permission list: a re-login must not resume the
        batches BRAIN already refused.
        """
        allowed = "MULTI_SIMULATION" in permissions and not self._batch_refused
        self.max_batch = MULTI_SIMULATION_SIZE if allowed else 1
        self.quick_allowed = "QUICK_MODE" in permissions

    def _moded(self, request: SimulationRequest) -> SimulationRequest:
        """``request`` in Quick mode where Settings ask for it and it names no mode itself."""
        settings = request.settings
        if (
            settings.simulation_mode is not None
            or not self.quick_allowed
            or settings.region not in self.quick_regions
        ):
            return request
        quick = settings.model_copy(update={"simulation_mode": QUICK_MODE, "visualization": False})
        return request.model_copy(update={"settings": quick})

    # -- queueing --------------------------------------------------------

    async def enqueue(
        self,
        requests: list[SimulationRequest],
        *,
        task: str,
    ) -> dict[str, Any]:
        """Accept work. Returns what was queued and what was skipped as a duplicate.

        Duplicates are checked before anything is sent: re-running an identical alpha
        spends daily quota on an alpha that already exists. ``outcomes`` carries one entry
        per request, in the order given, so a caller can tell which of *its* items became
        which row — the optimizer scores a trial matched to an existing alpha immediately.
        """
        queued: list[int] = []
        skipped: list[dict[str, Any]] = []
        outcomes: list[dict[str, Any]] = []
        moded = [self._moded(request) for request in requests]

        # The duplicate lookup and the insert must not interleave with another enqueue:
        # two callers would each find no active row and both queue, spending quota twice.
        async with self._enqueue_lock, self.db.session() as session:
            hashes = [request_hash(request) for request in moded]
            # A request made Quick is answered as well by a Full Alpha of the same point, which
            # is worth more: it can be submitted. Never the other way round.
            fulls = [
                request_hash(asked) if sent is not asked else None
                for sent, asked in zip(moded, requests, strict=True)
            ]
            known: dict[str, str] = {}
            #: Hash -> an active row's id, or the row this call is adding for it.
            in_flight: dict[str, int | SimulationRecord] = {}
            # Looked up in bulk — a harvest enqueues thousands, and two reads per request
            # would scale queueing with round-trips — and chunked to stay under SQLite's
            # variable cap.
            looked_up = set(hashes) | {full for full in fulls if full}
            for chunk in itertools.batched(looked_up, ENQUEUE_CHUNK, strict=False):
                for entry in await session.scalars(
                    select(DedupEntry).where(DedupEntry.request_hash.in_(chunk))
                ):
                    known[entry.request_hash] = entry.alpha_id
                for digest, record_id in (
                    await session.execute(
                        select(SimulationRecord.request_hash, SimulationRecord.id).where(
                            SimulationRecord.request_hash.in_(chunk),
                            SimulationRecord.status.in_(ACTIVE),
                        )
                    )
                ).tuples():
                    in_flight.setdefault(digest, record_id)

            # (index, hash, status, alpha id, the row: new, or an existing row's id)
            plan: list[tuple[int, str, SimStatus, str | None, int | SimulationRecord]] = []
            for index, (request, digest, full) in enumerate(zip(moded, hashes, fulls, strict=True)):
                if full in known or digest in known:
                    alpha_id = known[full] if full in known else known[digest]
                    record = new_record(
                        request,
                        task=task,
                        status=SimStatus.SKIPPED,
                        alpha_id=alpha_id,
                        message="Identical alpha already simulated; reused the existing result.",
                        finished_at=utcnow(),
                    )
                    session.add(record)
                    plan.append((index, digest, SimStatus.SKIPPED, alpha_id, record))
                elif digest in in_flight:
                    # Same payload still queued or running (possibly from earlier in this
                    # call): share that row, so its result reaches both callers for one run.
                    plan.append((index, digest, SimStatus.SKIPPED, None, in_flight[digest]))
                else:
                    record = new_record(request, task=task, status=SimStatus.QUEUED)
                    session.add(record)
                    in_flight[digest] = record
                    plan.append((index, digest, SimStatus.QUEUED, None, record))
            await session.flush()

            for index, digest, status, alpha_id, row in plan:
                record_id = row if isinstance(row, int) else row.id
                if status == SimStatus.QUEUED:
                    queued.append(record_id)
                else:
                    skipped.append({"alphaId": alpha_id, "hash": digest})
                outcomes.append(
                    {
                        "index": index,
                        "recordId": record_id,
                        "status": str(status),
                        "alphaId": alpha_id,
                        "hash": digest,
                    }
                )

        log.info("engine.enqueued", task=task, queued=len(queued), skipped=len(skipped))
        await self.tracker.notify()
        return {"queued": queued, "skipped": skipped, "outcomes": outcomes}

    async def drop_queued(self, task: str | None = None) -> int:
        """Discard queued work that has not been sent anywhere yet.

        Safe by construction: a QUEUED row has no platform id because nothing was ever
        submitted for it.
        """
        async with self.db.session() as session:
            statement = (
                update(SimulationRecord)
                .where(SimulationRecord.status == SimStatus.QUEUED)
                .values(
                    status=SimStatus.CANCELLED,
                    finished_at=utcnow(),
                    message="Removed from the queue before it was submitted.",
                )
            )
            if task is not None:
                statement = statement.where(SimulationRecord.task == task)
            result = await session.execute(statement)
            dropped = result.rowcount or 0  # pyright: ignore[reportAttributeAccessIssue]  # DML returns a CursorResult; stubs say Result

        if dropped:
            log.info("engine.queue_dropped", task=task, count=dropped)
            await self.tracker.notify()
        return dropped

    async def disown_orphans(self) -> int:
        """Cancel queued work whose task no longer exists. Returns how many.

        Queued rows outlive the task that made them — a study deleted, a crash between
        queueing and recording it — and nothing downstream notices. Dispatch selects on
        status alone, and :func:`allocate_slots` treats a task with no quota row as
        *unconstrained*, so orphaned work is sent as fast as the slots allow, spends the
        day's quota, and produces Alphas no task will ever score. On screen it looks like
        the app simulating on its own: an empty Tasks table, no cores assigned, and the
        day's allowance draining anyway.

        Cancelled rather than skipped, because a row left QUEUED is invisible in a
        different way — it would sit in the backlog for good and the count would never
        explain itself.
        """
        async with self.db.session() as session:
            result = await session.execute(
                update(SimulationRecord)
                .where(
                    SimulationRecord.status == SimStatus.QUEUED,
                    # The documented task for work nobody owns; it has no study by design.
                    SimulationRecord.task != MANUAL_TASK,
                    SimulationRecord.task.not_in(select(Study.task)),
                )
                .values(
                    status=SimStatus.CANCELLED,
                    finished_at=utcnow(),
                    message="The task that queued this no longer exists, so it was not sent.",
                )
            )
            dropped = result.rowcount or 0  # pyright: ignore[reportAttributeAccessIssue]
        if dropped:
            log.warning("engine.orphans_disowned", count=dropped)
            await self.tracker.notify()
        return dropped

    async def abandon(self, task: str) -> int:
        """Cancel everything ``task`` still has out on BRAIN. Returns how many were asked.

        Except a batch it opened that also carries another task's simulations: cancelling
        it would take theirs with it, so it is left to finish.

        Best effort on purpose. A simulation BRAIN has already finished refuses to cancel,
        and marking it cancelled here would hide an alpha that exists — so the refusal is
        left to the next poll, which records what really happened. The *task* ends either
        way: a task is a local scheduling object, and a simulation that outlives it still
        lands in the vault with the quota it already spent.
        """
        async with self.db.session() as session:
            rows = list(
                (
                    await session.scalars(
                        select(SimulationRecord.id).where(
                            SimulationRecord.task == task,
                            SimulationRecord.status.in_(
                                [SimStatus.PENDING, SimStatus.RUNNING, SimStatus.ORPHANED]
                            ),
                        )
                    )
                ).all()
            )
            shared = set(
                (
                    await session.scalars(
                        select(SimulationRecord.parent_record_id).where(
                            SimulationRecord.parent_record_id.in_(rows),
                            SimulationRecord.task != task,
                        )
                    )
                ).all()
            )
        rows = [r for r in rows if r not in shared]
        # Together rather than one after another: this runs while the task's lock is held,
        # and a task with fifty simulations out would otherwise hold it for the sum of fifty
        # round trips. Bounded, because BRAIN meters this endpoint like any other.
        gate = asyncio.Semaphore(ABANDON_AT_ONCE)

        async def stop(record_id: int) -> None:
            async with gate:
                await self.tracker.cancel(record_id)

        outcomes = await asyncio.gather(*(stop(r) for r in rows), return_exceptions=True)
        for record_id, outcome in zip(rows, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                # One refusal must not strand the rest; the poll records what really happened.
                log.warning("engine.cancel_failed", record_id=record_id, error=str(outcome))
        if rows:
            log.info("engine.task_abandoned", task=task, count=len(rows))
            await self.tracker.notify()
        return len(rows)

    # -- quotas ----------------------------------------------------------

    async def set_quota(self, name: str, max_slots: int, *, enabled: bool = True) -> None:
        async with self.db.session() as session:
            row = await session.get(TaskQuota, name)
            if row is None:
                session.add(TaskQuota(name=name, max_slots=max_slots, enabled=enabled))
            else:
                row.max_slots = max_slots
                row.enabled = enabled

    async def quotas(self) -> dict[str, int]:
        async with self.db.session() as session:
            rows = await session.scalars(select(TaskQuota))
            return {r.name: (r.max_slots if r.enabled else 0) for r in rows}

    async def status(self) -> dict[str, Any]:
        """Everything the matrix header needs in one call."""
        async with self.db.session() as session:
            in_flight = await self._in_flight_by_task(session)
            queued_rows = await session.execute(
                select(SimulationRecord.task, func.count())
                .where(SimulationRecord.status == SimStatus.QUEUED)
                .group_by(SimulationRecord.task)
            )
            queued = dict(queued_rows.tuples().all())
            recent = await session.scalar(
                select(func.count()).where(
                    SimulationRecord.status == SimStatus.COMPLETE,
                    SimulationRecord.alpha_id.is_not(None),
                    SimulationRecord.finished_at >= utcnow() - RATE_WINDOW,
                )
            )

        used = sum(in_flight.values())
        per_minute = (recent or 0) / (RATE_WINDOW.total_seconds() / 60)
        waiting = sum(queued.values())
        return {
            "slots": self.slots,
            "maxBatch": self.max_batch,
            "slotsUsed": used,
            "slotsFree": max(0, self.slots - used),
            "queued": queued,
            "queuedTotal": sum(queued.values()),
            "inFlight": in_flight,
            "quotas": await self.quotas(),
            "dailyLimitHit": self._daily_limit_hit,
            "sessionLost": self._session_lost,
            "awake": self._awake.state,
            "quickAllowed": self.quick_allowed,
            "lastPause": (
                {"start": self._last_pause[0], "end": self._last_pause[1]}
                if self._last_pause
                else None
            ),
            # Straight-line rate over the last 15 minutes; ignores quota and batch timing.
            "minutesLeft": round(waiting / per_minute) if waiting and per_minute else None,
        }

    # -- the loop --------------------------------------------------------

    async def _run(self) -> None:
        rounds = 0
        while True:
            try:
                await self.tick()
                if rounds % AWAKE_TICKS == 0:
                    self._awake.hold(self.keep_awake and await self._busy())
                rounds += 1
            except Exception:
                log.exception("engine.tick_failed")
            # Only the wait is timed: a slow round is not a sleep, a two-second nap that took
            # a minute is.
            before = time.time()
            await asyncio.sleep(TICK_SECONDS)
            if time.time() - before > SLEEP_GAP:
                self._last_pause = (datetime.fromtimestamp(before, UTC), datetime.now(UTC))
                log.warning("engine.slept", seconds=round(time.time() - before))

    async def tick(self) -> int:
        """One scheduling round. Returns how many batches were submitted.

        Finished batches are read back in a task of their own: a read-back costs a request
        per child, and waiting for it would leave slots idle that the next round would have
        filled. It touches only a finished parent's children, never the QUEUED rows a round
        claims, and every write is a compare-and-set. A failed read-back is retried on a
        later round.
        """
        submitted = await self._fill_slots()
        if self._expansion is None or self._expansion.done():
            self._expansion = spawn(self._expand_finished_batches(), name="batch-expand")
        return submitted

    async def _fill_slots(self) -> int:
        reset = self._limit_resets_at
        if self._daily_limit_hit and reset is not None and time.time() >= reset:
            # BRAIN's day has turned: the quota is back.
            self._daily_limit_hit = False
            self._limit_resets_at = None
        if self._daily_limit_hit:
            return 0
        if self._session_lost:
            if not await self._on_unauthorized():
                return 0
            self._session_lost = False
            log.info("engine.session_back")

        # Before anything is chosen to send. A task can be orphaned mid-session, and what it
        # left behind is exactly the work that would be sent hardest — nothing caps it. Not
        # every tick, though: it is a write transaction, and orphaning is rare enough that a
        # minute of a stray task sending is the cost of not writing every two seconds.
        now = time.monotonic()
        if now - self._last_orphan_sweep >= ORPHAN_SWEEP_SECONDS:
            self._last_orphan_sweep = now
            await self.disown_orphans()

        async with self.db.session() as session:
            in_flight = await self._in_flight_by_task(session)
            free = self.slots - sum(in_flight.values())
            if free <= 0:
                return 0

            # Only the head of each task's queue: no round can send more than
            # ``free * max_batch`` rows for one task, and loading the whole backlog every
            # tick would make scheduling cost grow with the queue.
            window = free * self.max_batch
            position = (
                select(
                    SimulationRecord.id,
                    func.row_number()
                    .over(partition_by=SimulationRecord.task, order_by=SimulationRecord.id)
                    .label("position"),
                )
                .where(SimulationRecord.status == SimStatus.QUEUED)
                .subquery()
            )
            queued = (
                await session.scalars(
                    select(SimulationRecord)
                    .join(position, position.c.id == SimulationRecord.id)
                    .where(position.c.position <= window)
                    .order_by(SimulationRecord.id)
                )
            ).all()
            if not queued:
                return 0

            items = [
                WorkItem(
                    record_id=r.id,
                    key=key_of(r.payload or {}),
                    task=r.task,
                    payload=r.payload or {},
                )
                for r in queued
            ]

        quotas = await self.quotas()
        demand = demand_by_task(items, self.max_batch)
        allocation = allocate_slots(
            free, demand=demand, quotas=quotas, in_flight=in_flight, lend=self.lend_idle_cores
        )
        if not allocation:
            return 0

        batches = pack(
            items,
            free_slots=free,
            max_batch=self.max_batch,
            # Every task with work gets a cap: ``pack`` treats one it is not told about as
            # unlimited, and a task granted nothing is exactly one at its quota.
            task_capacity={task: allocation.get(task, 0) for task in demand},
        )

        submitted = 0
        for batch in batches:
            if self._daily_limit_hit or self._session_lost:
                break
            if await self._submit(batch):
                submitted += 1

        if submitted:
            await self.tracker.notify()
        return submitted

    # -- submission ------------------------------------------------------

    async def _submit(self, batch: Batch) -> bool:
        """Send one batch, keeping the id-before-request discipline.

        A single-item batch reuses the tracker's ordinary path so there is exactly one
        implementation of "record the id atomically". A real batch creates a parent row
        first — the parent is the only cancellable handle.
        """
        items = await self._reject_invalid(batch.items)
        if not items:
            return False
        if len(items) == 1:
            return await self._submit_single(items[0])
        return await self._submit_batch(dataclasses.replace(batch, items=tuple(items)))

    async def _reject_invalid(self, items: tuple[WorkItem, ...]) -> list[WorkItem]:
        """Reject rows whose stored payload no longer validates, and return the rest.

        Such a row can never be sent; left queued, its error would escape every round and
        every batch sorted after it would wait behind it forever.
        """
        valid: list[WorkItem] = []
        for item in items:
            try:
                SimulationRequest.model_validate(item.payload)
            except ValidationError as exc:
                async with self.db.session() as session:
                    await transition(
                        session,
                        item.record_id,
                        from_=[SimStatus.QUEUED],
                        status=SimStatus.REJECTED,
                        finished_at=utcnow(),
                        message=f"The stored request is no longer valid: {exc}"[:500],
                    )
                log.warning("engine.invalid_payload", record_id=item.record_id)
                continue
            valid.append(item)
        return valid

    async def _submit_single(self, item: WorkItem) -> bool:
        request = SimulationRequest.model_validate(item.payload)
        try:
            await self.tracker.submit(request, item.record_id)
            return True
        except SubmissionFailed as exc:
            # The tracker wraps platform errors, so the daily cap arrives as a cause
            # rather than as itself. Missing it here would march the whole queue into
            # a limit that cannot be satisfied until tomorrow.
            if isinstance(exc.cause, BrainDailyLimitReached):
                await self._on_daily_limit()
            elif isinstance(exc.cause, BrainAuthError):
                self._on_session_lost()
            else:
                log.warning("engine.single_failed", record_id=item.record_id, error=str(exc))
            return False
        # The dispatch loop must outlive any single submission; the failure is logged.
        except Exception as exc:  # noqa: BLE001
            log.warning("engine.single_failed", record_id=item.record_id, error=str(exc))
            return False

    async def _submit_batch(self, batch: Batch) -> bool:
        first = batch.items[0].payload
        settings = first.get("settings") or {}

        # 1. Parent row, committed before anything leaves the process. Children are
        # claimed only if still queued: one dropped since the queue was read stays dropped.
        async with self.db.session() as session:
            parent = SimulationRecord(
                request_hash="",
                payload={},
                expression="",
                sim_type=batch.key.sim_type,
                instrument_type=batch.key.instrument_type,
                region=batch.key.region,
                delay=batch.key.delay,
                language=batch.key.language,
                universe=settings.get("universe"),
                task=batch.task,
                status=SimStatus.PENDING,
                is_batch=True,
                sent_at=utcnow(),
            )
            session.add(parent)
            await session.flush()
            parent_id = parent.id

            claimed = set(
                await transition(
                    session,
                    batch.record_ids,
                    from_=[SimStatus.QUEUED],
                    status=SimStatus.PENDING,
                    parent_record_id=parent_id,
                    sent_at=parent.sent_at,
                )
            )
            items = [i for i in batch.items if i.record_id in claimed]
            record_ids = [i.record_id for i in items]
            parent.request_hash = hash_payload([i.payload for i in items])
            parent.payload = {"children": [i.payload for i in items]}
            parent.expression = f"{len(items)} simulations"
            if len(items) < 2:
                # A multi-simulation needs two children. Give any survivor back to the
                # queue, where the next round packs it afresh; the parent was never sent.
                await transition(
                    session,
                    record_ids,
                    from_=[SimStatus.PENDING],
                    status=SimStatus.QUEUED,
                    parent_record_id=None,
                )
                await session.delete(parent)
                return False

        # 2. Send it.
        requests = [SimulationRequest.model_validate(i.payload) for i in items]
        self.tracker.sending.add(parent_id)
        try:
            response = await self.endpoints.create_simulation(requests)
        except BrainDailyLimitReached:
            # Back to the queue: the work is still wanted, and runs after the reset.
            await self._fail_batch(
                parent_id, record_ids, "Daily simulation limit reached.", requeue=True
            )
            await self._on_daily_limit()
            return False
        except BrainAuthError as exc:
            # A session problem says nothing about the alphas: back to the queue.
            await self._fail_batch(parent_id, record_ids, exc.message, requeue=True)
            self._on_session_lost()
            return False
        except BrainValidationError as exc:
            await self._reject_batch(parent_id, record_ids, exc)
            return False
        except BrainForbidden as exc:
            # 403 on POST /simulations: "Can't run multi-simulations" (04-error-handling.md).
            # The work is fine; send it one at a time from now on.
            self.max_batch = 1
            self._batch_refused = True
            await self._fail_batch(parent_id, record_ids, exc.message, requeue=True)
            return False
        except BrainError as exc:
            if exc.maybe_delivered:
                # The batch may be running on BRAIN with its id lost in transit. Its children
                # are matched to their alphas individually, or queued again once none come.
                await self._fail_batch(
                    parent_id,
                    record_ids,
                    f"BRAIN may have started this batch but its answer was lost "
                    f"({exc.message}). Matching its simulations against your alphas.",
                    requeue=False,
                    status=SimStatus.ORPHANED,
                )
            else:
                await self._fail_batch(parent_id, record_ids, exc.message, requeue=exc.retryable)
            return False
        else:
            # 3. Record the parent id immediately — it is the only way to cancel the batch —
            # and before leaving ``sending``, so a stale-send sweep cannot orphan it meanwhile.
            platform_id = extract_simulation_id(response.location)
            if platform_id is None:
                await self._fail_batch(
                    parent_id,
                    record_ids,
                    "BRAIN accepted the batch but returned no id, so it cannot be cancelled "
                    "from here. Check the platform.",
                    requeue=False,
                    status=SimStatus.ORPHANED,
                )
                return False

            async with self.db.session() as session:
                won = await record_launch(
                    session, parent_id, platform_id, response.rate_limit, children=record_ids
                )
        finally:
            self.tracker.sending.discard(parent_id)

        if not won:
            if not await cancel_after_lost_race(
                self.db, self.endpoints, parent_id, platform_id, children=record_ids
            ):
                log.warning("engine.batch_cancel_refused", parent=parent_id)
                self.tracker.watch(platform_id)
                return True
            log.info("engine.batch_cancelled_after_send", parent=parent_id)
            return False

        self.tracker.watch(platform_id, response.retry_after)
        log.info(
            "engine.batch_running",
            parent=parent_id,
            platform_id=platform_id,
            size=len(items),
            task=batch.task,
            key=batch.key.describe(),
        )
        return True

    async def _reject_batch(
        self, parent_id: int, record_ids: list[int], exc: BrainValidationError
    ) -> None:
        """Reject only the children BRAIN objected to; the rest go back in the queue.

        A multi-simulation's ``400`` body is an array lined up with the request array, empty
        where that child was fine. Rejecting all ten for one bad expression would drop nine
        good alphas; any other body shape falls back to rejecting the whole batch.
        """
        entries = exc.body if isinstance(exc.body, list) else None
        # All entries empty names no culprit: resending it would be refused the same way.
        if entries is None or len(entries) != len(record_ids) or not any(entries):
            reason = describe_fields(exc.fields, exc.message)
            await self._fail_batch(
                parent_id, record_ids, f"BRAIN rejected these settings — {reason}", requeue=False
            )
            return
        fine = [rid for rid, entry in zip(record_ids, entries, strict=True) if not entry]
        # Requeue first: that deletes the never-run parent, so the rejections below only
        # close their own rows instead of leaving a REJECTED batch that ran nothing.
        await self._fail_batch(parent_id, fine, exc.message, requeue=True)
        for rid, entry in zip(record_ids, entries, strict=True):
            if entry:
                reason = describe_fields(entry, exc.message)
                await self._fail_batch(
                    parent_id, [rid], f"BRAIN rejected these settings — {reason}", requeue=False
                )

    async def _fail_batch(
        self,
        parent_id: int,
        record_ids: list[int],
        message: str,
        *,
        requeue: bool,
        status: SimStatus = SimStatus.REJECTED,
    ) -> None:
        """Mark a batch that never started.

        Retryable failures put the children back in the queue; a rejection that will
        never succeed marks them so, rather than looping forever on the same payload.
        """
        async with self.db.session() as session:
            parent = await session.get(SimulationRecord, parent_id)
            if parent is not None and parent.status == SimStatus.CANCELLED:
                # Cancelled while the POST was out: this work must never be queued again.
                requeue, status, message = False, SimStatus.CANCELLED, parent.message or message
            child_status = SimStatus.QUEUED if requeue else status
            values: dict[str, Any] = {"status": child_status, "parent_record_id": None}
            if not requeue:
                values["message"] = message
                values["finished_at"] = utcnow()
            await transition(session, record_ids, from_=[SimStatus.PENDING], **values)
            if requeue:
                # Its work is waiting again and the batch never ran: a REJECTED parent
                # would only report a failure that did not happen to any alpha.
                await session.execute(
                    delete(SimulationRecord).where(
                        SimulationRecord.id == parent_id,
                        SimulationRecord.status == SimStatus.PENDING,
                    )
                )
            else:
                await transition(
                    session,
                    parent_id,
                    from_=[SimStatus.PENDING],
                    status=status,
                    message=message,
                    finished_at=utcnow(),
                    # Nothing ran, so there is nothing to expand.
                    children_expanded=True,
                )
        log.warning("engine.batch_failed", parent=parent_id, requeued=requeue, message=message)
        await self.tracker.notify()

    def _on_session_lost(self) -> None:
        if not self._session_lost:
            log.warning("engine.session_lost")
        self._session_lost = True

    async def _on_daily_limit(self) -> None:
        self._daily_limit_hit = True
        self._limit_resets_at = time.time() + await self._seconds_to_reset()
        log.warning("engine.daily_limit_reached")
        await self.tracker.notify()

    async def _seconds_to_reset(self) -> float:
        """Until BRAIN's quota resets, by its own ``X-Ratelimit-Reset`` when that is current.

        A reading whose reset has already passed is from an earlier day, so the next platform
        midnight stands in: every recorded reset has landed exactly there.
        """
        now = datetime.now(UTC)
        latest = await self.tracker.latest_quota()
        if latest is not None and latest.reset_seconds is not None:
            reset = latest.observed_at + timedelta(seconds=latest.reset_seconds)
            if reset > now:
                return (reset - now).total_seconds()
        # In UTC: subtracting two New York times ignores a DST change between them.
        midnight = platform_midnight() + timedelta(days=1)
        return (midnight.astimezone(UTC) - now).total_seconds()

    # -- child expansion -------------------------------------------------

    async def _expand_finished_batches(self) -> None:
        """Resolve the children of every batch that has dispatched them.

        Two kinds of parent qualify: one still RUNNING whose read listed its children, and
        one that ended — failed and cancelled included, or their children would stay RUNNING
        forever. Selected from disk, so a restart picks up where the last process stopped.
        """
        async with self.db.session() as session:
            candidates = (
                await session.scalars(
                    select(SimulationRecord).where(
                        SimulationRecord.is_batch.is_(True),
                        SimulationRecord.status.in_(
                            [
                                SimStatus.RUNNING,
                                SimStatus.COMPLETE,
                                SimStatus.WARNING,
                                SimStatus.ERROR,
                                SimStatus.FAILED,
                                SimStatus.TIMEOUT,
                                SimStatus.CANCELLED,
                            ]
                        ),
                        SimulationRecord.children_expanded.is_(False),
                    )
                )
            ).all()
        parents = [p for p in candidates if p.status != SimStatus.RUNNING or p.child_ids]

        # Every parent each round: a child read is due only when its Retry-After has run
        # out, so a pass over batches still simulating costs almost nothing, and taking one
        # parent per round would leave finished alphas waiting behind running batches.
        for parent in parents:
            await self._expand(parent)

    async def _expand(self, parent: SimulationRecord) -> None:
        child_ids: list[str] = list(parent.child_ids or [])
        async with self.db.session() as session:
            records = list(
                await session.scalars(
                    select(SimulationRecord)
                    .where(SimulationRecord.parent_record_id == parent.id)
                    .order_by(SimulationRecord.id)
                )
            )

        if not child_ids:
            # The platform reported no children. Say so on the rows rather than
            # leaving them stuck as RUNNING forever.
            ended_badly = parent.status not in (SimStatus.COMPLETE, SimStatus.WARNING)
            await self._finish_children(
                records,
                {},
                {},
                fallback_message=(
                    parent.message or f"The batch ended {parent.status} before it ran."
                )
                if ended_badly
                else "The batch finished but BRAIN listed no child simulations.",
                child_status=SimStatus.CANCELLED
                if parent.status == SimStatus.CANCELLED
                else SimStatus.ERROR,
            )
            await self._mark_expanded(parent.id)
            return

        # A child counts as attributed once a finished row carries its id.
        attributed = {
            r.platform_id for r in records if r.platform_id and SimStatus(r.status).terminal
        }
        open_rows = [r for r in records if not SimStatus(r.status).terminal]
        if await self._read_children([c for c in child_ids if c not in attributed]):
            return  # The session is gone; every read would fail the same way.

        bodies = [
            (c, self._child_results[c][0])
            for c in child_ids
            if c in self._child_results and c not in attributed
        ]
        outcomes = {c: self._child_results[c][1] for c in child_ids if c in self._child_results}
        settled = all(c in attributed or c in outcomes for c in child_ids)
        # Submission order is trusted only once every child is in: a partial set lined up
        # by position would hand one request's alpha to another.
        resolved = match_children(
            {r.id: r.payload or {} for r in open_rows}, bodies, positional=settled
        )
        if not settled:
            if resolved:
                await self._finish_children(
                    [r for r in open_rows if r.id in resolved], resolved, outcomes
                )
                await self.tracker.notify()
            return

        await self._finish_children(open_rows, resolved, outcomes)
        async with self.db.session() as session:
            # Its children are all in, so it no longer holds a slot on BRAIN either. A
            # cancel that already ended it wins this compare-and-set.
            await transition(
                session,
                parent.id,
                from_=[SimStatus.RUNNING],
                status=SimStatus.COMPLETE,
                finished_at=utcnow(),
            )
        await self._mark_expanded(parent.id)
        for child_id in child_ids:
            self._child_results.pop(child_id, None)
        log.info(
            "engine.batch_expanded",
            parent=parent.id,
            children=len(child_ids),
            matched=len(resolved),
        )
        await self.tracker.notify()

    async def _read_children(self, child_ids: list[str]) -> bool:
        """Read each unfinished child that is due. Returns True if the session was refused.

        Read through the tracker, so a child keeps the same schedule and lost-simulation rule
        as a simulation sent alone: one BRAIN has lost ends as an error rather than holding
        its batch's core for good. Finished outcomes are kept in memory until their batch is
        expanded, so a child that cannot be attributed yet is not read again. A restart loses
        them, which costs one more read each — nothing is decided from memory alone.
        """
        for child_id in child_ids:
            if child_id in self._child_results:
                continue
            read = await self.tracker.read(child_id)
            if read is None:
                continue
            outcome, body = read
            match outcome.kind:
                case "unauthorized":
                    self._on_session_lost()
                    return True
                case "pending" | "retry":
                    # Still simulating, or the read was refused for now: either way the alpha is
                    # coming, and closing the row now would throw it away.
                    continue
                case "fanout":
                    self._child_results[child_id] = (
                        body,
                        Outcome(
                            "finished",
                            SimStatus.ERROR,
                            message="BRAIN reported children under a child simulation.",
                        ),
                    )
                case "gone" | "finished":
                    self._child_results[child_id] = (body, outcome)
        return False

    async def _finish_children(
        self,
        records: list[SimulationRecord],
        resolved: dict[int, dict[str, Any]],
        outcomes: dict[str, Outcome],
        *,
        fallback_message: str | None = None,
        child_status: SimStatus = SimStatus.ERROR,
    ) -> None:
        landed: list[str] = []
        async with self.db.session() as session:
            for record in records:
                found = resolved.get(record.id)
                if found is None:
                    message = (
                        fallback_message or "BRAIN did not report a result for this simulation."
                    )
                    await finish(
                        session, record, Outcome("finished", child_status, message=message)
                    )
                    continue

                outcome = outcomes[found["platform_id"]]
                if found.get("positional"):
                    joined = f"{outcome.message or ''} {POSITIONAL_NOTE}".strip()
                    outcome = dataclasses.replace(outcome, message=joined)
                if alpha_id := await finish(
                    session, record, outcome, platform_id=found["platform_id"]
                ):
                    landed.append(alpha_id)

        # The tracker polls only the parent, which carries no alpha of its own. Without
        # this hand-off no batched alpha would reach the vault, so nothing batched could
        # ever be judged submittable.
        for alpha_id in landed:
            self.tracker.alpha_landed(alpha_id)

    async def _mark_expanded(self, parent_id: int) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(SimulationRecord)
                .where(SimulationRecord.id == parent_id)
                .values(children_expanded=True)
            )

    # -- helpers ---------------------------------------------------------

    async def _busy(self) -> bool:
        """Work that still needs this machine awake: running, or queued and sendable."""
        async with self.db.session() as session:
            if await self._in_flight_by_task(session):
                return True
            if self._daily_limit_hit or self._session_lost:
                return False
            queued = await session.scalar(
                select(
                    select(SimulationRecord.id)
                    .where(SimulationRecord.status == SimStatus.QUEUED)
                    .exists()
                )
            )
            return bool(queued)

    async def _in_flight_by_task(self, session: Any) -> dict[str, int]:
        """Cores currently held, counted per task.

        Only parents and standalone simulations count — a batch's children ride in its
        single slot and must not be double-counted. A region-agnostic simulation holds four,
        one per region it is translated into, and a GLB one holds two: BRAIN meters that
        region at double rate, so only four GLB simulations run at once.
        """
        result = await session.execute(
            select(SimulationRecord.task, func.sum(SLOT_COST))
            .where(
                SimulationRecord.status.in_([SimStatus.PENDING, SimStatus.RUNNING]),
                SimulationRecord.parent_record_id.is_(None),
            )
            .group_by(SimulationRecord.task)
        )
        return {task: int(cost or 0) for task, cost in result.all()}
