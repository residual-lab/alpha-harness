"""What the packer may send in a round, and how a finished batch is read back."""

from collections import Counter
from typing import Any

import numpy as np
from hypothesis import given
from hypothesis import strategies as st

from alpha_harness.brain.schemas import FULL_MODE, QUICK_MODE, SimulationType
from alpha_harness.engine.packer import (
    Batch,
    BatchKey,
    WorkItem,
    allocate_slots,
    demand_by_task,
    match_children,
    pack,
)

TASKS = ["search", "template", "super"]
tasks = st.sampled_from(TASKS)
keys = st.builds(
    BatchKey,
    sim_type=st.sampled_from(
        [SimulationType.REGULAR, SimulationType.SUPER, SimulationType.REGION_AGNOSTIC]
    ),
    instrument_type=st.just("EQUITY"),
    region=st.sampled_from(["USA", "GLB", "CHN"]),
    delay=st.sampled_from([0, 1]),
    language=st.just("FASTEXPR"),
    mode=st.sampled_from([FULL_MODE, QUICK_MODE]),
)
per_task = st.dictionaries(tasks, st.integers(0, 9))


@st.composite
def queues(draw: st.DrawFn) -> list[WorkItem]:
    """Queued work as the engine reads it, oldest first. Few keys, so tasks share them."""
    pool = draw(st.lists(keys, min_size=1, max_size=4, unique=True))
    picks = draw(st.lists(st.tuples(tasks, st.sampled_from(pool)), max_size=60))
    return [
        WorkItem(record_id=n, key=key, task=task, payload={})
        for n, (task, key) in enumerate(picks, 1)
    ]


def seats(key: BatchKey, max_batch: int) -> int:
    return min(max_batch, key.max_batch)


def cores_by_task(batches: list[Batch]) -> Counter[str]:
    held: Counter[str] = Counter()
    for batch in batches:
        held[batch.task] += batch.key.cost
    return held


@given(
    items=queues(),
    free_slots=st.integers(-1, 12),
    max_batch=st.integers(1, 10),
    task_capacity=st.none() | per_task,
)
def test_pack_sends_only_what_fits(
    items: list[WorkItem], free_slots: int, max_batch: int, task_capacity: dict[str, int] | None
) -> None:
    asked = None if task_capacity is None else dict(task_capacity)
    batches = pack(items, free_slots=free_slots, max_batch=max_batch, task_capacity=task_capacity)

    assert task_capacity == asked, "pack changed the capacities it was given"
    spent = sum(batch.key.cost for batch in batches)
    assert spent <= max(free_slots, 0)
    held = cores_by_task(batches)
    for task, cap in (task_capacity or {}).items():
        assert held[task] <= cap, f"{task} holds {held[task]} cores of {cap}"

    queued = {item.record_id: item for item in items}
    sent = [item for batch in batches for item in batch.items]
    assert len({item.record_id for item in sent}) == len(sent), "an item went twice"
    assert all(queued[item.record_id] is item for item in sent), "an item was invented"
    for batch in batches:
        assert all(item.key == batch.key for item in batch.items)
        assert 1 <= len(batch.items) <= seats(batch.key, max_batch)
        # The task that opened a batch holds its core, so it has a simulation in it.
        assert batch.items[0].task == batch.task

    # Nothing is left behind that could have gone: no seat open for it, no core to open one.
    sent_ids = {item.record_id for item in sent}
    for item in items:
        if item.record_id in sent_ids:
            continue
        assert all(
            len(batch.items) == seats(batch.key, max_batch)
            for batch in batches
            if batch.key == item.key
        ), f"{item.record_id} stayed queued beside an empty seat"
        cap = (task_capacity or {}).get(item.task)
        assert spent + item.key.cost > free_slots or (
            cap is not None and cap - held[item.task] < item.key.cost
        ), f"{item.record_id} stayed queued with a core free for it"


@given(items=queues(), max_batch=st.integers(1, 10))
def test_pack_sends_everything_when_every_core_is_free(
    items: list[WorkItem], max_batch: int
) -> None:
    demand = sum(demand_by_task(items, max_batch).values())
    batches = pack(items, free_slots=demand, max_batch=max_batch)
    sent = sorted(item.record_id for batch in batches for item in batch.items)
    assert sent == [item.record_id for item in items]
    spent = sum(batch.key.cost for batch in batches)
    # Riders fill other tasks' tails, so several tasks can cost less than their demand.
    assert spent == demand if len({item.task for item in items}) == 1 else spent <= demand


