"""LLM Power Pool Lab: an LLM writes Power Pool Alphas for one dataset at a time.

While a task runs, a background call asks the chosen model for 20 expressions. Each is
checked offline (operators, fields, at most 8 operators and 3 data fields, counted the way
BRAIN counts them), given a random universe, neutralization and decay, and kept as a
waiting trial until cores are free. Calls never happen inside ``advance``.
"""

import json
import random
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import func, or_, select

from ..brain.schemas import REGION_AGNOSTIC_REGION, SimulationSettings
from ..catalog.queries import CatalogQueries, FieldFilter, Tuple4
from ..db.models import Study, StudyStatus, Trial, TrialState, utcnow
from ..llm import library
from ..llm.keys import BudgetExhaustedError, LLMError
from ..llm.prompts import POWER_POOL_LAB, REGION_AGNOSTIC_LAB
from ..llm.text import FENCE
from ..tasks import spawn
from . import scheduler, search
from .fastexpr import (
    GROUPING,
    MAX_FIELDS,
    MAX_OPERATORS,
    ParseError,
    node_at,
    operator_count,
    operator_table,
    parse,
    render,
    validate,
    walk,
)
from .objectives import FAILURE
from .params import PowerPoolParams, RegionAgnosticParams, params_of

if TYPE_CHECKING:  # pragma: no cover
    import asyncio

    from ..db.duck import Catalog
    from .study import Optimizer

log = structlog.get_logger(__name__)

#: The built-in's slug, which is also the kind of every saved prompt this lab can send.
KIND = "power_pool_lab"
#: Region Agnostic Lab's built-in, and the kind of every saved prompt it can send.
RA_KIND = "region_agnostic_lab"
#: The regions BRAIN translates a region-agnostic Alpha into, and so the ones this lab runs in.
RA_MARKETS = ("USA", "EUR", "ASI", "GLB")
#: Fewer than this and an Alpha is not region agnostic at all.
MIN_REGIONS = 2
#: BRAIN's region-agnostic universe setting, as the per-region universe each size runs in.
RA_UNIVERSES: dict[str, dict[str, str]] = {
    "LARGE": {"USA": "TOP3000", "EUR": "TOP2500", "ASI": "MINVOL1M", "GLB": "MINVOL1M"},
    "MEDIUM": {"USA": "TOP2000", "EUR": "TOP1200", "ASI": "MINVOL10M", "GLB": "MINVOL10M"},
    "SMALL": {"USA": "TOP1000", "EUR": "TOP800", "ASI": "TOP500", "GLB": "TOPDIV3000"},
}
PER_CALL = 20
FIELDS_PER_CALL = 200
#: Price and volume basics every market has, offered beside the chosen datasets' own fields.
DATA_FIELDS = ("close", "open", "high", "low", "vwap", "volume", "adv20", "returns", "cap")
PROPOSED = "Written by the LLM; waiting for cores."
SCHEMA = {
    "type": "object",
    "properties": {
        "alphas": {
            "type": "array",
            "minItems": PER_CALL,
            "maxItems": PER_CALL,
            "items": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
        }
    },
    "required": ["alphas"],
}

#: In memory, so a restart loses at most one in-flight LLM request per task.
_calls: dict[int, asyncio.Task[None]] = {}
_retry: dict[int, float] = {}


def forget_retry(study_id: int) -> None:
    """Drop the back-off after a failed call, for a task resumed by hand."""
    _retry.pop(study_id, None)


class Rejected(ValueError):
    """Why an expression is thrown away before it is simulated."""


@dataclass(frozen=True, slots=True)
class Field:
    id: str
    type: str
    coverage: float | None
    description: str
    #: How many regions carry it, in the region-agnostic market; null in every other.
    regions: int | None
    #: Filled for chosen fields only, whose prompt lines say where each comes from and how
    #: crowded it is.
    dataset: str = ""
    alphas: int | None = None
    users: int | None = None
    #: Region Agnostic Lab only: the chosen regions that carry it, e.g. ``USA, EUR, GLB``.
    where: str = ""


@dataclass(frozen=True, slots=True)
class Context:
    id: str
    name: str
    category: str
    description: str
    fields: tuple[Field, ...]  # the dataset's own, most complete first
    basics: tuple[Field, ...]
    groups: tuple[str, ...]
    types: dict[str, str]
    own: frozenset[str]
    names: frozenset[str]
    held: dict[str, frozenset[str]]
    #: Set when the task was given single fields: ``fields`` holds only them, in the order
    #: they were ranked, and ``rank_by`` says how.
    chosen: bool = False
    rank_by: str | None = None
    #: The datasets the chosen fields come from, as ``(id, name)``.
    datasets: tuple[tuple[str, str], ...] = ()


