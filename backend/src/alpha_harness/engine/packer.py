"""Packing simulations into multi-simulation batches.

``POST /simulations`` accepts an array of 2-10 simulation objects, but every child of one
batch must agree on ``type``, ``instrumentType``, ``region``, ``delay`` and ``language``;
universe, neutralization, decay, truncation and expression may differ.

That constraint is the whole reason this module exists: throughput is not "80 at a time"
but "8 batches of up to 10 that happen to share a 5-tuple", so a sweep varying region or
delay fragments into small batches while one varying universe and expression packs
perfectly. Pure by design — no database, no HTTP.
"""

import itertools
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..brain.platform import GLB_REGION, GLB_SLOTS, INSTRUMENT_TYPE, MULTI_SIMULATION_SIZE, RA_SLOTS
from ..brain.schemas import FULL_MODE, SimulationType
from .reconcile import same_value, squash

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True, slots=True)
class BatchKey:
    """The fields every child of one batch must share: BRAIN's five, and the mode.

    BRAIN names only the five. The mode is kept apart too: a batch is back only when its
    slowest child is, so one Full child would hold nine Quick ones to Full time.
    """

    sim_type: str
    instrument_type: str
    region: str
    delay: int
    language: str
    mode: str = FULL_MODE

    def describe(self) -> str:
        return (
            f"{self.sim_type} {self.instrument_type} {self.region} "
            f"delay {self.delay} {self.language}"
            + (f" {self.mode.lower()}" if self.mode != FULL_MODE else "")
        )

    @property
    def region_agnostic(self) -> bool:
        return self.sim_type == SimulationType.REGION_AGNOSTIC

    @property
    def cost(self) -> int:
        """Concurrent cores one simulation with this key can occupy, at most.

        GLB counts two: BRAIN gives that region 2 of the 8 slots per simulation, so four
        run at once (BRAIN announcement, 2025-09-23). A batch is one simulation to BRAIN,
        so its children do not multiply this.

        A region-agnostic one costs the sum of its own children's quota, which varies with
        the regions its fields reach; :data:`RA_SLOTS` reserves the usual three, so two run
        at once.
        """
        if self.region_agnostic:
            return RA_SLOTS
        return GLB_SLOTS if self.region == GLB_REGION else 1

    @property
    def max_batch(self) -> int:
        """How many of these a multi-simulation may carry.

        Measured: BRAIN answers ``201`` to an array of region-agnostic simulations and then
        fails the parent and cancels every child, so they go one at a time.
        """
        # SUPER: BRAIN allows three concurrent SuperAlpha simulations, so each one stands alone
        # and the task's cores (at most three) are what bounds them.
        if self.region_agnostic or self.sim_type == SimulationType.SUPER:
            return 1
        return MULTI_SIMULATION_SIZE


@dataclass(frozen=True, slots=True)
class WorkItem:
    """One queued simulation, identified by its local record id."""

    record_id: int
    key: BatchKey
    task: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Batch:
    """A set of items that may legally be submitted together."""

    key: BatchKey
    task: str
    items: tuple[WorkItem, ...]

    @property
    def record_ids(self) -> list[int]:
        return [item.record_id for item in self.items]


def key_of(payload: dict[str, Any]) -> BatchKey:
    """Derive the batch key from a simulation request body."""
    settings = payload.get("settings") or {}
    return BatchKey(
        sim_type=str(payload.get("type", "REGULAR")),
        instrument_type=str(settings.get("instrumentType", INSTRUMENT_TYPE)),
        region=str(settings.get("region", "")),
        delay=int(settings.get("delay", 1)),
        language=str(settings.get("language", "FASTEXPR")),
        mode=str(settings.get("simulationMode") or FULL_MODE),
    )


