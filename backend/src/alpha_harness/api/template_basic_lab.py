"""Basic Template Research: Template Lab's classic block builder, kept beside the typed editor.

Templates are trees of blocks. Presets are read-only; the ones the user saves live in the
``template`` table under the ``template-lab`` origin as block documents (version 1), apart from
the typed editor's (version 2). A task freezes its template and market when it is added and runs
from the Tasks tab on ``labs.template_v1``, which upstream keeps for exactly this shape.
"""

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..db.models import Template, utcnow
from ..labs import search
from ..labs import template_blocks as template
from ..labs.launch import (
    NO_SIMULATIONS,
    AddedTask,
    FieldCounts,
    LeftOut,
    OperatorsRead,
    SampleAlpha,
    SearchRequest,
    account_operators,
    add_study,
    field_counts,
    market_for,
    operators_read,
    preview_samples,
    startup_trials,
)
from ..labs.params import TEMPLATE_SAMPLER, TemplateParams
from ..schemas import Out
from .deps import State, refuse

router = APIRouter(prefix="/api/template-basic-lab", tags=["template-basic-lab"])

ORIGIN = "template-lab"


class BasicTemplateBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=500)
    tree: dict[str, Any]


class BasicTemplateTask(SearchRequest):
    tree: dict[str, Any]
    template_name: str = Field(default="Template", max_length=128)


class BasicTemplateOptions(Out):
    operators: OperatorsRead
    #: One block per operator: name, category, inputs, options, symbol, output, description.
    blocks: list[dict[str, Any]]
    variables: dict[str, list[int | float | str]]
    data_fields: list[str]
    group_fields: list[str]
    vector: list[str]
    decays: list[int]
    max_simulations: int


class BasicTemplateSummary(Out):
    #: ``preset:<slug>`` for a preset, a number for a saved template.
    id: str | int
    name: str
    description: str | None
    preset: bool
    #: The template document: ``{version, root}``.
    tree: dict[str, Any]
    skeleton: str
    #: Operators the template names that the account does not have.
    missing: list[str]
    #: Where a preset comes from, e.g. AIRS; starred on its card.
    source: str | None
    updated_at: str | None


class BasicTemplateList(Out):
    templates: list[BasicTemplateSummary]


class BasicTemplateRemoved(Out):
    removed: int


class BasicTemplatePreview(Out):
    fields: FieldCounts
    left_out: LeftOut
    universes: list[str]
    skeleton: str
    sample: list[SampleAlpha]
    #: Everything blocking a task; ``templateProblems`` are the ones about the blocks.
    problems: list[str]
    template_problems: list[str]
    warnings: list[str]


def _loaded(tree: dict[str, Any]) -> dict[str, Any]:
    try:
        return template.load(tree)
    except ValueError as exc:
        raise refuse(422, "template_malformed", str(exc)) from exc


async def _table(state: Any) -> dict[str, template.Block]:
    return template.blocks(await account_operators(state, refresh=False))


@router.get("/options")
async def options(state: State, refresh: bool = False) -> BasicTemplateOptions:
    """The blocks this account can build with, and what a task can be set to."""
    operators = await account_operators(state, refresh=refresh)
    described = {o.get("name"): o.get("description") for o in operators}
    return BasicTemplateOptions.model_validate(
        {
            "operators": operators_read(operators),
            "blocks": [
                {**block.to_dict(), "description": described.get(block.name)}
                for block in template.blocks(operators).values()
            ],
            "variables": {name: list(values) for name, values in template.VARIABLES.items()},
            "dataFields": list(template.DATA_FIELDS),
            "groupFields": list(template.GROUP_FIELDS),
            "vector": list(search.catalogue(operators).vector),
            "decays": list(search.DECAYS),
            "maxSimulations": search.MAX_SIMULATIONS,
        }
    )


def _saved(row: Template, table: dict[str, template.Block]) -> BasicTemplateSummary:
    doc = row.parsed or {"version": template.VERSION, "root": None}
    return BasicTemplateSummary.model_validate(
        {
            "id": row.id,
            "name": row.name,
            "description": row.description,
            "preset": False,
            "tree": doc,
            "skeleton": row.source,
            "missing": template.missing(doc, table),
            "source": None,
            "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
        }
    )


@router.get("/templates")
async def templates(state: State) -> BasicTemplateList:
    """The presets, then the user's saved templates, newest first."""
    table = await _table(state)
    presets = [
        BasicTemplateSummary.model_validate(
            {
                "id": preset.id,
                "name": preset.name,
                "description": preset.description,
                "preset": True,
                "tree": preset.doc,
                "skeleton": template.skeleton(preset.doc),
                "missing": template.missing(preset.doc, table),
                "source": preset.source,
                "updatedAt": None,
            }
        )
        for preset in template.PRESETS
    ]
    async with state.db.session() as session:
        rows = (
            await session.scalars(
                select(Template)
                .where(Template.origin == ORIGIN)
                .order_by(Template.updated_at.desc())
            )
        ).all()
    # The typed editor's templates are documents of another version; they are its to list.
    blocks = [row for row in rows if (row.parsed or {}).get("version") == template.VERSION]
    return BasicTemplateList(templates=[*presets, *(_saved(row, table) for row in blocks)])