#: The dataset a chosen-fields task files every Alpha under: one pool, so one memory.
CHOSEN = "chosen fields"


async def _rows(
    catalog: Catalog, region: str, delay: int, universes: list[str], where: str, args: list[Any]
) -> list[dict[str, Any]]:
    """The market's rows for ``where``, plus the price, volume and grouping fields."""
    marks = ", ".join("?" for _ in universes)
    extra = (*DATA_FIELDS, *GROUPING)
    return await catalog.query(
        f"""
        SELECT field_id, dataset_id, field_type, universe, coverage, description,
               region_coverage, alpha_count, user_count FROM data_field
        WHERE instrument_type = 'EQUITY' AND region = ? AND delay = ? AND universe IN ({marks})
          AND field_type IN ('MATRIX', 'VECTOR', 'GROUP')
          AND ({where} OR field_id IN ({", ".join("?" for _ in extra)}))
        """,  # noqa: S608
        [region, delay, *universes, *args, *extra],
    )


def _best(
    rows: list[dict[str, Any]], universes: list[str]
) -> tuple[dict[str, set[str]], dict[str, dict[str, Any]]]:
    """Which universes hold each field, and its row in the first universe that has it."""
    held: dict[str, set[str]] = {}
    info: dict[str, dict[str, Any]] = {}
    rank = {u: i for i, u in enumerate(universes)}
    for r in rows:
        field_id = str(r["field_id"])
        held.setdefault(field_id, set()).add(str(r["universe"]))
        best = info.get(field_id)
        if best is None or rank[str(r["universe"])] < rank[str(best["universe"])]:
            info[field_id] = r
    return held, info


def _field(r: dict[str, Any], *, chosen: bool = False) -> Field:
    base = Field(
        str(r["field_id"]),
        str(r["field_type"]),
        r["coverage"],
        str(r["description"] or "")[:160],
        r["region_coverage"],
    )
    if not chosen:
        return base
    return replace(
        base, dataset=str(r["dataset_id"]), alphas=r["alpha_count"], users=r["user_count"]
    )


async def chosen_context(
    catalog: Catalog,
    region: str,
    delay: int,
    universes: list[str],
    field_ids: list[str],
    rank_by: str | None,
) -> Context | None:
    """One pool of the fields the task was given, in their ranked order.

    A field no downloaded universe has is left out; ``None`` when none is left.
    """
    marks = ", ".join("?" for _ in field_ids)
    rows = await _rows(catalog, region, delay, universes, f"field_id IN ({marks})", field_ids)
    held, info = _best(rows, universes)
    wanted = [f for f in dict.fromkeys(field_ids) if f in info and f not in GROUPING]
    if not wanted:
        return None
    own = frozenset(wanted)
    ids = list(dict.fromkeys(str(info[f]["dataset_id"]) for f in wanted))
    meta = await catalog.query(
        f"""
        SELECT DISTINCT dataset_id, name FROM data_set
        WHERE region = ? AND delay = ? AND dataset_id IN ({", ".join("?" for _ in ids)})
        """,  # noqa: S608
        [region, delay, *ids],
    )
    names = {str(m["dataset_id"]): str(m["name"] or m["dataset_id"]) for m in meta}
    # Only the chosen fields and the basics are known names, so an Alpha reaching for any
    # other field of these datasets is thrown away rather than simulated.
    known = {f: r for f, r in info.items() if f in own or f in DATA_FIELDS or f in GROUPING}
    return Context(
        id=CHOSEN,
        name=f"{len(wanted)} chosen fields",
        category="",
        description="",
        fields=tuple(_field(info[f], chosen=True) for f in wanted),
        basics=tuple(_field(info[f]) for f in DATA_FIELDS if f in info and f not in own),
        groups=tuple(g for g in GROUPING if g in info),
        types={f: str(r["field_type"]) for f, r in known.items()},
        own=own,
        names=frozenset(known),
        held={f: frozenset(held[f]) for f in known},
        chosen=True,
        rank_by=rank_by,
        datasets=tuple((d, names.get(d, d)) for d in ids),
    )


