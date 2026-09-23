"""From a lab's form to a task in Tasks: what the account and market allow, and the Study row.

Every lab router asks the same questions — which operators the account has, which universes
and neutralizations BRAIN allows, whether the market is downloaded — so the answers live
here and the four labs cannot drift apart on them.

Nothing here raises HTTP errors: a problem is returned as a sentence, and the router
decides whether it blocks.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError

from ..brain.errors import BrainError
from ..brain.schemas import region_label
from ..brain.settings_schema import resolve_options
from ..db.models import Study, StudyStatus
from ..schemas import Out
from . import scheduler, search

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from ..brain.schemas import SimulationRequest
    from ..db.models import Trial
    from .params import TaskParams

OPERATORS_UNREAD = "Your BRAIN operators could not be read. Sign in again, then reload."


# --- wire shapes every lab's responses share ------------------------------------------


class OperatorsRead(Out):
    synced: bool
    count: int


class FieldCounts(Out):
    total: int
    matrix: int
    vector: int


class LeftOut(Out):
    vector: int


class SampleAlpha(Out):
    expression: str
    settings: dict[str, Any]


class AddedTask(Out):
    id: int
    name: str


def operators_read(operators: list[dict[str, Any]]) -> OperatorsRead:
    return OperatorsRead(synced=bool(operators), count=len(operators))


def field_counts(fields: dict[str, str]) -> FieldCounts:
    matrix = sum(1 for t in fields.values() if t == "MATRIX")
    return FieldCounts(total=len(fields), matrix=matrix, vector=len(fields) - matrix)


# --- what the account and the market allow --------------------------------------------


async def account_operators(state: Any, *, refresh: bool) -> list[dict[str, Any]]:
    """The account's own operators, fetched from BRAIN when missing or asked for."""
    cached = await state.metadata.cached_operators()
    if refresh or not cached:
        try:
            return await state.metadata.refresh_operators()
        except BrainError, ValidationError:
            return cached or []
    return cached


def legal_choices(schema: dict[str, Any] | None, region: str, delay: int) -> dict[str, Any]:
    """BRAIN's legal settings for an equity market; empty until the schema is loaded."""
    market = {"instrumentType": "EQUITY", "region": region, "delay": delay}
    return resolve_options(schema, market) if schema else {}


def choices(legal: dict[str, Any], name: str) -> list[Any]:
    return [c.get("value") for c in (legal.get(name, {}).get("choices") or [])]


async def neutralizations_for(
    state: Any, region: str, delay: int, chosen: Sequence[str] = ()
) -> list[str]:
    """The neutralizations a lab searches in a market, in BRAIN's own order.

    Chosen ones are honoured as given, narrowed only by what BRAIN accepts in that market. The
    lab's own default list is four group neutralizations, and quietly intersecting a choice
    with it would drop ``STATISTICAL`` or ``CROWDING`` from a sweep that asked for them —
    a task that never ran what it was told to.

    Choosing nothing keeps the default, so every lab behaves exactly as before until a reader
    says otherwise.
    """
    schema = await state.metadata.cached_settings_schema()
    if not schema:
        return []
    offered = choices(legal_choices(schema, region, delay), "neutralization")
    if chosen and (picked := [n for n in offered if n in set(chosen)]):
        return picked
    return [n for n in search.NEUTRALIZATIONS if n in offered] or offered[:1]


async def synced_universes(
    state: Any, legal: dict[str, Any], region: str, delay: int, first: str | None
) -> list[str]:
    """The market's legal universes that are downloaded, the chosen one first."""
    synced = {
        str(r["universe"])
        for r in await state.queries.synced_tuples()
        if r["instrument_type"] == "EQUITY" and r["region"] == region and int(r["delay"]) == delay
    }
    universes = [u for u in choices(legal, "universe") if u in synced]
    if first in universes:
        universes.remove(first)
        universes.insert(0, first)
    return universes


class SearchRequest(BaseModel):
    region: str
    delay: int = Field(ge=0, le=1)
    #: The chosen market's universe; searched first, other universes join if they overlap.
    universe: str | None = None
    dataset_ids: list[str] = Field(default_factory=list, max_length=200)
    vector_operators: list[str] = Field(default_factory=list)
    #: Empty keeps the lab's default four; anything here is searched instead.
    neutralizations: list[str] = Field(default_factory=list, max_length=20)
    decay: int = 0
    cores: int = Field(default=search.MAX_CORES, ge=1, le=search.MAX_CORES)
    #: Needed to add a task; a preview ignores it.
    simulations: int = Field(default=0, ge=0, le=search.MAX_SIMULATIONS)