async def _name_free(session: Any, name: str, template_id: int | None = None) -> None:
    if any(preset.name.lower() == name.lower() for preset in template.PRESETS):
        raise refuse(409, "name_taken", f"{name} is the name of a preset.")
    clash = select(Template.id).where(func.lower(Template.name) == name.lower())
    if template_id is not None:
        clash = clash.where(Template.id != template_id)
    if await session.scalar(clash) is not None:
        raise refuse(409, "name_taken", f"A template named {name} already exists.")


@router.post("/templates", status_code=201)
async def create_template(body: BasicTemplateBody, state: State) -> BasicTemplateSummary:
    doc, name = _loaded(body.tree), body.name.strip()
    async with state.db.session() as session:
        await _name_free(session, name)
        row = Template(
            name=name,
            description=body.description,
            source=template.skeleton(doc),
            parsed=doc,
            origin=ORIGIN,
            tags=[],
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return _saved(row, await _table(state))


@router.put("/templates/{template_id}")
async def update_template(
    template_id: int, body: BasicTemplateBody, state: State
) -> BasicTemplateSummary:
    doc, name = _loaded(body.tree), body.name.strip()
    async with state.db.session() as session:
        row = await session.get(Template, template_id)
        if row is None or row.origin != ORIGIN:
            raise refuse(404, "template_not_found", "That template no longer exists.")
        await _name_free(session, name, template_id)
        row.name, row.description = name, body.description
        row.source, row.parsed = template.skeleton(doc), doc
        await session.commit()
        await session.refresh(row)
    return _saved(row, await _table(state))


@router.delete("/templates/{template_id}")
async def delete_template(template_id: int, state: State) -> BasicTemplateRemoved:
    async with state.db.session() as session:
        row = await session.get(Template, template_id)
        if row is None or row.origin != ORIGIN:
            raise refuse(404, "template_not_found", "That template no longer exists.")
        await session.delete(row)
        await session.commit()
    return BasicTemplateRemoved(removed=template_id)


async def _plan(body: BasicTemplateTask, state: Any) -> dict[str, Any]:
    """Everything a task would search, checked, without queueing anything."""
    problems: list[str] = []
    try:
        doc: dict[str, Any] | None = template.load(body.tree)
    except ValueError as exc:
        doc = None
        problems.append(str(exc))
    use = template.used(doc) if doc is not None else None
    market = await market_for(body, state, need=use.data if use else ())
    if doc is not None:
        problems.extend(template.problems(doc, template.blocks(market["operators"])))
    # Said under the blocks; the market's problems are said with the task's settings.
    template_problems = list(problems)
    problems.extend(market["problems"])
    pool = market["pool"]
    if use and len(use.fields) > 1 and 0 < len(pool.fields) < len(use.fields):
        problems.append(
            f"This template reads {len(use.fields)} different fields, "
            f"but the chosen datasets hold {len(pool.fields)}."
        )

    space = {
        "fields": pool.fields,
        "universes": list(pool.universes),
        "absent": pool.absent,
        "vector": market["vector"],
        "neutralizations": market["neutralizations"],
        "variables": {name: list(values) for name, values in template.VARIABLES.items()},
    }
    sample: list[SampleAlpha] = []
    problems = list(dict.fromkeys(problems))
    if not problems and doc is not None:
        run = TemplateParams(
            region=body.region, delay=body.delay, decay=body.decay, tree=doc, space=space
        )
        choices = search.field_choices(space)
        # A draw can land two FIELD tags on one field, so it is given a few more tries.
        sample = preview_samples(
            lambda rng: template.draw(search.RandomTrial(rng), run, choices)[1], tries=25
        )

    return {
        "round": body.cores * 10,
        "fields": field_counts(pool.fields),
        "leftOut": {"vector": pool.vector_skipped},
        "universes": list(pool.universes),
        "skeleton": template.skeleton(doc) if doc is not None else "?",
        "sample": sample,
        "problems": problems,
        "templateProblems": template_problems,
        "warnings": market["warnings"],
        "space": space,
        "tree": doc,
    }


@router.post("/preview")
async def preview(body: BasicTemplateTask, state: State) -> BasicTemplatePreview:
    """What a task would search. Free; queues nothing."""
    return BasicTemplatePreview.model_validate(await _plan(body, state))


@router.post("/tasks", status_code=201)
async def add_task(body: BasicTemplateTask, state: State) -> AddedTask:
    """Add the template's search to Tasks, not started. It spends nothing until run there."""
    if body.simulations < 1:
        raise refuse(422, "no_simulations", NO_SIMULATIONS)
    plan = await _plan(body, state)
    if plan["problems"]:
        raise refuse(422, "template_blocked", plan["problems"][0])

    per_round, size = plan["round"], body.simulations
    return await add_study(
        state,
        now=utcnow(),
        sampler=TEMPLATE_SAMPLER,
        params=TemplateParams(
            tree=plan["tree"],
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
        template_source=plan["skeleton"],
        template_name=body.template_name.strip() or "Template",
    )