async def ra_context(
    catalog: Catalog,
    delay: int,
    markets: dict[str, str],
    field_ids: list[str],
    rank_by: str | None,
    dataset_ids: list[str] | None = None,
) -> Context | None:
    """The chosen fields as each region's own market holds them; with no fields chosen, every
    field of ``dataset_ids``, the ones most regions carry first.

    ``markets`` maps each chosen region to its universe. ``held`` then names regions rather
    than universes, which is what decides where an Alpha can run. A field no chosen region has
    is left out; ``None`` when none is left.
    """
    regions = list(markets)
    extra = (*DATA_FIELDS, *GROUPING)
    whole = not field_ids
    chosen = list(dataset_ids or []) if whole else field_ids
    if not chosen:
        return None
    column = "dataset_id" if whole else "field_id"
    pick = f"{column} IN ({', '.join('?' for _ in chosen)})"
    pairs = " OR ".join("(region = ? AND universe = ?)" for _ in regions)
    rows = await catalog.query(
        f"""
        SELECT field_id, dataset_id, field_type, region, universe, coverage, description,
               region_coverage, alpha_count, user_count FROM data_field
        WHERE instrument_type = 'EQUITY' AND delay = ? AND ({pairs})
          AND field_type IN ('MATRIX', 'VECTOR', 'GROUP')
          AND ({pick} OR field_id IN ({", ".join("?" for _ in extra)}))
        """,  # noqa: S608
        [delay, *(v for r in regions for v in (r, markets[r])), *chosen, *extra],
    )
    held: dict[str, set[str]] = {}
    info: dict[str, dict[str, Any]] = {}
    order = {r: i for i, r in enumerate(regions)}
    for row in rows:
        f = str(row["field_id"])
        held.setdefault(f, set()).add(str(row["region"]))
        best = info.get(f)
        if best is None or order[str(row["region"])] < order[str(best["region"])]:
            info[f] = row
    if whole:
        mine = set(chosen)
        wanted = sorted(
            (f for f, r in info.items() if str(r["dataset_id"]) in mine and f not in GROUPING),
            key=lambda f: (-len(held[f]), -(info[f]["coverage"] or 0), f),
        )
    else:
        wanted = [f for f in dict.fromkeys(field_ids) if f in info and f not in GROUPING]
    if not wanted:
        return None
    own = frozenset(wanted)
    ids = list(dict.fromkeys(str(info[f]["dataset_id"]) for f in wanted))
    meta = await catalog.query(
        f"""
        SELECT dataset_id, any_value(name) AS name FROM data_set
        WHERE delay = ? AND dataset_id IN ({", ".join("?" for _ in ids)}) GROUP BY dataset_id
        """,  # noqa: S608
        [delay, *ids],
    )
    names = {str(m["dataset_id"]): str(m["name"] or m["dataset_id"]) for m in meta}
    known = {f: r for f, r in info.items() if f in own or f in DATA_FIELDS or f in GROUPING}

    def where(f: str) -> str:
        return ", ".join(r for r in regions if r in held[f])

    return Context(
        id=CHOSEN,
        name=f"{len(wanted)} chosen fields",
        category="",
        description="",
        fields=tuple(
            replace(_field(info[f], chosen=True), regions=len(held[f]), where=where(f))
            for f in wanted
        ),
        basics=tuple(_field(info[f]) for f in DATA_FIELDS if f in info and f not in own),
        groups=tuple(g for g in GROUPING if g in info),
        types={f: str(r["field_type"]) for f, r in known.items()},
        own=own,
        names=frozenset(known),
        held={f: frozenset(held[f]) for f in known},
        chosen=True,
        rank_by=rank_by if not whole else "regions carrying it, most first",
        datasets=tuple((d, names.get(d, d)) for d in ids),
    )


async def context_for(
    catalog: Catalog,
    region: str,
    delay: int,
    universes: list[str],
    dataset: str,
    narrow: FieldFilter | None = None,
) -> Context | None:
    rows = await _rows(catalog, region, delay, universes, "dataset_id = ?", [dataset])
    if not any(r["dataset_id"] == dataset for r in rows):
        return None
    meta = await catalog.query(
        "SELECT name, description, category_name, subcategory_name FROM data_set "
        "WHERE region = ? AND delay = ? AND dataset_id = ? LIMIT 1",
        [region, delay, dataset],
    )
    held, info = _best(rows, universes)
    own = {f for f, r in info.items() if r["dataset_id"] == dataset}
    if narrow is not None:
        # Only what the Data Explorer showed when the dataset was chosen, read through its query.
        queries = CatalogQueries(catalog)
        shown: set[str] = set()
        for universe in universes:
            page = await queries.fields(
                Tuple4(region=region, delay=delay, universe=universe),
                narrow.model_copy(
                    update={"dataset_ids": [dataset], "limit": search.POOL_LIMIT, "offset": 0}
                ),
            )
            shown.update(str(r["field_id"]) for r in page.get("results") or [])
        own &= shown
    fields = sorted(
        (_field(info[f]) for f in own if f not in GROUPING), key=lambda x: -(x.coverage or 0)
    )
    m = meta[0] if meta else {}
    return Context(
        id=dataset,
        name=str(m.get("name") or dataset),
        category=" › ".join(
            str(c) for c in (m.get("category_name"), m.get("subcategory_name")) if c
        ),
        description=str(m.get("description") or "")[:800],
        fields=tuple(fields),
        basics=tuple(_field(info[f]) for f in DATA_FIELDS if f in info and f not in own),
        groups=tuple(g for g in GROUPING if g in info),
        types={f: str(r["field_type"]) for f, r in info.items()},
        own=frozenset(own),
        names=frozenset(info),
        held={f: frozenset(u) for f, u in held.items()},
    )


