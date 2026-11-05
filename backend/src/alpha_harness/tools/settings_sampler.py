"""Settings Sampler: run one proven expression everywhere BRAIN will accept it.

The expression, decay and truncation are held exactly as the source Alpha has them, unless
the Truncation Agent sets truncation market by market. What
varies is the market — region, delay, universe — plus neutralization and the
maxTrade/maxPosition pair. A market only counts when every data field the expression reads
is downloaded there, so a two-field Alpha is judged on the intersection.
"""

import asyncio
import math
import random
from itertools import batched, product
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import func, select

from ..brain.platform import (
    DEFAULT_NAN_HANDLING,
    DEFAULT_PASTEURIZATION,
    DEFAULT_TRUNCATION,
    GROUPING_FIELDS,
    INSTRUMENT_TYPE,
    MULTI_SIMULATION_SIZE,
    RA_CHILDREN,
    REGION_AGNOSTIC_REGION,
    TEST_PERIOD,
)
from ..brain.schemas import SimulationRequest, SimulationSettings
from ..brain.settings_schema import valid_values
from ..db.models import StudyStatus, Trial, TrialState
from ..labs import scheduler, template
from ..labs.fastexpr import ParseError, data_fields, parse
from ..labs.truncation import truncation_for

if TYPE_CHECKING:
    from ..db.models import Study
    from ..labs.study import Optimizer

log = structlog.get_logger(__name__)

PENDING_SEND = scheduler.PENDING_SEND


def pairs_for(position_ok: bool) -> list[tuple[str, str]]:
    """The maxTrade/maxPosition pairs BRAIN accepts.

    Both ON is refused everywhere — *"Max Position and Max Trade cannot both be set to On
    simultaneously"* — so a market offers three pairs where Max Position exists and two
    where it does not.
    """
    positions = ["OFF", "ON"] if position_ok else ["OFF"]
    return [(t, p) for t in ("OFF", "ON") for p in positions if not (t == "ON" and p == "ON")]


async def plan(
    state: Any,
    alpha_id: str,
    *,
    expression: str | None = None,
    decay: int | None = None,
    truncation: float | None = None,
    pasteurization: str | None = None,
    nan_handling: str | None = None,
    test_period: str | None = None,
) -> dict[str, Any]:
    """Everything the screen needs: the expression, its fields, and the space they open up.

    From an Alpha, the expression and every setting are its own to begin with. Each of decay,
    truncation, pasteurization, NaN handling and the test period can be overridden anyway:
    re-running a proven expression at a different decay is as much a sweep as re-running it in
    another market, and refusing to let the source Alpha be varied would be an arbitrary line.
    """
    problems: list[str] = []
    warnings: list[str] = []

    if expression is not None:
        source: dict[str, Any] = {}
    else:
        body = await state.endpoints.alpha_body(alpha_id)
        code = body.get("regular") or body.get("combo") or body.get("selection") or {}
        expression = code.get("code") if isinstance(code, dict) else None
        source = dict(body.get("settings") or {})
    # An override supplied stands; anything left out keeps the Alpha's own, or the platform
    # default when there is no Alpha to inherit from.
    source["decay"] = source.get("decay", 0) if decay is None else decay
    source["truncation"] = (
        source.get("truncation", DEFAULT_TRUNCATION) if truncation is None else truncation
    )
    source["pasteurization"] = (
        pasteurization or source.get("pasteurization") or DEFAULT_PASTEURIZATION
    )
    source["nanHandling"] = nan_handling or source.get("nanHandling") or DEFAULT_NAN_HANDLING
    source["testPeriod"] = test_period or source.get("testPeriod") or TEST_PERIOD
    if not expression:
        problems.append(f"{alpha_id or 'The expression'} has no expression to re-run.")
        return _empty(alpha_id, "", [], source, problems, warnings)

    try:
        tree = parse(expression)
        fields = data_fields(tree)
        # Grouping fields do not count as data but must exist where it runs, so the markets
        # are placed on everything it reads.
        placed = data_fields(tree, grouping=True)
    except ParseError:
        problems.append("Its expression could not be parsed, so its data fields are unknown.")
        return _empty(alpha_id, expression, [], source, problems, warnings)
    if not fields:
        problems.append("Its expression reads no data field, so there is nothing to place.")
        return _empty(alpha_id, expression, [], source, problems, warnings)

    # Read in parallel: catalog reads run off the event loop in threads and take no write
    # lock, so a multi-field Alpha waits once rather than once per field.
    per_field = await asyncio.gather(*(state.queries.field_availability(f) for f in placed))
    held: dict[tuple[str, int, str], float] = {}
    for index, (field, rows) in enumerate(zip(placed, per_field, strict=True)):
        here = {
            (str(r["region"]), int(r["delay"]), str(r["universe"])): float(r["coverage"] or 0.0)
            for r in rows
            if r["instrument_type"] == INSTRUMENT_TYPE
        }
        if not here:
            problems.append(f"{field} is not downloaded in any market. Sync from BRAIN first.")
            return _empty(
                alpha_id, expression, fields, source, problems, warnings, _grouping(placed)
            )
        # The Alpha needs every field present, and is only as covered as its thinnest one.
        held = here if index == 0 else {k: min(v, here[k]) for k, v in held.items() if k in here}
    if not held:
        problems.append(f"No downloaded market holds all of {', '.join(fields)} together.")
        return _empty(alpha_id, expression, fields, source, problems, warnings, _grouping(placed))

    schema = await state.metadata.cached_settings_schema()
    if not schema:
        problems.append("BRAIN's settings list is not loaded. Sign in again.")
        return _empty(alpha_id, expression, fields, source, problems, warnings, _grouping(placed))

    if missing := await _unsynced(state, schema):
        warnings.append(
            f"{missing} market{'s' if missing > 1 else ''} are not downloaded, so this is what "
            "your catalog can see rather than everything BRAIN offers."
        )

    regions = _regions(held, schema)
    totals = _totals(regions)
    return {
        "alphaId": alpha_id,
        "expression": expression,
        "dataFields": fields,
        "groupingFields": _grouping(placed),
        "settings": _settings(source),
        "regions": regions,
        "totals": totals,
        "problems": problems,
        "warnings": warnings,
    }


