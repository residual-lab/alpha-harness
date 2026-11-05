"""Template Lab: typed templates with ``$variables``, and the tasks that search them for Sharpe.

Templates the user saves live in the ``template`` table under the ``template-lab`` origin. A
task freezes its template, its variables' choices and its market when it is added and, like
every lab's task, only runs from the Tasks tab.
"""

import asyncio
import contextlib
from typing import Annotated, Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..brain.platform import (
    INSTRUMENT_TYPE,
    INVESTABILITY,
    MAX_DECAY,
    MAX_TRADE_REGIONS,
    POWER_POOL_MAX_FIELDS,
    POWER_POOL_MAX_OPERATORS,
    TEST_PERIOD,
    TEST_PERIOD_PATTERN,
)
from ..brain.schemas import region_label
from ..catalog.queries import FieldFilter, Tuple4
from ..db.models import Template, utcnow
from ..labs import search, template
from ..labs.fastexpr import Node, ParseError, operator_table, walk
from ..labs.launch import (
    NO_NEUTRALIZATION,
    NO_SIMULATIONS,
    OPERATORS_UNREAD,
    AddedTask,
    FieldCounts,
    OperatorsRead,
    SampleAlpha,
    account_operators,
    add_study,
    choices,
    field_counts,
    legal_choices,
    operators_read,
    preview_samples,
    startup_trials,
    synced_universes,
)
from ..labs.params import TEMPLATE_SAMPLER, TemplateParams
from ..schemas import Out
from .deps import State, refuse

router = APIRouter(prefix="/api/template-lab", tags=["template-lab"])

ORIGIN = "template-lab"
LIMITS = [POWER_POOL_MAX_OPERATORS, POWER_POOL_MAX_FIELDS]
#: A saved template's document: its text and its variables.
DOC_VERSION = 2


class FieldsVariable(BaseModel):
    """A variable whose values are data fields: the chosen datasets', narrowed by a filter."""

    kind: Literal["fields"]
    dataset_ids: list[str] = Field(default_factory=list, max_length=500)
    #: The Data Explorer's filter, for anything beyond the datasets: Region Exclusive,
    #: coverage, field types. Field types left out follow where the template writes it.
    filter: FieldFilter | None = None
    #: With matrix and vector fields both searched: what a vector field is reduced with.
    vector_operators: list[str] = Field(default_factory=list, max_length=20)


class ValuesVariable(BaseModel):
    """A variable whose values are typed: numbers, groups, operator names or expressions."""

    kind: Literal["values"]
    values: str = Field(default="", max_length=4000)


Variable = Annotated[FieldsVariable | ValuesVariable, Field(discriminator="kind")]


class TemplateBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=500)
    text: str = Field(default="", max_length=template.MAX_TEXT)
    variables: dict[str, Variable] = Field(default_factory=dict, max_length=64)


class TemplateTask(BaseModel):
    region: str
    delay: int = Field(ge=0, le=1)
    template: str = Field(default="", max_length=template.MAX_TEXT)
    #: By name, without the ``$``. Only the ones the template writes are read.
    variables: dict[str, Variable] = Field(default_factory=dict, max_length=64)
    #: Searched, like the variables. Each needs at least one.
    universes: list[str] = Field(default_factory=list, max_length=20)
    neutralizations: list[str] = Field(default_factory=list, max_length=20)
    investability: list[Literal["none", "max_trade", "max_position"]] = Field(
        default_factory=list, max_length=3
    )
    #: Held at one value for every simulation.
    decay: int = Field(default=0, ge=0, le=MAX_DECAY)
    truncation: float = Field(default=search.TRUNCATION, ge=0, le=1)
    pasteurization: Literal["ON", "OFF"] = "ON"
    nan_handling: Literal["ON", "OFF"] = "ON"
    #: ``P{years}Y{months}M0D``, the shape BRAIN's own field takes.
    test_period: str = Field(default=TEST_PERIOD, pattern=TEST_PERIOD_PATTERN)
    cores: int = Field(default=search.MAX_CORES, ge=1, le=search.MAX_CORES)
    #: Needed to add a task; a preview ignores it.
    simulations: int = Field(default=0, ge=0, le=search.MAX_SIMULATIONS)
    template_name: str = Field(default="Template", max_length=128)


class OperatorDoc(Out):
    name: str
    category: str
    #: Its signature as BRAIN publishes it, e.g. ``ts_rank(x, d, constant = 0)``.
    definition: str
    description: str