def pack(
    items: Sequence[WorkItem],
    *,
    free_slots: int,
    max_batch: int = MULTI_SIMULATION_SIZE,
    task_capacity: dict[str, int] | None = None,
) -> list[Batch]:
    """Choose what to submit next.

    Groups items by batch key and fills up to ``free_slots`` *cores* with batches of at most
    ``max_batch`` each, fullest groups first so a round moves the most simulations it can. A
    core is one ordinary simulation; a GLB one costs two and a region-agnostic one three, and
    the latter never shares a batch (see :attr:`BatchKey.cost` and :attr:`BatchKey.max_batch`).

    ``task_capacity`` caps the cores each named task may take in this round; a task absent
    from the mapping is unconstrained. A batch belongs to the task that opened it,
    which holds its core. Seats it leaves empty go to other tasks' simulations that share its
    key, oldest first: they ride in a core already held, so no task holds more cores than it
    was given, and each keeps its own task, so its result is still scored where it belongs.
    """
    if free_slots <= 0 or not items:
        return []

    remaining = dict(task_capacity or {})

    # Group by (task, key): a batch must share the 5-tuple, and is opened by one task.
    groups: dict[tuple[str, BatchKey], list[WorkItem]] = defaultdict(list)
    for item in items:
        groups[(item.task, item.key)].append(item)

    # Slice each group into batch-sized chunks up front, so ordering can consider the
    # chunks themselves rather than the groups they came from.
    chunks: list[tuple[str, BatchKey, tuple[WorkItem, ...]]] = []
    for (task, key), members in groups.items():
        size = min(max_batch, key.max_batch)
        chunks.extend(
            (task, key, chunk) for chunk in itertools.batched(members, size, strict=False)
        )

    # Fullest chunks first; ties broken by the earliest queued item so a small group
    # cannot be starved indefinitely behind a steadily refilled large one.
    chunks.sort(key=lambda c: (-len(c[2]), c[2][0].record_id))

    opened: list[tuple[str, BatchKey, list[WorkItem]]] = []

    def seat(key: BatchKey, members: list[WorkItem]) -> list[WorkItem]:
        """Fit ``members`` into seats left in batches already opened; return the rest."""
        for _, open_key, seats in opened:
            if open_key == key and members:
                room = min(max_batch, key.max_batch) - len(seats)
                seats.extend(members[:room])
                members = members[room:]
        return members

    taken = 0
    for task, key, chunk in chunks:
        # A partial tail first fills another batch's empty seats: tails of 7 and 3 are one
        # core, not two.
        members = seat(key, list(chunk))
        if not members:
            continue
        # A region-agnostic batch that will not fit is skipped rather than ending the round:
        # an ordinary one behind it still fits in the core it would have needed.
        if taken + key.cost > free_slots:
            continue
        if task in remaining:
            if remaining[task] < key.cost:
                continue
            remaining[task] -= key.cost
        opened.append((task, key, members))
        taken += key.cost

    # Seats still empty take work no batch opened for: a task at its core limit, or one
    # whose chunk came up before the batch with room was opened.
    placed = {item.record_id for _, _, seats in opened for item in seats}
    for key in {key for _, key, _ in opened}:
        seat(key, [i for i in items if i.key == key and i.record_id not in placed])

    return [Batch(key=key, task=task, items=tuple(seats)) for task, key, seats in opened]


def allocate_slots(
    free_slots: int,
    *,
    demand: dict[str, int],
    quotas: dict[str, int],
    in_flight: dict[str, int] | None = None,
    lend: bool = False,
) -> dict[str, int]:
    """Divide free slots between tasks that want them.

    ``demand`` is how many cores each task's queued work could fill right now, ``quotas`` the
    configured ceiling on *concurrent* slots per task, and ``in_flight`` how many each
    already holds. A task with no quota is unconstrained.

    Allocation is round-robin rather than proportional, so a large sweep cannot take every
    slot while a single manual experiment waits behind it.

    With ``lend``, slots still free once every task has its own share go round-robin to
    tasks with more work than that, so a one-core task does not leave seven idle while
    nothing else wants them. A task at quota 0 is paused or finished and borrows nothing.
    Nothing is taken back mid-batch: a task that starts later waits for borrowed batches
    to come back, a few minutes at most.
    """
    if free_slots <= 0:
        return {}

    running = in_flight or {}
    headroom: dict[str, int] = {}
    for task, wanted in demand.items():
        if wanted <= 0:
            continue
        limit = quotas.get(task)
        available = wanted if limit is None else max(0, limit - running.get(task, 0))
        capped = min(wanted, available)
        if capped > 0:
            headroom[task] = capped

    allocation: dict[str, int] = defaultdict(int)
    granted = _round_robin(allocation, headroom, free_slots)
    if lend and granted < free_slots:
        beyond = {
            task: wanted - allocation[task]
            for task, wanted in demand.items()
            if quotas.get(task, 1) > 0 and wanted > allocation[task]
        }
        _round_robin(allocation, beyond, free_slots - granted)

    return {task: n for task, n in allocation.items() if n > 0}


