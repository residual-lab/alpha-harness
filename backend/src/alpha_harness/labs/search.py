"""Search Lab: first- and second-degree Alphas over chosen datasets, searched for Sharpe.

Every field is cleaned with ``ts_backfill(field, 21)`` first. That is data cleaning, not a
degree, and neither is the vector operator a VECTOR field needs before any other operator
can read it. On that base ``B`` the lab writes six shapes:

    cs(B)    ts(B, d)    group(B, g)    cs(ts(B, d))    ts(ts(B, d), d)    group(ts(B, d), g)

A run is a single-objective TPE study asked define-by-run, because the space is
conditional: a lookback exists only where a time-series operator does, a vector operator
only for a vector field, and a field only in a universe that has it. The first pass gives
every field one trial, budget permitting; after that the sampler concentrates on what
scores.
"""

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from ..brain.schemas import SimulationRequest, SimulationSettings
from ..catalog.queries import FieldFilter, Tuple4
from .fastexpr import OperatorInfo, operator_table

if TYPE_CHECKING:  # pragma: no cover
    import random

    from ..catalog.queries import CatalogQueries
    from .params import SearchParams

log = structlog.get_logger(__name__)

#: Data cleaning around every field. Not an operator in BRAIN's count, and not a degree.
BACKFILL = 21
#: The economic windows from the research notes: a week, a month, a quarter, half a year, a year.
LOOKBACKS = (5, 21, 63, 126, 252)
#: Grouping fields for group operators; they do not count as data fields.
GROUPS = ("market", "sector", "industry", "subindustry")
DECAYS = (0, 3, 5, 7, 10)
TRUNCATION = 0.08
NEUTRALIZATIONS = ("MARKET", "SECTOR", "INDUSTRY", "SUBINDUSTRY")
#: Most concurrent slots one lab task may hold: all of the engine's, so a single sweep
#: can use the whole account when nothing else is running.
MAX_CORES = 8
#: A task's simulations. A big task carries on through the next days' allowances.
MAX_SIMULATIONS = 100_000
FAMILIES = ("cs", "ts", "group", "cs_ts", "ts_ts", "group_ts")
POOL_LIMIT = 20_000

#: Cleaning operators, and operators whose inputs do not fit a one-field shape.
EXCLUDED = frozenset(
    {
        "ts_backfill",
        "group_backfill",
        "ts_count_nans",
        "ts_step",
        "hump",
        "days_from_last_change",
        "kth_element",
        "ts_corr",
        "ts_covariance",
        "ts_regression",
        "trade_when",
        "vector_neut",
        "regression_neut",
        "vector_proj",
        "regression_proj",
        "vec_choose",
        "group_cartesian_product",
    }
)


# --- operators --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Catalogue:
    """The account's operators that fit each slot of a shape."""

    cs: tuple[str, ...]
    ts: tuple[str, ...]
    group: tuple[str, ...]
    vector: tuple[str, ...]


def _kind(info: OperatorInfo) -> str | None:
    category = info.category.lower().replace("-", " ").replace("_", " ")
    if "vector" in category or info.name.startswith("vec_"):
        return "vector"
    if "group" in category or info.name.startswith("group_"):
        return "group"
    if "time series" in category or info.name.startswith("ts_"):
        return "ts"
    if "cross sectional" in category:
        return "cs"
    return None


def catalogue(operators: list[dict[str, Any]]) -> Catalogue:
    """Sort the account's operators into the slots a shape can use.

    Arity comes from each operator's published definition: one input for cross-sectional
    and vector operators, ``(x, d)`` for time-series, ``(x, group)`` for group operators.
    Anything else, and the cleaning operators, never fill a slot.
    """
    slots: dict[str, list[str]] = {"cs": [], "ts": [], "group": [], "vector": []}
    for name, info in sorted(operator_table(operators, "REGULAR").items()):
        kind = _kind(info)
        if kind is None or name in EXCLUDED:
            continue
        inputs = 2 if kind in ("ts", "group") else 1
        if info.required == inputs and (info.maximum is None or info.maximum >= inputs):
            slots[kind].append(name)
    return Catalogue(**{kind: tuple(names) for kind, names in slots.items()})