class TemplateLabOptions(Out):
    operators: OperatorsRead
    #: Every operator a template can call, for the editor's completions and hovers.
    reference: list[OperatorDoc]
    #: What a variable named one of these starts as.
    presets: dict[str, str]
    #: The account's vector operators, for a variable searching matrix and vector fields.
    vector: list[str]
    max_simulations: int


class TemplateSummary(Out):
    id: int
    name: str
    description: str | None
    text: str
    #: Typed, so every default is filled in: a saved document leaves defaults out, and a
    #: fields variable without its ``dataset_ids`` broke every screen that read it.
    variables: dict[str, Variable]
    updated_at: str | None


class TemplateStats(Out):
    """How large a template's Alphas are, and which kinds of Alpha it makes."""

    #: Fewest and most over the values its variables can take, as Power Pool counts them.
    operators: list[int]
    fields: list[int]
    #: ``...`` still to be filled with a signal.
    holes: int
    #: Operators and fields every Alpha still has free under Power Pool's limits; null once
    #: some Alpha is over them.
    power_pool: list[int] | None
    #: Power Pool's most operators and unique data fields, what the counts are measured against.
    limits: list[int]
    #: Every Alpha it makes reads one dataset: BRAIN's single dataset Alpha.
    single_dataset: bool
    #: Operators it calls that the account does not have.
    missing: list[str]
    #: Why it can't be read; the counts are zero then.
    problem: str | None


class StatsItem(BaseModel):
    text: str = Field(default="", max_length=template.MAX_TEXT)
    variables: dict[str, Variable] = Field(default_factory=dict, max_length=64)


class StatsRequest(BaseModel):
    region: str
    delay: int = Field(ge=0, le=1)
    #: Every card the Templates panel shows: the built-ins and every saved template.
    templates: list[StatsItem] = Field(default_factory=list, max_length=2000)


class TemplateStatsList(Out):
    stats: list[TemplateStats]


class TemplateList(Out):
    templates: list[TemplateSummary]


class TemplateRemoved(Out):
    removed: int


class VariableInfo(Out):
    name: str
    #: ``fields`` or ``values``; empty while it has no definition.
    kind: str
    #: Written as an operator, ``$name(...)``.
    operator: bool
    #: Fields only: what matches in this market, and the types searched.
    fields: FieldCounts | None
    field_types: list[str]
    #: Values only: how many.
    values: int
    problems: list[str]


class TemplateLabPreview(Out):
    #: The template's own size and kind; null while it can't be read.
    stats: TemplateStats | None
    variables: list[VariableInfo]
    #: Legal here and downloaded: what Universe can be ticked from.
    universes: list[str]
    #: What Investability can be ticked from here; Max Position only where BRAIN takes it.
    investability: list[str]
    #: Different simulations the search space holds; 0 while it can't be planned.
    combinations: int
    sample: list[SampleAlpha]
    #: Everything blocking a task, and of those, the ones about the template's text and the
    #: ones about its settings. A variable's own are on the variable.
    problems: list[str]
    template_problems: list[str]
    settings_problems: list[str]
    warnings: list[str]


# --- options and saved templates ------------------------------------------------


@router.get("/options")
async def options(state: State, refresh: bool = False) -> TemplateLabOptions:
    """What a template can use on this account."""
    operators = await account_operators(state, refresh=refresh)
    regular = operator_table(operators)
    return TemplateLabOptions.model_validate(
        {
            "operators": operators_read(operators),
            "reference": [
                {
                    "name": o["name"],
                    "category": str(o.get("category") or ""),
                    "definition": str(o.get("definition") or ""),
                    "description": str(o.get("description") or ""),
                }
                for o in operators
                if o.get("name") in regular
            ],
            "presets": template.PRESETS,
            "vector": list(search.catalogue(operators).vector),
            "maxSimulations": search.MAX_SIMULATIONS,
        }
    )


def _missing(tree: Node, known: set[str]) -> list[str]:
    called = {n.value for _, n in walk(tree) if n.kind == "call" and not n.value.startswith("$")}
    return sorted(called - known - {template.HOLE})