def _regions(
    held: dict[tuple[str, int, str], float],
    schema: dict[str, Any],
) -> list[dict[str, Any]]:
    by_region: dict[str, dict[int, dict[str, float]]] = {}
    for (region, delay, universe), coverage in held.items():
        by_region.setdefault(region, {}).setdefault(delay, {})[universe] = coverage

    out: list[dict[str, Any]] = []
    for region, delays in by_region.items():
        market = {"instrumentType": INSTRUMENT_TYPE, "region": region, "delay": next(iter(delays))}
        neutralizations = [str(n) for n in valid_values(schema, "neutralization", market)]
        position = template.takes_max_position(region)
        pairs = pairs_for(position)
        markets: list[dict[str, Any]] = [
            {
                "region": region,
                "delay": delay,
                "universe": universe,
                "coverage": coverage,
                "total": len(neutralizations) * len(pairs),
                "agentTruncation": truncation_for(region, delay, universe),
            }
            for delay in sorted(delays)
            for universe, coverage in sorted(delays[delay].items())
        ]
        out.append(
            {
                "region": region,
                "delays": sorted(delays),
                "universes": sorted({m["universe"] for m in markets}),
                "neutralizations": neutralizations,
                "pairs": [{"maxTrade": t, "maxPosition": p} for t, p in pairs],
                "positionAvailable": position,
                # All regions sends region-agnostic simulations, each charged per region it
                # reaches. Said per region so every estimate can count it, not hide it.
                "cost": simulation_cost(region),
                "markets": markets,
                "total": sum(int(m["total"]) for m in markets),
            }
        )
    # Richest first: the regions worth the most simulations lead, ties alphabetical.
    out.sort(key=lambda r: (-int(r["total"]), str(r["region"])))
    return out


def simulation_cost(region: str) -> int:
    """Simulations of the day's allowance one run in this region uses."""
    return RA_CHILDREN if region == REGION_AGNOSTIC_REGION else 1


def batch_size(region: str) -> int:
    """How many runs one multi-simulation carries: BRAIN fails a batch of region-agnostic ones."""
    return 1 if region == REGION_AGNOSTIC_REGION else MULTI_SIMULATION_SIZE