async def market_for(body: SearchRequest, state: Any, need: tuple[str, ...] = ()) -> dict[str, Any]:
    """The market a task searches, checked: universes, fields, neutralizations, vector operators.

    ``need`` names the fixed fields a template reads; a universe that does not have one is
    left out, with a warning rather than in silence.
    """
    problems: list[str] = []
    warnings: list[str] = []

    operators = await account_operators(state, refresh=False)
    if not operators:
        problems.append(OPERATORS_UNREAD)
    elif "ts_backfill" not in {o.get("name") for o in operators}:
        problems.append("ts_backfill is not available on this account, so fields can't be cleaned.")
    if body.decay not in search.DECAYS:
        problems.append(f"Decay must be one of {', '.join(map(str, search.DECAYS))}.")

    schema = await state.metadata.cached_settings_schema()
    if not schema:
        problems.append("BRAIN's settings list is not loaded. Sign in again.")
    legal = legal_choices(schema, body.region, body.delay)
    universes = await synced_universes(state, legal, body.region, body.delay, body.universe)
    downloaded = bool(universes)
    neutralizations = await neutralizations_for(
        state, body.region, body.delay, body.neutralizations
    )
    if schema and not neutralizations:
        problems.append(f"BRAIN offers no neutralization for {region_label(body.region)}.")

    lacking: set[str] = set()
    if need and universes:
        held: dict[str, set[str]] = {}
        for name in need:
            held[name] = {
                str(r["universe"])
                for r in await state.queries.field_availability(name)
                if r["region"] == body.region and int(r["delay"]) == body.delay
            }
        for universe in list(universes):
            short = [name for name in need if universe not in held[name]]
            if short:
                universes.remove(universe)
                lacking.update(short)
                verb = "is" if len(short) == 1 else "are"
                warnings.append(f"{', '.join(short)} {verb} not in {universe}, so it is left out.")

    vector_ops = [
        v for v in dict.fromkeys(body.vector_operators) if v in search.catalogue(operators).vector
    ]
    pool = search.Pool({}, (), {}, 0)
    if not body.dataset_ids:
        problems.append("Choose at least one dataset.")
    elif not downloaded:
        problems.append(
            f"No {region_label(body.region)} delay {body.delay} market is downloaded. "
            "Sync it in the Data Explorer."
        )
    elif not universes:
        names = ", ".join(sorted(lacking) or need)
        problems.append(
            f"No downloaded {region_label(body.region)} delay {body.delay} universe has {names}."
        )
    else:
        pool = await search.field_pool(
            state.queries,
            region=body.region,
            delay=body.delay,
            universes=universes,
            dataset_ids=body.dataset_ids,
            allow_vector=bool(vector_ops),
        )
        if not pool.fields:
            problems.append(
                "The chosen datasets have no usable fields in this market."
                + (
                    " Allow a vector operator to use their vector fields."
                    if pool.vector_skipped
                    else ""
                )
            )
    if pool.vector_skipped and pool.fields:
        warnings.append(
            f"{pool.vector_skipped:,} vector fields are left out; "
            "allow a vector operator to use them."
        )
    return {
        "operators": operators,
        "pool": pool,
        "neutralizations": neutralizations,
        "vector": vector_ops,
        "problems": problems,
        "warnings": warnings,
    }


# --- a preview, and the task itself ---------------------------------------------------


def preview_samples(
    draw: Callable[[random.Random], SimulationRequest | None], *, tries: int, want: int = 5
) -> list[SampleAlpha]:
    """Up to ``want`` Alphas a task could send, from at most ``tries`` draws."""
    rng = random.Random()
    sample: list[SampleAlpha] = []
    for _ in range(tries):
        if len(sample) >= want:
            break
        request = draw(rng)
        if request is not None:
            settings = request.settings.model_dump(by_alias=True, exclude_none=True)
            sample.append(SampleAlpha(expression=request.regular or "", settings=settings))
    return sample


def startup_trials(fields: int, size: int, per_round: int) -> int:
    """Random draws before the search steers: one per field, at most half the task."""
    cover = min(fields, size // 2)
    return max(per_round, -(-cover // per_round) * per_round)


async def add_study(
    state: Any,
    *,
    now: datetime,
    lab: str,
    prefix: str,
    sampler: str,
    params: TaskParams,
    objective: str,
    simulations: int,
    batch_size: int,
    template_source: str,
    template_name: str | None = None,
    template_id: int | None = None,
    run: bool = False,
    seeds: Callable[[int], list[Trial]] | None = None,
) -> Study:
    """Store a task: not started, or queued for the scheduler when ``run``.

    ``seeds`` are trials the task starts with, written in the same transaction.
    """
    task = f"{prefix}-{now:%y%m%d%H%M%S%f}"
    row = Study(
        name=f"{lab} · {task}",
        template_id=template_id,
        template_name=template_name or lab,
        template_source=template_source,
        sampler=sampler,
        sampler_params=params.dump(),
        objectives=[objective],
        directions=["maximize"],
        batch_size=batch_size,
        max_trials=simulations,
        task=task,
        status=StudyStatus.QUEUED if run else StudyStatus.IDLE,
        sampler_notes="",
    )
    async with state.db.session() as session:
        session.add(row)
        if seeds is not None:
            await session.flush()
            session.add_all(seeds(row.id))
        await session.commit()
        await session.refresh(row)
    if run:
        await scheduler.start_waiting(state.optimizer)
    await state.optimizer.notify()
    return row