def check(
    text: str, ctx: Context, table: dict[str, Any], *, power_pool: bool = True
) -> tuple[str, frozenset[str]]:
    """The canonical expression and the fields it uses, or :class:`Rejected`.

    ``power_pool`` holds it to Power Pool's operator and field limits; Region Agnostic Lab's
    Alphas are ordinary ones and are not.
    """
    try:
        tree = parse(text)
    except ParseError as exc:
        raise Rejected(f"Could not be read: {exc}") from exc
    if problems := validate(tree, table, set(ctx.names)):
        raise Rejected(problems[0])
    if power_pool and (count := operator_count(tree)) > MAX_OPERATORS:
        raise Rejected(f"{count} operators; Power Pool allows 8.")
    used = frozenset(n.value for _, n in walk(tree) if n.kind == "name" and n.value in ctx.names)
    data = sorted(used - set(GROUPING))
    if power_pool and len(data) > MAX_FIELDS:
        raise Rejected(f"{len(data)} data fields ({', '.join(data)}); Power Pool allows 3.")
    if not used & (ctx.own - set(GROUPING)):
        raise Rejected(
            "Uses none of the chosen fields." if ctx.chosen else f"Uses no field of {ctx.id}."
        )
    for path, node in walk(tree):
        if node.kind == "name" and ctx.types.get(node.value) == "VECTOR":
            parent = node_at(tree, path[:-1]) if path else None
            if parent is None or parent.kind != "call" or not parent.value.startswith("vec_"):
                raise Rejected(
                    f"{node.value} is a VECTOR field: put it straight inside a vec_ operator."
                )
    return render(tree), used


def draw(
    rng: random.Random, used: frozenset[str], ctx: Context, run: PowerPoolParams
) -> dict[str, Any]:
    fit = [u for u in run.universes if all(u in ctx.held.get(f, ()) for f in used)]
    if not fit:
        raise Rejected(f"No downloaded universe has all of {', '.join(sorted(used))}.")
    return SimulationSettings(
        region=run.region,
        delay=run.delay,
        universe=rng.choice(fit),
        neutralization=rng.choice(run.neutralizations),
        decay=rng.choice(search.DECAYS),
        truncation=search.TRUNCATION,
    ).model_dump(by_alias=True, exclude_none=True)


def draw_regions(
    rng: random.Random, used: frozenset[str], ctx: Context, run: RegionAgnosticParams
) -> list[dict[str, Any]]:
    """One simulation per chosen region that carries every field ``used``, alike but for the
    region and its universe: the settings are drawn once, so the regions can be compared."""
    regions = [r for r in run.regions if all(r in ctx.held.get(f, ()) for f in used)]
    if len(regions) < MIN_REGIONS:
        where = ", ".join(regions) or "none"
        raise Rejected(
            f"Its fields are together in {len(regions)} of the chosen regions ({where}); "
            f"a region-agnostic Alpha needs {MIN_REGIONS}."
        )
    neutralization = rng.choice(run.neutralizations)
    decay = rng.choice(search.DECAYS)
    return [
        SimulationSettings(
            region=region,
            delay=run.delay,
            universe=run.region_universes[region],
            neutralization=neutralization,
            decay=decay,
            truncation=search.TRUNCATION,
        ).model_dump(by_alias=True, exclude_none=True)
        for region in regions
    ]


def operators_text(operators: list[dict[str, Any]]) -> str:
    table = operator_table(operators)
    by: dict[str, list[str]] = {}
    for o in operators:
        name = o.get("name")
        if name in table:
            line = f"{name}: {o.get('definition')} | {str(o.get('description') or '')[:160]}"
            by.setdefault(str(o.get("category") or "Other"), []).append(line)
    return "\n".join(f"## {c}\n" + "\n".join(lines) for c, lines in sorted(by.items()))