def _round_robin(allocation: dict[str, int], headroom: dict[str, int], free: int) -> int:
    """Grant up to ``free`` slots, one per task per pass, within ``headroom``. Returns how many."""
    granted = 0
    # Deterministic order so the same queue always allocates the same way.
    tasks = sorted(headroom)
    while granted < free:
        progressed = False
        for task in tasks:
            if granted >= free:
                break
            if headroom[task] <= 0:
                continue
            allocation[task] += 1
            headroom[task] -= 1
            granted += 1
            progressed = True
        if not progressed:
            break
    return granted


def demand_by_task(items: Sequence[WorkItem], max_batch: int) -> dict[str, int]:
    """How many cores each task's queued work could fill right now."""
    counts: dict[tuple[str, BatchKey], int] = defaultdict(int)
    for item in items:
        counts[(item.task, item.key)] += 1

    demand: dict[str, int] = defaultdict(int)
    for (task, key), n in counts.items():
        size = min(max_batch, key.max_batch)
        demand[task] += -(-n // size) * key.cost  # ceiling division
    return dict(demand)


# -- reading a finished batch back --------------------------------------------


def match_children(
    payloads: dict[int, dict[str, Any]],
    children: Sequence[tuple[str, dict[str, Any]]],
    *,
    positional: bool = True,
) -> dict[int, dict[str, Any]]:
    """Attribute each child simulation BRAIN returned to the queued row that asked for it.

    ``payloads`` maps record id to the request it sent, in submission order; ``children``
    is ``(platform id, simulation body)`` per child read back. Rows are matched by the
    fields that may vary within a batch rather than by position, because those fields are
    exactly what identifies which request produced which alpha. Returns record id ->
    ``{"platform_id", "body"}``, plus ``"positional": True`` where the signature could not
    place a child and submission order was used instead, so a wrong attribution stays
    visible; ``positional=False`` disables that fallback, for a partial set of children.
    """
    resolved: dict[int, dict[str, Any]] = {}
    unmatched: list[tuple[str, dict[str, Any]]] = []
    for child_id, body in children:
        match = next(
            (
                record_id
                for record_id, payload in payloads.items()
                if record_id not in resolved and _asked_for(payload, body)
            ),
            None,
        )
        if match is not None:
            resolved[match] = {"platform_id": child_id, "body": body}
        else:
            unmatched.append((child_id, body))

    if not positional:
        return resolved
    leftovers = [record_id for record_id in payloads if record_id not in resolved]
    for record_id, (child_id, body) in zip(leftovers, unmatched, strict=False):
        resolved[record_id] = {"platform_id": child_id, "body": body, "positional": True}
    return resolved


def _asked_for(payload: dict[str, Any], body: dict[str, Any]) -> bool:
    """Whether a child simulation the platform returned is what ``payload`` requested.

    Every setting both carry must agree: children of one batch can differ in any of them,
    and two that differ only in an unchecked one would trade alphas. A setting the readback
    leaves out (``testPeriod``, say) cannot tell them apart and is not compared; expressions
    go through :func:`.reconcile.squash` because stored code comes back rewrapped.
    """

    def code(d: dict[str, Any]) -> str | None:
        return squash(d.get("regular") or d.get("combo") or d.get("selection"))

    if code(payload) != code(body):
        return False
    theirs = body.get("settings") or {}
    return all(
        same_value(value, theirs[key])
        for key, value in (payload.get("settings") or {}).items()
        if key in theirs
    )