def _saved(row: Template) -> TemplateSummary:
    doc = row.parsed or {}
    if doc.get("version") == DOC_VERSION:
        text, variables = str(doc.get("text") or ""), dict(doc.get("variables") or {})
    else:
        # Saved while templates were built from blocks: its skeleton, as text.
        text, variables = template.from_blocks(row.source or "")
    return TemplateSummary.model_validate(
        {
            "id": row.id,
            "name": row.name,
            "description": row.description,
            "text": text,
            "variables": variables,
            "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
        }
    )


async def _known(state: Any) -> set[str]:
    return {str(o.get("name")) for o in await account_operators(state, refresh=False)}


async def _market(state: Any, region: str, delay: int) -> Tuple4 | None:
    """The market as its widest downloaded universe: its universes hold the same fields,
    bar a few, so that one stands for all of them."""
    rows = [
        r
        for r in await state.queries.synced_tuples()
        if r["instrument_type"] == INSTRUMENT_TYPE
        and r["region"] == region
        and int(r["delay"]) == delay
    ]
    if not rows:
        return None
    widest = max(rows, key=lambda r: int(r["fields"]))
    return Tuple4(region=region, delay=delay, universe=str(widest["universe"]))


Sized = list[tuple[Node, template.Size] | str]


def _sized(items: list[StatsItem]) -> Sized:
    """Each template parsed and counted, or why it can't be read. Half a millisecond each."""
    read: Sized = []
    for item in items:
        try:
            tree = template.program(item.text)
        except ParseError as exc:
            read.append(str(exc))
            continue
        typed: dict[str, list[str]] = {}
        for name, variable in item.variables.items():
            if isinstance(variable, ValuesVariable):
                with contextlib.suppress(ValueError):
                    typed[name] = template.values(variable.values)
        read.append((tree, template.size(tree, typed)))
    return read


def _judged(
    items: list[StatsItem], read: Sized, datasets: dict[str, str], known: set[str]
) -> list[dict[str, Any]]:
    """Each counted template's kind of Alpha, its fields' datasets known."""
    found: list[dict[str, Any]] = []
    for item, entry in zip(items, read, strict=True):
        if isinstance(entry, str):
            found.append(
                {
                    "operators": [0, 0],
                    "fields": [0, 0],
                    "holes": 0,
                    "powerPool": None,
                    "limits": LIMITS,
                    "singleDataset": False,
                    "missing": [],
                    "problem": entry,
                }
            )
            continue
        tree, sized = entry
        slots = {
            name: frozenset(v.dataset_ids)
            if isinstance(v, FieldsVariable) and v.dataset_ids
            else None
            for name, v in item.variables.items()
        }
        free = sized.power_pool
        found.append(
            {
                "operators": list(sized.operators),
                "fields": list(sized.fields),
                "holes": sized.holes,
                "powerPool": list(free) if free is not None else None,
                "limits": LIMITS,
                "singleDataset": template.single_dataset(tree, sized, datasets, slots),
                "missing": _missing(tree, known) if known else [],
                "problem": None,
            }
        )
    return found


#: Templates counted between turns of the event loop: a millisecond or two of parsing.
CHUNK = 5


async def _stats(
    state: Any, items: list[StatsItem], region: str, delay: int
) -> list[dict[str, Any]]:
    """Each template's size and kind of Alpha, its fields' datasets read in one query.

    Counted a few at a time, yielding between: two thousand templates are a fifth of a second
    of parsing, and the engine's polling shares this loop. A worker thread would not help, as
    pure-Python parsing holds the GIL and slowed every other request for the whole count.
    """
    known = await _known(state)
    read: Sized = []
    for start in range(0, len(items), CHUNK):
        read.extend(_sized(items[start : start + CHUNK]))
        await asyncio.sleep(0)
    names = sorted({n for entry in read if not isinstance(entry, str) for n in entry[1].read})
    market = await _market(state, region, delay) if names else None
    datasets = (
        {
            str(r["field_id"]): str(r["dataset_id"])
            for r in await state.queries.fields_by_id(market, names)
        }
        if market
        else {}
    )
    # A field this market lacks still has a dataset, as long as every market agrees on it.
    datasets |= await state.queries.sole_datasets([n for n in names if n not in datasets])
    found: list[dict[str, Any]] = []
    for start in range(0, len(items), CHUNK):
        part = slice(start, start + CHUNK)
        found.extend(_judged(items[part], read[part], datasets, known))
        await asyncio.sleep(0)
    return found