def _line(f: Field) -> str:
    coverage = "?" if f.coverage is None else f"{f.coverage * 100:.0f}%"
    # The region count only exists in the region-agnostic market, and there it decides
    # whether two fields can appear in one expression at all.
    regions = f" · {f.regions}/4 regions" if f.regions is not None else ""
    if f.where:
        crowd = f" · {f.alphas or 0:,} alphas · {f.users or 0:,} users"
        return (
            f"{f.id} · {f.dataset} · {f.type} · {coverage} · in {f.where}{crowd} · {f.description}"
        )
    if f.dataset:
        crowd = f" · {f.alphas or 0:,} alphas · {f.users or 0:,} users"
        return f"{f.id} · {f.dataset} · {f.type} · {coverage}{regions}{crowd} · {f.description}"
    return f"{f.id} · {f.type} · {coverage}{regions} · {f.description}"


async def memory_of(optimizer: Optimizer, study_id: int, dataset: str) -> str:
    """What the task already wrote for ``dataset``, as the few lines the prompt shows.

    Three slices read by the database: loading every trial of a task (up to 100,000) for
    forty lines cost each LLM call a full table load.
    """
    score = func.json_extract(Trial.values, "$[0]")
    mine = (Trial.study_id == study_id, func.json_extract(Trial.params, "$.dataset") == dataset)
    async with optimizer.db.session() as session:
        done = (
            await session.scalars(
                select(Trial)
                .where(*mine, Trial.state == TrialState.COMPLETE, score > FAILURE)
                .order_by(score.desc(), Trial.number)
                .limit(20)
            )
        ).all()
        # The newest of each, shown oldest first.
        waiting = (
            await session.scalars(
                select(Trial)
                .where(
                    *mine,
                    or_(
                        Trial.state.in_([TrialState.QUEUED, TrialState.RUNNING]),
                        Trial.message == PROPOSED,
                    ),
                )
                .order_by(Trial.number.desc())
                .limit(15)
            )
        ).all()[::-1]
        # ``rejected`` is only ever written as true.
        thrown = (
            await session.scalars(
                select(Trial)
                .where(*mine, func.json_extract(Trial.params, "$.rejected") == 1)
                .order_by(Trial.number.desc())
                .limit(5)
            )
        ).all()[::-1]
    return memory_text(list(done), list(waiting), list(thrown))


def _once(trials: list[Any]) -> list[Any]:
    """Each expression once, where it first appears: a region-agnostic Alpha is a trial per
    region, and listing it four times would spend the prompt on nothing new."""
    seen: set[str] = set()
    out = []
    for t in trials:
        if t.expression not in seen:
            seen.add(t.expression)
            out.append(t)
    return out


def memory_text(done: list[Any], waiting: list[Any], thrown: list[Any]) -> str:
    done, waiting, thrown = _once(done), _once(waiting), _once(thrown)
    parts = []
    if done:
        parts.append(
            "Simulated, best Sharpe first:\n"
            + "\n".join(f"{float(t.values[0]):.2f} | {(t.expression or '')[:300]}" for t in done)
        )
    if waiting:
        parts.append(
            "Not simulated yet:\n" + "\n".join((t.expression or "")[:300] for t in waiting)
        )
    if thrown:
        parts.append(
            "Thrown away:\n"
            + "\n".join(f"{(t.expression or '')[:300]} | {(t.message or '')[:120]}" for t in thrown)
        )
    return "\n".join(parts) or "None yet."


#: What a model cannot infer from the market line when the region is ALL. The warning about
#: cross-sectional comparison is BRAIN's own: one expression is translated into four
#: markets whose currencies, market caps and face values are not on one scale.
REGION_AGNOSTIC_BRIEF = """
This expression runs in USA, Europe, Asia and Global at once, and the alpha is submittable
where two or more of them hold up. Two fields combine only where their regions overlap, so
prefer fields carried by all four, and stay inside one dataset. Comparing raw values across
stocks is unsafe here — currencies, market caps and face values differ by region — so
normalise by scale, or compare a stock against its own history with time-series operators.
""".rstrip()