def families(cat: Catalogue) -> tuple[str, ...]:
    """The shapes this account's operators can fill."""
    has = {"cs": bool(cat.cs), "ts": bool(cat.ts), "group": bool(cat.group)}
    needs = {
        "cs": ("cs",),
        "ts": ("ts",),
        "group": ("group",),
        "cs_ts": ("cs", "ts"),
        "ts_ts": ("ts",),
        "group_ts": ("group", "ts"),
    }
    return tuple(f for f in FAMILIES if all(has[n] for n in needs[f]))


# --- expressions ------------------------------------------------------------


def base(field_id: str, vector_op: str | None = None) -> str:
    """A field as every shape reads it: reduced to a matrix if it is a vector, then backfilled."""
    inner = f"{vector_op}({field_id})" if vector_op else field_id
    return f"ts_backfill({inner}, {BACKFILL})"


def render(params: dict[str, Any]) -> str:
    """The Fast Expression for one point of the search."""
    b = base(params["field"], params.get("vector_op"))
    match params["family"]:
        case "cs":
            return f"{params['cs']}({b})"
        case "ts":
            return f"{params['ts']}({b}, {params['d']})"
        case "group":
            return f"{params['group_op']}({b}, {params['group']})"
        case "cs_ts":
            return f"{params['cs']}({params['ts']}({b}, {params['d']}))"
        case "ts_ts":
            inner = f"{params['ts']}({b}, {params['d']})"
            return f"{params['ts_outer']}({inner}, {params['d_outer']})"
        case "group_ts":
            return f"{params['group_op']}({params['ts']}({b}, {params['d']}), {params['group']})"
        case _:
            raise ValueError(f"Unknown shape {params['family']!r}")


def field_choices(space: dict[str, Any]) -> dict[str, list[str]]:
    """Per searched universe, the fields it has, in the pool's order.

    Frozen with the task, so every ``field@universe`` slot keeps one distribution. A task
    added before fields were paired with universes has no ``absent`` and offers them all.
    """
    absent = space.get("absent") or {}
    choices: dict[str, list[str]] = {}
    for universe in space["universes"]:
        missing: set[str] = set(absent.get(universe) or ())
        choices[universe] = [f for f in space["fields"] if f not in missing]
    return choices


def coverage(space: dict[str, Any]) -> tuple[str, list[str]]:
    """The parameter the first pass covers, and the fields it gives a trial each."""
    return "field", list(space["fields"])


def first_pass(space: dict[str, Any], field_id: str) -> dict[str, Any]:
    """Fixed choices that try a field once, in the first universe that has it."""
    absent = space.get("absent") or {}
    universe = next(
        (u for u in space["universes"] if field_id not in set[str](absent.get(u) or ())),
        space["universes"][0],
    )
    return {"universe": universe, f"field@{universe}": field_id}


def suggest(trial: Any, space: dict[str, Any], choices: dict[str, list[str]]) -> dict[str, Any]:
    """One point, asked define-by-run: each choice exists only where its shape needs it.

    Names are shared across shapes (``ts`` and ``d`` are the same slot in every shape that
    has one), so each name keeps a single distribution for the whole run. Fields are the
    exception, because BRAIN scopes them by universe: the universe is chosen first, then a
    field from that universe's own slot, so a field is never paired with a universe that
    lacks it.
    """
    fields: dict[str, str] = space["fields"]
    universes = list(space["universes"])
    universe = (
        universes[0] if len(universes) == 1 else trial.suggest_categorical("universe", universes)
    )
    field_id = trial.suggest_categorical(f"field@{universe}", choices[universe])
    params: dict[str, Any] = {"universe": universe, "field": field_id}
    if fields[field_id] == "VECTOR":
        params["vector_op"] = trial.suggest_categorical("vector_op", list(space["vector"]))
    family = trial.suggest_categorical("family", list(space["families"]))
    params["family"] = family
    if family in ("cs", "cs_ts"):
        params["cs"] = trial.suggest_categorical("cs", list(space["cs"]))
    if family in ("ts", "cs_ts", "ts_ts", "group_ts"):
        params["ts"] = trial.suggest_categorical("ts", list(space["ts"]))
        params["d"] = trial.suggest_categorical("d", list(space["lookbacks"]))
    if family == "ts_ts":
        params["ts_outer"] = trial.suggest_categorical("ts_outer", list(space["ts"]))
        params["d_outer"] = trial.suggest_categorical("d_outer", list(space["lookbacks"]))
    if family in ("group", "group_ts"):
        params["group_op"] = trial.suggest_categorical("group_op", list(space["group"]))
        params["group"] = trial.suggest_categorical("group", list(space["groups"]))
    neutralizations = list(space["neutralizations"])
    params["neutralization"] = (
        neutralizations[0]
        if len(neutralizations) == 1
        else trial.suggest_categorical("neutralization", neutralizations)
    )
    return params