@given(
    free=st.integers(-1, 12),
    demand=st.dictionaries(tasks, st.integers(-1, 12)),
    quotas=per_task,
    in_flight=per_task,
    ceilings=st.none() | per_task,
)
def test_allocate_slots_is_fair_and_lends_only_spare_cores(
    free: int,
    demand: dict[str, int],
    quotas: dict[str, int],
    in_flight: dict[str, int],
    ceilings: dict[str, int] | None,
) -> None:
    inputs = repr((demand, quotas, in_flight, ceilings))
    own = allocate_slots(free, demand=demand, quotas=quotas, in_flight=in_flight)
    lent = allocate_slots(
        free, demand=demand, quotas=quotas, in_flight=in_flight, lend=True, ceilings=ceilings
    )
    assert repr((demand, quotas, in_flight, ceilings)) == inputs, "an input was changed"

    # What each task may hold of its own: its demand, within its quota when it has one.
    share: dict[str, int] = {}
    for task, wanted in demand.items():
        quota = quotas.get(task)
        share[task] = wanted if quota is None else min(wanted, quota - in_flight.get(task, 0))
    assert sum(own.values()) == min(max(free, 0), sum(max(0, n) for n in share.values()))
    for task, granted in own.items():
        assert 0 < granted <= share[task]
    # Round-robin: a task left short is at most one core behind any other.
    for task, wanted in share.items():
        if own.get(task, 0) < wanted:
            assert all(n <= own.get(task, 0) + 1 for n in own.values())

    assert sum(lent.values()) <= max(free, 0)
    for task in demand:
        granted, extra = lent.get(task, 0), lent.get(task, 0) - own.get(task, 0)
        assert extra >= 0, "lending took a task's own cores away"
        assert granted <= max(0, demand[task])
        if quotas.get(task) == 0:
            assert granted == 0, "a paused task borrowed cores"
        if extra and ceilings and task in ceilings:
            assert granted <= ceilings[task] - in_flight.get(task, 0)


@given(items=queues(), free=st.integers(0, 8), quotas=per_task, in_flight=per_task)
def test_a_round_never_takes_a_task_past_its_quota(
    items: list[WorkItem], free: int, quotas: dict[str, int], in_flight: dict[str, int]
) -> None:
    """The round as the engine runs it: demand, then allocation, then packing within it."""
    demand = demand_by_task(items, 10)
    allocation = allocate_slots(free, demand=demand, quotas=quotas, in_flight=in_flight)
    batches = pack(
        items,
        free_slots=free,
        max_batch=10,
        task_capacity={task: allocation.get(task, 0) for task in demand},
    )
    held = cores_by_task(batches)
    assert sum(held.values()) <= free
    for task, quota in quotas.items():
        assert held[task] <= max(0, quota - in_flight.get(task, 0))


codes = st.sampled_from(["rank(close)", "ts_mean(close, 5)", "-rank(volume)"])
settings_sent = st.fixed_dictionaries(
    {
        "universe": st.sampled_from(["TOP3000", "TOP1000"]),
        "decay": st.integers(0, 2),
        "truncation": st.sampled_from([0.05, 0.08]),
        "neutralization": st.sampled_from(["MARKET", "SUBINDUSTRY"]),
    }
)
# Template Lab pins both Investability settings; the other labs leave both to BRAIN.
investability = st.none() | st.fixed_dictionaries(
    {"maxTrade": st.sampled_from(["ON", "OFF"]), "maxPosition": st.sampled_from(["ON", "OFF"])}
)
BRAIN_DEFAULTS = {"maxTrade": "OFF", "maxPosition": "OFF", "testPeriod": "P0Y0M0D"}


def echo(payload: dict[str, Any]) -> dict[str, Any]:
    """A child as BRAIN reads it back: code rewrapped, numbers in single precision, and
    every setting the request left out filled in."""
    settings = {
        key: float(np.float32(value)) if isinstance(value, float) else value
        for key, value in payload["settings"].items()
    }
    return {
        "regular": {"code": payload["regular"].replace(" ", "  ") + "\n"},
        "settings": {**BRAIN_DEFAULTS, **settings},
    }


def simulated(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """What BRAIN actually runs for a request: its code, under its settings or BRAIN's."""
    return payload["regular"], {**BRAIN_DEFAULTS, **payload["settings"]}


@st.composite
def readbacks(draw: st.DrawFn) -> tuple[dict[int, dict[str, Any]], list[tuple[str, Any]]]:
    """A batch's requests by record id, and its children in whatever order they come back."""
    sent = draw(st.lists(st.tuples(codes, settings_sent, investability), min_size=1, max_size=10))
    payloads = {
        n: {"type": "REGULAR", "regular": c, "settings": {**s, **(pinned or {})}}
        for n, (c, s, pinned) in enumerate(sent)
    }
    return payloads, draw(st.permutations([(f"child-{n}", echo(p)) for n, p in payloads.items()]))


@given(batch=readbacks(), data=st.data())
def test_match_children_gives_each_request_its_own_child(
    batch: tuple[dict[int, dict[str, Any]], list[tuple[str, Any]]], data: st.DataObject
) -> None:
    payloads, children = batch
    read = data.draw(st.integers(0, len(children)), label="children read so far")
    for positional, given_children in ((True, children), (False, children[:read])):
        resolved = match_children(payloads, given_children, positional=positional)
        assert len(resolved) == len(given_children), "a child was left unattributed"
        assert len({found["platform_id"] for found in resolved.values()}) == len(resolved)
        for record_id, found in resolved.items():
            assert "positional" not in found
            # Requests BRAIN runs identically may trade children; any two others must not.
            source = int(found["platform_id"].removeprefix("child-"))
            assert simulated(payloads[source]) == simulated(payloads[record_id])