def user_prompt(
    ctx: Context,
    operators: list[dict[str, Any]],
    run: PowerPoolParams,
    memory: str,
    offset: int,
    budget: int,
    system: str = POWER_POOL_LAB,
) -> tuple[str, int]:
    """The user turn, and how many field lines it shows beside ``system``.

    A dataset's fields go most complete first, a window at a time. Chosen fields go in the
    order they were ranked, numbered, and stand in for the dataset the rules speak of.
    """
    ra = isinstance(run, RegionAgnosticParams)
    if ra:
        market = (
            f"MARKETS · Delay {run.delay}\n"
            + "\n".join(f"{r} · {run.region_universes.get(r, '?')}" for r in run.regions)
            + "\nEvery Alpha runs in each of these that carries all its fields, and must hold up "
            "in each."
        )
    else:
        market = f"MARKET\n{run.region} · Delay {run.delay} · Universes {', '.join(run.universes)}"
    if run.region == REGION_AGNOSTIC_REGION and not ra:
        market += REGION_AGNOSTIC_BRIEF
    if ra:
        about = "DATASETS\n" + "\n".join(f"{d} · {name}" for d, name in ctx.datasets)
        mine, ask = (
            "THESE FIELDS",
            f"Write {PER_CALL} new Alphas, each using at least one of your fields.",
        )
    elif ctx.chosen:
        about = "DATASETS\n" + "\n".join(f"{d} · {name}" for d, name in ctx.datasets)
        mine, ask = (
            "THESE FIELDS",
            f"Write {PER_CALL} new Power Pool Alphas, each using at least one of your fields.",
        )
    else:
        about = f"DATASET\n{ctx.id} · {ctx.name} · {ctx.category}\n{ctx.description}"
        mine, ask = ctx.id, f"Write {PER_CALL} new Power Pool Alphas that use {ctx.id}."
    head = "\n\n".join([market, "OPERATORS\n" + operators_text(operators), about])
    tail = "\n\n".join(
        [
            "PRICE AND VOLUME FIELDS · count as data fields\n"
            + "\n".join(_line(f) for f in ctx.basics),
            "GROUPING FIELDS · not counted\n" + ", ".join(ctx.groups),
            f"YOUR EARLIER ALPHAS ON {mine}\n{memory}",
            ask,
        ]
    )
    room = budget * 4 - len(system) - len(head) - len(tail) - 200
    total = len(ctx.fields)
    start = offset % total if total else 0
    ordered = ctx.fields[start:] + ctx.fields[:start]
    lines: list[str] = []
    for f in ordered[:FIELDS_PER_CALL]:
        line = _line(f)
        if room - len(line) < 0:
            break
        room -= len(line) + 1
        lines.append(line)
    if ctx.chosen:
        lines = [f"{start + i + 1}. {line}" for i, line in enumerate(lines)]
        order = f"ranked by {ctx.rank_by}" if ctx.rank_by else "in the order they were chosen"
        title = f"YOUR FIELDS · {start + 1}-{start + len(lines)} of {total:,}, {order}\n" + (
            "They were chosen for this task: every Alpha uses at least one of them, and no "
            "other field of these datasets. Each says which regions carry it."
            if ra
            else "They were chosen for this task and stand in for the dataset in the rules: "
            "every Alpha uses at least one of them, and no other field of these datasets."
        )
    else:
        title = (
            f"FIELDS OF {ctx.id} · {start + 1}-{start + len(lines)} of {total:,}, "
            "most complete first"
        )
    return f"{head}\n\n{title}\n" + "\n".join(lines) + f"\n\n{tail}", len(lines)


# --- the running task ----------------------------------------------------------