@router.post("/stats")
async def stats(body: StatsRequest, state: State) -> TemplateStatsList:
    """Each template's operators and fields, and whether its Alphas are Power Pool or single
    dataset ones. Free; reads only the catalog."""
    found = await _stats(state, body.templates, body.region, body.delay)
    return TemplateStatsList.model_validate({"stats": found})


class TreeOption(Out):
    name: str
    value: TreeNode


class TreeNode(Out):
    """One step of a template, as the Blocks view draws it. ``...`` is a name: an empty input."""

    kind: Literal["num", "str", "name", "call", "unary", "binary", "ternary", "assign", "seq"]
    value: str
    args: list[TreeNode]
    kwargs: list[TreeOption]


class TreeRequest(BaseModel):
    text: str = Field(max_length=template.MAX_TEXT)


class TemplateTree(Out):
    tree: TreeNode | None = None
    #: Why the text has no tree: the Code view is where to fix it.
    problem: str | None = None


def _tree(node: Node) -> TreeNode:
    return TreeNode.model_validate(
        {
            "kind": node.kind,
            "value": node.value,
            "args": [_tree(a) for a in node.args],
            "kwargs": [{"name": k, "value": _tree(v)} for k, v in node.kwargs],
        }
    )


@router.post("/tree")
async def tree(body: TreeRequest) -> TemplateTree:
    """The template as blocks, read with the same grammar every task runs on."""
    try:
        return TemplateTree(tree=_tree(template.program(body.text)))
    except ParseError as exc:
        return TemplateTree(problem=str(exc))


@router.get("/templates")
async def templates(state: State) -> TemplateList:
    """The user's saved templates, newest first."""
    async with state.db.session() as session:
        rows = (
            await session.scalars(
                select(Template)
                .where(Template.origin == ORIGIN)
                .order_by(Template.updated_at.desc())
            )
        ).all()
    return TemplateList(templates=[_saved(row) for row in rows])


async def _name_free(session: Any, name: str, template_id: int | None = None) -> None:
    clash = select(Template.id).where(
        Template.origin == ORIGIN, func.lower(Template.name) == name.lower()
    )
    if template_id is not None:
        clash = clash.where(Template.id != template_id)
    if await session.scalar(clash) is not None:
        raise refuse(409, "name_taken", f"A template named {name} already exists.")


def _doc(body: TemplateBody) -> dict[str, Any]:
    return {
        "version": DOC_VERSION,
        "text": body.text,
        "variables": {
            name: variable.model_dump(mode="json", exclude_defaults=True)
            for name, variable in body.variables.items()
        },
    }


