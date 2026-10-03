"""Search Lab: choose datasets, cores and simulations, then run the search as a task.

A task waits in Tasks for free cores and may use more than one day's allowance before it
is done.
"""

from typing import Any

from fastapi import APIRouter

from ..db.models import utcnow
from ..labs import search
from ..labs.launch import (
    NO_SIMULATIONS,
    AddedTask,
    FieldCounts,
    LeftOut,
    SampleAlpha,
    SearchRequest,
    account_operators,
    add_study,
    field_counts,
    market_for,
    preview_samples,
    startup_trials,
)
from ..labs.params import SEARCH_SAMPLER, SearchParams
from ..schemas import Out
from .deps import State, refuse

router = APIRouter(prefix="/api/search-lab", tags=["search-lab"])


class Options(Out):
    vector: list[str]
    decays: list[int]
    max_simulations: int


class Preview(Out):
    fields: FieldCounts
    left_out: LeftOut
    universes: list[str]
    sample: list[SampleAlpha]
    problems: list[str]
    warnings: list[str]


@router.get("/options")
async def options(state: State) -> Options:
    operators = await account_operators(state, refresh=False)
    return Options(
        vector=list(search.catalogue(operators).vector),
        decays=list(search.DECAYS),
        max_simulations=search.MAX_SIMULATIONS,
    )


async def _plan(body: SearchRequest, state: Any) -> dict[str, Any]:
    """Everything a task would search, checked, without queueing anything."""
    market = await market_for(body, state)
    problems, pool = market["problems"], market["pool"]
    cat = search.catalogue(market["operators"])
    shapes = search.families(cat)
    if market["operators"] and not shapes:
        problems.append("None of your operators fit a search shape.")

    space = {
        "fields": pool.fields,
        "families": list(shapes),
        "cs": list(cat.cs),
        "ts": list(cat.ts),
        "group": list(cat.group),
        "vector": market["vector"],
        "lookbacks": list(search.LOOKBACKS),
        "groups": list(search.GROUPS),
        "universes": list(pool.universes),
        "absent": pool.absent,
        "neutralizations": market["neutralizations"],
    }
    sample: list[SampleAlpha] = []
    if not problems:
        run = SearchParams(region=body.region, delay=body.delay, decay=body.decay, space=space)
        choices = search.field_choices(space)
        sample = preview_samples(
            lambda rng: search.request_for(
                search.suggest(search.RandomTrial(rng), space, choices), run
            ),
            tries=5,
        )

    return {
        "round": body.cores * 10,
        "fields": field_counts(pool.fields),
        "leftOut": {"vector": pool.vector_skipped},
        "universes": list(pool.universes),
        "sample": sample,
        "problems": problems,
        "warnings": market["warnings"],
        "space": space,
    }


@router.post("/preview")
async def preview(body: SearchRequest, state: State) -> Preview:
    """What a task would search. Free; queues nothing."""
    return Preview.model_validate(await _plan(body, state))


@router.post("/tasks", status_code=201)
async def add_task(body: SearchRequest, state: State) -> AddedTask:
    """Add the search to Tasks, not started. Running it is the Tasks route's job."""
    if body.simulations < 1:
        raise refuse(422, "no_simulations", NO_SIMULATIONS)
    plan = await _plan(body, state)
    if plan["problems"]:
        raise refuse(422, "search_blocked", plan["problems"][0])
    now = utcnow()
    per_round, size = plan["round"], body.simulations
    return await add_study(
        state,
        now=now,
        sampler=SEARCH_SAMPLER,
        params=SearchParams(
            space=plan["space"],
            region=body.region,
            delay=body.delay,
            decay=body.decay,
            cores=body.cores,
            dataset_ids=body.dataset_ids,
            field_ids=body.field_ids,
            n_startup_trials=startup_trials(len(plan["space"]["fields"]), size, per_round),
        ),
        simulations=size,
        batch_size=per_round,
        template_source="# Search Lab writes its own expressions; there is no template.",
    )