async def refill(optimizer: Optimizer, row: Study, want: int, waiting: bool) -> int:
    async with optimizer.db.session() as session:
        # Only the written Alphas still waiting to be sent, not the whole task.
        ready = list(
            (
                await session.scalars(
                    select(Trial)
                    .where(
                        Trial.study_id == row.id,
                        Trial.state == TrialState.PRUNED,
                        Trial.message == PROPOSED,
                    )
                    .order_by(Trial.number)
                )
            ).all()
        )
        batch = ready[:want] if want > 0 else []
        sent = await scheduler.send_parked(optimizer, row, batch) if batch else 0
    left = len(ready) - sent
    llm = params_of(row, PowerPoolParams).llm
    calls = int(llm.get("calls") or 0)
    # Counted from the last resume: a paused task resumed gets a whole allowance again.
    cap = int(llm.get("capFrom") or 0) + max(3, 2 * -(-row.max_trials // PER_CALL))
    stop = (
        "the last 3 LLM calls wrote no new valid Alpha"
        if int(llm.get("empty") or 0) >= 3
        else (f"it reached {cap} LLM calls" if calls >= cap else None)
    )
    busy = row.id in _calls and not _calls[row.id].done()
    if (
        not stop
        and not busy
        and left < row.batch_size
        and time.monotonic() >= _retry.get(row.id, 0.0)
    ):
        _calls[row.id] = spawn(_write(optimizer, row.id), name=f"power-pool-{row.id}")
    elif stop and not busy and not (waiting or sent or left):
        # Paused, not complete: the target is not met, and a resume or a clone with another
        # prompt can still meet it.
        await scheduler.finish(
            optimizer,
            row.id,
            StudyStatus.PAUSED,
            f"Paused: {stop}. Resume to keep going, or clone it with another prompt.",
        )
    return sent


async def _pause(optimizer: Optimizer, study_id: int, message: str) -> None:
    async with optimizer.lock(study_id):
        row = await optimizer.get(study_id)
        if row is None:
            return
        await scheduler.finish(optimizer, study_id, StudyStatus.PAUSED, message)
        await optimizer.engine.drop_queued(row.task)
        await scheduler.prune_unsent(optimizer, study_id)


async def _write(optimizer: Optimizer, study_id: int) -> None:
    try:
        row = await optimizer.get(study_id)
        if row is None or row.status != StudyStatus.RUNNING:
            return None
        run = params_of(row, PowerPoolParams)
        llm = dict(run.llm)
        by = dict(llm.get("byDataset") or {})
        model = optimizer.llm.registry.get(run.model)
        operators = await optimizer.metadata.cached_operators() or []
        catalog = optimizer.alphas.catalog
        ra = run if isinstance(run, RegionAgnosticParams) else None
        if ra is not None:
            dataset = CHOSEN
            ctx = await ra_context(
                catalog, ra.delay, ra.region_universes, ra.field_ids, ra.rank_by, ra.dataset_ids
            )
        elif run.field_ids:
            dataset = CHOSEN
            ctx = await chosen_context(
                catalog, run.region, run.delay, run.universes, run.field_ids, run.rank_by
            )
        else:
            ids = run.dataset_ids
            dataset = min(ids, key=lambda d: (by.get(d, {}).get("calls", 0), ids.index(d)))
            ctx = await context_for(
                catalog,
                run.region,
                run.delay,
                run.universes,
                dataset,
                FieldFilter.model_validate(run.field_filter) if run.field_filter else None,
            )
        if model is None:
            return await _pause(
                optimizer,
                study_id,
                f"{run.model} is no longer set up. Set it up again under LLM Integration › "
                "Models, then resume.",
            )
        if not operators:
            return await _pause(
                optimizer,
                study_id,
                "Your BRAIN operators could not be read. Sign in again, then resume.",
            )
        if ctx is None:
            return await _pause(
                optimizer,
                study_id,
                ("None of the chosen fields is" if run.field_ids else f"{dataset} is not")
                + " in the downloaded catalog. Sync it, then resume.",
            )

        prompt_name, system = await _system(optimizer, run)
        memory = await memory_of(optimizer, study_id, dataset)
        offset = int(by.get(dataset, {}).get("offset", 0))
        user, shown = user_prompt(ctx, operators, run, memory, offset, model.prompt_tokens, system)
        entry: dict[str, Any] = {
            "at": utcnow().isoformat(),
            "dataset": dataset,
            "model": model.id,
            "prompt": prompt_name,
            "fields": shown,
        }
        items: list[dict[str, Any]] = []
        answered = False
        try:
            answer = await optimizer.llm.generate(
                system=system,
                user=user,
                model_ref=model.ref,
                response_schema=SCHEMA,
                temperature=1.0,
            )
            answered = True
            items = parse_alphas(answer.text)[:40]
            entry["tokens"] = answer.total_tokens
            if not items:
                entry["error"] = "The answer was not the JSON asked for: " + answer.text[:200]
        except BudgetExhaustedError as exc:
            _retry[study_id] = time.monotonic() + max(5.0, exc.retry_after)
            if exc.daily:
                await _note(optimizer, study_id, f"Waiting for LLM budget: {exc}")
            return None
        except LLMError as exc:
            _retry[study_id] = time.monotonic() + 60.0
            entry["error"] = str(exc)[:300]
            llm["failed"] = int(llm.get("failed") or 0) + 1
            if llm["failed"] >= 3:
                await _pause(optimizer, study_id, f"The LLM failed 3 times in a row: {exc}")
                return None

        table = operator_table(operators)
        rng = random.Random()
        async with optimizer.lock(study_id), optimizer.db.session() as session:
            stored = await session.get(Study, study_id)
            if stored is None:
                return None
            existing = list(
                (await session.scalars(select(Trial).where(Trial.study_id == study_id))).all()
            )
            seen = {t.expression for t in existing}
            number = max((t.number for t in existing), default=-1)
            valid = rejected = 0
            for item in items:
                text = str(item.get("expression") or "").strip()
                number += 1
                try:
                    expression, used = check(text, ctx, table, power_pool=ra is None)
                    if expression in seen:
                        raise Rejected("Already written in this task.")
                    if ra is not None:
                        # One trial per region, filed under the first one's number: the group
                        # the results show together, and one Alpha to the rest of the task.
                        group = number
                        for i, settings in enumerate(draw_regions(rng, used, ctx, ra)):
                            number = group + i
                            session.add(
                                Trial(
                                    study_id=study_id,
                                    number=number,
                                    params={
                                        "dataset": dataset,
                                        "group": group,
                                        "region": settings["region"],
                                    },
                                    distributions={},
                                    expression=expression,
                                    settings=settings,
                                    state=TrialState.PRUNED,
                                    message=PROPOSED,
                                )
                            )
                    else:
                        session.add(
                            Trial(
                                study_id=study_id,
                                number=number,
                                params={"dataset": dataset},
                                distributions={},
                                expression=expression,
                                settings=draw(rng, used, ctx, run),
                                state=TrialState.PRUNED,
                                message=PROPOSED,
                            )
                        )
                    seen.add(expression)
                    valid += 1
                except Rejected as exc:
                    rejected += 1
                    session.add(
                        Trial(
                            study_id=study_id,
                            number=number,
                            params={"dataset": dataset, "rejected": True},
                            distributions={},
                            expression=text[:2000],
                            settings={},
                            state=TrialState.PRUNED,
                            message=str(exc)[:500],
                            finished_at=utcnow(),
                        )
                    )
            params = params_of(stored, PowerPoolParams)
            llm = dict(params.llm) | {k: llm[k] for k in ("failed",) if k in llm}
            if items:
                llm["failed"] = 0
            # A call that failed showed the model nothing: the same fields go out again next
            # time, and it counts toward neither the call nor the empty-answer stops.
            if answered:
                llm["calls"] = int(llm.get("calls") or 0) + 1
                llm["empty"] = 0 if valid else int(llm.get("empty") or 0) + 1
                by = dict(llm.get("byDataset") or {})
                mine = dict(by.get(dataset) or {})
                mine["calls"] = int(mine.get("calls") or 0) + 1
                mine["offset"] = offset + shown
                by[dataset] = mine
                llm["byDataset"] = by
            entry |= {"valid": valid, "rejected": rejected}
            params.llm = llm
            params.calls = [*params.calls, entry][-100:]
            stored.sampler_params = params.dump()
            if valid and stored.message and stored.message.startswith("Waiting for LLM budget"):
                stored.message = None
        await optimizer.notify()
    except Exception:
        log.exception("power_pool.write_failed", study_id=study_id)
        _retry[study_id] = time.monotonic() + 60.0


async def _system(optimizer: Optimizer, run: PowerPoolParams) -> tuple[str, str]:
    """The saved prompt's text as it is now, so an edit reaches the next call; the text it had
    when the task was added once it is deleted; the built-in when none was chosen."""
    kind = kind_of(run)
    if run.prompt_id is not None:
        try:
            return await library.resolve(optimizer.db, kind, run.prompt_id)
        except library.PromptNotFoundError:
            if run.system:
                return f"{run.prompt_name or 'Prompt'} (deleted)", run.system
    return await library.resolve(optimizer.db, kind, None)


def kind_of(run: PowerPoolParams) -> str:
    """The built-in a task stands on, and so the prompts it can send."""
    return RA_KIND if isinstance(run, RegionAgnosticParams) else KIND


def system_for(run: PowerPoolParams) -> str:
    return REGION_AGNOSTIC_LAB if isinstance(run, RegionAgnosticParams) else POWER_POOL_LAB


async def _note(optimizer: Optimizer, study_id: int, message: str) -> None:
    async with optimizer.db.session() as session:
        stored = await session.get(Study, study_id)
        if stored is not None:
            stored.message = message
    await optimizer.notify()


def parse_alphas(text: str) -> list[dict[str, Any]]:
    """Pull the candidate list out of a response.

    A batch that cannot be parsed is a whole request wasted, so a fenced block is tried next
    and an empty list is returned rather than raising.
    """
    for candidate in (text, *(m.group(1) for m in FENCE.finditer(text))):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError, TypeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("alphas"), list):
            return [a for a in payload["alphas"] if isinstance(a, dict) and a.get("expression")]
        if isinstance(payload, list):
            return [a for a in payload if isinstance(a, dict) and a.get("expression")]
    return []