class RandomTrial:
    """Stands in for an Optuna trial, to draw example points without a study."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng

    def suggest_categorical(self, _name: str, choices: list[Any]) -> Any:
        return self.rng.choice(choices)


def request_for(params: dict[str, Any], run: SearchParams) -> SimulationRequest:
    return SimulationRequest(
        settings=SimulationSettings(
            region=run.region,
            delay=run.delay,
            universe=params["universe"],
            neutralization=params["neutralization"],
            decay=run.decay,
            truncation=TRUNCATION,
        ),
        regular=render(params),
    )


def identity_of(expression: str | None, settings: dict[str, Any] | None) -> tuple[str, str]:
    """What makes two trials the same simulation: the expression and the settings.

    Settings go through the request model first: seeds keep the short dict stored with the
    Alpha and children the full request, so compared raw a child identical to a seed looks
    new.
    """
    try:
        normal = SimulationSettings.model_validate(settings or {}).model_dump(
            by_alias=True, exclude_none=True
        )
    except ValueError:
        normal = settings or {}
    return expression or "", json.dumps(normal, sort_keys=True, default=str)


# --- the field pool ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pool:
    #: Field id to MATRIX or VECTOR, across every searched universe, most complete first.
    fields: dict[str, str]
    #: Universes holding at least one field, the chosen market's first.
    universes: tuple[str, ...]
    #: Per universe, the fields it does not have; universes missing none are left out.
    absent: dict[str, list[str]]
    #: Vector fields left out because no vector operator is allowed.
    vector_skipped: int


async def field_pool(
    queries: CatalogQueries,
    *,
    region: str,
    delay: int,
    universes: list[str],
    dataset_ids: list[str],
    allow_vector: bool,
    field_ids: list[str] | None = None,
    narrow: FieldFilter | None = None,
) -> Pool:
    """Fields of the chosen datasets, or only ``field_ids`` of them, and which searched
    universes have each.

    BRAIN scopes fields by universe, so a field missing from one universe is not dropped:
    the search only pairs it with the universes that have it. Ordered by coverage in the
    first universe, the chosen market's, so a capped first pass tries the most complete.

    ``narrow`` is the Data Explorer's filter the datasets were chosen under: only fields it
    shows are searched, read through the same query so the two can never disagree.
    """
    wanted = narrow.field_types if narrow and narrow.field_types else ("MATRIX", "VECTOR")
    types = [t for t in ("MATRIX", "VECTOR") if t in wanted]
    if not types:
        return Pool({}, (), {}, 0)
    found: dict[str, dict[str, str]] = {}
    for universe in universes:
        page = await queries.fields(
            Tuple4(region=region, delay=delay, universe=universe),
            (narrow or FieldFilter()).model_copy(
                update={
                    "dataset_ids": list(dataset_ids),
                    "field_ids": list(field_ids or []),
                    "field_types": types,
                    "sort_by": "coverage",
                    "sort_desc": True,
                    "limit": POOL_LIMIT,
                    "offset": 0,
                }
            ),
        )
        rows = page.get("results") or []
        if rows:
            found[universe] = {str(r["field_id"]): str(r["field_type"]) for r in rows}

    fields: dict[str, str] = {}
    for held in found.values():
        for field_id, field_type in held.items():
            fields.setdefault(field_id, field_type)
    skipped = 0
    if not allow_vector:
        skipped = sum(1 for t in fields.values() if t == "VECTOR")
        fields = {f: t for f, t in fields.items() if t != "VECTOR"}
    kept = tuple(u for u, held in found.items() if any(f in held for f in fields))
    absent = {u: [f for f in fields if f not in found[u]] for u in kept}
    return Pool(fields, kept, {u: ids for u, ids in absent.items() if ids}, skipped)


# --- drawing a point --------------------------------------------------------


def draw(
    trial: Any, run: SearchParams, choices: dict[str, list[str]]
) -> tuple[dict[str, Any], SimulationRequest | None]:
    """One point of the search and the simulation it becomes (see ``labs.scheduler``)."""
    params = suggest(trial, run.space, choices)
    return params, request_for(params, run)