def _totals(regions: list[dict[str, Any]]) -> dict[str, Any]:
    batches = 0
    for region in regions:
        per_delay: dict[int, int] = {}
        for market in region["markets"]:
            delay = int(market["delay"])
            per_delay[delay] = per_delay.get(delay, 0) + int(market["total"])
        size = batch_size(str(region["region"]))
        batches += sum(math.ceil(n / size) for n in per_delay.values())
    return {"total": sum(int(r["total"]) * int(r["cost"]) for r in regions), "batches": batches}


def _settings(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "region": source.get("region"),
        "universe": source.get("universe"),
        "delay": source.get("delay"),
        "neutralization": source.get("neutralization"),
        "decay": source.get("decay"),
        "truncation": source.get("truncation"),
        "maxTrade": source.get("maxTrade") or "OFF",
        "maxPosition": source.get("maxPosition") or "OFF",
        "pasteurization": source.get("pasteurization") or DEFAULT_PASTEURIZATION,
        "nanHandling": source.get("nanHandling") or DEFAULT_NAN_HANDLING,
        "testPeriod": source.get("testPeriod") or TEST_PERIOD,
    }


def _grouping(placed: list[str]) -> list[str]:
    """The grouping fields among those read: shown, though BRAIN counts none as data."""
    return [f for f in placed if f in GROUPING_FIELDS]


def _empty(
    alpha_id: str,
    expression: str,
    fields: list[str],
    source: dict[str, Any],
    problems: list[str],
    warnings: list[str],
    grouping: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "alphaId": alpha_id,
        "expression": expression,
        "dataFields": fields,
        "groupingFields": grouping or [],
        "settings": _settings(source),
        "regions": [],
        "totals": {"total": 0, "batches": 0},
        "problems": problems,
        "warnings": warnings,
    }


async def _unsynced(state: Any, schema: dict[str, Any]) -> int:
    """Markets BRAIN offers that the catalog has never downloaded.

    Availability is read from downloaded fields, so an unsynced market is invisible rather
    than empty. Saying how many are missing keeps a partial catalog from reading as a
    complete answer.
    """
    synced = {
        (str(r["region"]), int(r["delay"]), str(r["universe"]))
        for r in await state.queries.synced_tuples()
        if r["instrument_type"] == INSTRUMENT_TYPE
    }
    base = {"instrumentType": INSTRUMENT_TYPE}
    offered = {
        (str(region), int(delay), str(universe))
        for region in valid_values(schema, "region", base)
        if region != REGION_AGNOSTIC_REGION
        for delay in valid_values(schema, "delay", {**base, "region": region})
        for universe in valid_values(schema, "universe", {**base, "region": region, "delay": delay})
    }
    return len(offered - synced)


#: Neutralization that neutralizes against nothing.
NO_NEUTRALIZATION = "NONE"


def market_neutral(neutralization: str, trade: str, position: str) -> bool:
    """Whether this combination holds the book against the market at all.

    ``NONE`` with neither Max Trade nor Max Position is the one combination that does not:
    nothing is projected out and nothing is capped, so the Alpha carries the market's own
    direction. Either constraint on its own is enough, which is why this is not simply
    "neutralization is not NONE".
    """
    return neutralization != NO_NEUTRALIZATION or trade == "ON" or position == "ON"