@router.post("/templates", status_code=201)
async def create_template(body: TemplateBody, state: State) -> TemplateSummary:
    name = body.name.strip()
    async with state.db.session() as session:
        await _name_free(session, name)
        row = Template(
            name=name,
            description=body.description,
            source=body.text,
            parsed=_doc(body),
            origin=ORIGIN,
            tags=[],
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return _saved(row)


@router.put("/templates/{template_id}")
async def update_template(template_id: int, body: TemplateBody, state: State) -> TemplateSummary:
    name = body.name.strip()
    async with state.db.session() as session:
        row = await session.get(Template, template_id)
        if row is None or row.origin != ORIGIN:
            raise refuse(404, "template_not_found", "That template no longer exists.")
        await _name_free(session, name, template_id)
        row.name, row.description = name, body.description
        row.source, row.parsed = body.text, _doc(body)
        await session.commit()
        await session.refresh(row)
    return _saved(row)


@router.delete("/templates/{template_id}")
async def delete_template(template_id: int, state: State) -> TemplateRemoved:
    async with state.db.session() as session:
        row = await session.get(Template, template_id)
        if row is None or row.origin != ORIGIN:
            raise refuse(404, "template_not_found", "That template no longer exists.")
        await session.delete(row)
        await session.commit()
    return TemplateRemoved(removed=template_id)


# --- planning a task -------------------------------------------------------------


async def _held(state: Any, names: set[str], region: str, delay: int) -> dict[str, set[str]]:
    """Per fixed data field, the universes of this market that have it."""
    held: dict[str, set[str]] = {name: set() for name in names}
    for row in await state.queries.universes_holding(region, delay, sorted(names)):
        held[str(row["field_id"])].add(str(row["universe"]))
    return held


async def _plan(body: TemplateTask, state: Any) -> dict[str, Any]:
    """Everything a task would search, checked, without queueing anything."""
    template_problems: list[str] = []
    settings_problems: list[str] = []
    warnings: list[str] = []
    market = f"{region_label(body.region)} delay {body.delay}"

    operators = await account_operators(state, refresh=False)
    if not operators:
        settings_problems.append(OPERATORS_UNREAD)
    table = operator_table(operators)
    vector = set(search.catalogue(operators).vector)

    tree = None
    if not body.template.strip():
        template_problems.append("Write a template.")
    else:
        try:
            tree = template.program(body.template)
        except ParseError as exc:
            template_problems.append(f"The template can't be read: {exc}")

    # -- the settings searched
    schema = await state.metadata.cached_settings_schema()
    if not schema:
        settings_problems.append("BRAIN's settings list is not loaded. Sign in again.")
    legal = legal_choices(schema, body.region, body.delay)
    offered = await synced_universes(state, legal, body.region, body.delay, None)
    if schema and not offered:
        settings_problems.append(f"No {market} market is downloaded. Sync it in BRAIN › Sync.")
    universes = [u for u in offered if u in body.universes]
    if not universes:
        settings_problems.append("Choose at least one Universe.")
    neutralizations = [n for n in choices(legal, "neutralization") if n in body.neutralizations]
    if not neutralizations:
        settings_problems.append(NO_NEUTRALIZATION)
    position = template.takes_max_position(body.region)
    investable = [n for n in INVESTABILITY if n != "max_position" or position]
    investability = [n for n in investable if n in body.investability]
    if not investability:
        settings_problems.append("Choose at least one Investability.")
    elif body.region in MAX_TRADE_REGIONS and investability != ["max_trade"]:
        warnings.append(
            f"In {region_label(body.region)}, an Alpha without Max Trade is only submittable if "
            "it keeps 70% of its Sharpe under investability constraints."
        )

    # -- the variables
    given = body.variables

    def vector_call(op: str) -> bool:
        if not op.startswith("$"):
            return op in vector
        definition = given.get(op[1:])
        if not isinstance(definition, ValuesVariable):
            return False
        try:
            return all(v in vector for v in template.values(definition.values))
        except ValueError:
            return False

    used = template.uses(tree, vector_call) if tree is not None else {}
    if tree is not None:
        template_problems.extend(template.template_problems(tree, table))
    if len(used) > template.MAX_VARIABLES:
        template_problems.append(f"A template holds at most {template.MAX_VARIABLES} variables.")
    defined = template.assigned(tree) if tree is not None else set()
    # Counted over the universes ticked, or every one on offer until some are.
    searched = universes or offered

    infos: list[dict[str, Any]] = []
    space_variables: list[dict[str, Any]] = []
    #: Fixed data fields read, and the variable whose values read each (None: the template).
    reads: dict[str, set[str | None]] = {}
    for name in template.data_names(tree, defined) if tree is not None else []:
        reads.setdefault(name, set()).add(None)
    pools: dict[str, search.Pool] = {}
    for name, use in list(used.items())[: template.MAX_VARIABLES]:
        definition = given.get(name)
        info: dict[str, Any] = {
            "name": name,
            "kind": definition.kind if definition else "",
            "operator": bool(use.calls),
            "fields": None,
            "fieldTypes": [],
            "values": 0,
            "problems": [],
        }
        infos.append(info)
        if definition is None:
            info["problems"].append("Define it: choose its fields, or type its values.")
        elif isinstance(definition, ValuesVariable):
            try:
                listed = template.values(definition.values)
            except ValueError as exc:
                info["problems"].append(str(exc))
                continue
            info["values"] = len(listed)
            info["problems"].extend(template.values_problems(use, listed, table))
            if use.inputs:
                for read in template.value_names(listed, defined):
                    reads.setdefault(read, set()).add(name)
            space_variables.append({"name": name, "kind": "values", "values": listed})
        else:
            narrow = definition.filter or FieldFilter()
            types = template.field_types(use, narrow.field_types or None)
            info["fieldTypes"] = types
            wraps = [v for v in dict.fromkeys(definition.vector_operators) if v in vector]
            info["problems"].extend(template.fields_problems(use, types, wraps))
            if not definition.dataset_ids:
                info["problems"].append("Choose its datasets.")
                continue
            if not searched:
                continue
            pool = await search.field_pool(
                state.queries,
                region=body.region,
                delay=body.delay,
                universes=searched,
                dataset_ids=definition.dataset_ids,
                allow_vector="VECTOR" in types,
                narrow=narrow.model_copy(update={"field_types": types}),
            )
            pools[name] = pool
            info["fields"] = field_counts(pool.fields)
            if not pool.fields:
                info["problems"].append(f"No field in its datasets matches in {market}.")
            space_variables.append(
                {
                    "name": name,
                    "kind": "fields",
                    "fields": pool.fields,
                    "absent": pool.absent,
                    "vector": wraps if types == ["MATRIX", "VECTOR"] else [],
                }
            )

    # -- fixed data fields: each has to exist here, and a universe without one is left out
    held = await _held(state, set(reads), body.region, body.delay) if reads and searched else {}
    for name, readers in reads.items() if searched else ():
        if held.get(name):
            continue
        message = f"{name} is not a data field in {market}."
        for reader in readers:
            if reader is None:
                template_problems.append(message)
            else:
                next(i for i in infos if i["name"] == reader)["problems"].append(message)
    kept: list[str] = []
    for universe in universes:
        short = [n for n, there in held.items() if there and universe not in there]
        empty = [n for n, pool in pools.items() if pool.fields and universe not in pool.universes]
        if short or empty:
            lacking = ", ".join([*short, *(f"${n}" for n in empty)])
            warnings.append(f"{universe} is left out: it has no {lacking}.")
        else:
            kept.append(universe)
    if universes and not kept:
        settings_problems.append("No ticked Universe has everything the template reads.")

    variable_problems = [f"${i['name']}: {p}" for i in infos for p in i["problems"]]
    template_problems = list(dict.fromkeys(template_problems))
    settings_problems = list(dict.fromkeys(settings_problems))
    problems = [*template_problems, *variable_problems, *settings_problems]
    space = {
        "universes": kept,
        "neutralizations": neutralizations,
        "investability": investability,
        "variables": space_variables,
    }
    run = TemplateParams(
        region=body.region,
        delay=body.delay,
        template=body.template,
        space=space,
        decay=body.decay,
        truncation=body.truncation,
        pasteurization=body.pasteurization,
        nan_handling=body.nan_handling,
        test_period=body.test_period,
    )
    sample: list[SampleAlpha] = []
    if not problems:
        picks = template.field_choices(space)
        # A draw can land two variables on one field, so it is given a few more tries.
        sample = preview_samples(
            lambda rng: template.draw(search.RandomTrial(rng), run, picks)[1], tries=25
        )
    own = StatsItem(text=body.template, variables=given)
    return {
        "stats": (await _stats(state, [own], body.region, body.delay))[0]
        if tree is not None
        else None,
        "variables": infos,
        "universes": offered,
        "investability": investable,
        "combinations": template.combinations(space) if not problems else 0,
        "sample": sample,
        "problems": problems,
        "templateProblems": template_problems,
        "settingsProblems": settings_problems,
        "warnings": warnings,
        "run": run,
        "datasetIds": sorted(
            {d for v in given.values() if isinstance(v, FieldsVariable) for d in v.dataset_ids}
        ),
    }


@router.post("/preview")
async def preview(body: TemplateTask, state: State) -> TemplateLabPreview:
    """What a task would search. Free; queues nothing."""
    return TemplateLabPreview.model_validate(await _plan(body, state))


@router.post("/tasks", status_code=201)
async def add_task(body: TemplateTask, state: State) -> AddedTask:
    """Add the template's search to Tasks, not started. It spends nothing until run there."""
    if body.simulations < 1:
        raise refuse(422, "no_simulations", NO_SIMULATIONS)
    plan = await _plan(body, state)
    if plan["problems"]:
        raise refuse(422, "template_blocked", plan["problems"][0])

    run: TemplateParams = plan["run"]
    per_round, size = body.cores * 10, body.simulations
    _, covered = template.coverage(run.space)
    return await add_study(
        state,
        now=utcnow(),
        sampler=TEMPLATE_SAMPLER,
        params=run.model_copy(
            update={
                "cores": body.cores,
                "dataset_ids": plan["datasetIds"],
                "n_startup_trials": startup_trials(len(covered), size, per_round),
            }
        ),
        simulations=size,
        batch_size=per_round,
        template_source=body.template.strip(),
        template_name=body.template_name.strip() or "Template",
        # No test period leaves no train years to score, so the whole period is scored.
        objective="sharpe" if body.test_period == "P0Y0M0D" else None,
    )