def expand(
    plan_rows: list[dict[str, Any]],
    chosen: set[tuple[str, int, str]],
    neutralizations: set[str],
    pairs: set[tuple[str, str]],
    source: dict[str, Any],
    *,
    market_neutral_only: bool = True,
    truncation_agent: bool = False,
) -> list[SimulationRequest]:
    """Every simulation the selection asks for, ordered for both packing and watching.

    Two things pull against each other here. A multi-simulation's children must share region
    and delay, and the engine fills batches from the head of the queue, so simulations mixed
    one by one would fragment into part-full batches (``engine/packer.py``). But a queue in
    market order means watching one region for minutes before another appears.

    So the shuffling happens a batch at a time: each ten stay in one market, and the order
    those tens go out is random. Packing is untouched and no region waits its turn.

    With ``truncation_agent`` each market takes the agent's truncation.
    """
    expression = str(source.get("expression") or "")
    decay = int(source.get("decay") or 0)
    held = source.get("truncation")
    truncation = DEFAULT_TRUNCATION if held is None else float(held)
    pasteurization = str(source.get("pasteurization") or DEFAULT_PASTEURIZATION)
    nan_handling = str(source.get("nanHandling") or DEFAULT_NAN_HANDLING)
    test_period = str(source.get("testPeriod") or TEST_PERIOD)

    groups: dict[tuple[str, int], list[SimulationRequest]] = {}
    for region in plan_rows:
        legal_pairs = [
            (str(p["maxTrade"]), str(p["maxPosition"]))
            for p in region["pairs"]
            if not pairs or (p["maxTrade"], p["maxPosition"]) in pairs
        ]
        legal_neutral = [
            str(n) for n in region["neutralizations"] if not neutralizations or n in neutralizations
        ]
        markets = [
            m
            for m in region["markets"]
            if not chosen or (str(m["region"]), int(m["delay"]), str(m["universe"])) in chosen
        ]
        for market, neutralization, (trade, position) in product(
            markets, legal_neutral, legal_pairs
        ):
            if market_neutral_only and not market_neutral(neutralization, trade, position):
                continue
            key = (str(market["region"]), int(market["delay"]))
            request = SimulationRequest(
                settings=SimulationSettings(
                    region=key[0],
                    universe=str(market["universe"]),
                    delay=key[1],
                    neutralization=neutralization,
                    decay=decay,
                    truncation=(
                        truncation_for(*key, str(market["universe"]))
                        if truncation_agent
                        else truncation
                    ),
                    pasteurization=pasteurization,
                    nan_handling=nan_handling,
                    test_period=test_period,
                    max_trade=trade,
                    max_position=position,
                ),
                regular=expression,
            )
            groups.setdefault(key, []).append(request)

    # Whole batches, so every ten still share a market, then those batches interleaved.
    #
    # Only the full tens are shuffled. The engine reads a window of free x 10 rows off the
    # head of the queue, so a run of exact tens always meets that window on a boundary and
    # packs as it was built; a short tail let into the middle would straddle one and split
    # into two part-full batches. Tails therefore go last, where they cost nothing that the
    # remainder was not already going to cost.
    full: list[tuple[SimulationRequest, ...]] = []
    tails: list[tuple[SimulationRequest, ...]] = []
    for members in groups.values():
        for chunk in batched(members, MULTI_SIMULATION_SIZE, strict=False):
            (full if len(chunk) == MULTI_SIMULATION_SIZE else tails).append(chunk)
    random.shuffle(full)
    random.shuffle(tails)
    return [request for chunk in (*full, *tails) for request in chunk]


def seed_trials(requests: list[SimulationRequest]) -> Any:
    """Parked trials for every simulation, written with the task in one transaction."""

    def build(study_id: int) -> list[Trial]:
        return [
            Trial(
                study_id=study_id,
                number=number,
                params={},
                distributions={},
                expression=request.regular,
                settings=request.settings.model_dump(by_alias=True, exclude_none=True),
                state=TrialState.PRUNED,
                message=PENDING_SEND,
            )
            for number, request in enumerate(requests)
        ]

    return build


async def refill(optimizer: Optimizer, row: Study, want: int, waiting: bool) -> int:
    """Hand the next free cores their share of the written simulations.

    Only ``want`` rows are read. A sweep may carry thousands of parked trials, and loading
    them all on every scheduler tick is a cost that grows with the task.
    """
    parked = (
        Trial.study_id == row.id,
        Trial.state == TrialState.PRUNED,
        Trial.message == PENDING_SEND,
    )
    async with optimizer.db.session() as session:
        batch = (
            (
                await session.scalars(
                    select(Trial).where(*parked).order_by(Trial.number).limit(want)
                )
            ).all()
            if want > 0
            else []
        )
        sent = await scheduler.send_parked(optimizer, row, batch) if batch else 0
        # Counted, not loaded, and after the send so it sees what is left. A task cannot be
        # finished by ``advance`` alone: trials answered from the dedup cache are excluded
        # from its committed count, so one that never spends quota would otherwise run on.
        left = await session.scalar(select(func.count()).select_from(Trial).where(*parked))
    if not (waiting or sent or left):
        # No message: the status is the news, and a notice repeating it is noise.
        await scheduler.finish(optimizer, row.id, StudyStatus.COMPLETE, "")
    return sent
