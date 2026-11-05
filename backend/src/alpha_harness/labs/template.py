"""Template Lab: a Fast Expression with ``$variables``, searched for the best Sharpe.

A template is written the way BRAIN reads an Alpha, ``x = ...;`` lines naming its steps and
the last line the Alpha, with a ``$name`` wherever the search chooses. Each variable is
defined beside the template: **fields** from chosen datasets, or **values** typed as a list
of numbers, groups, operator names or whole expressions. Written as ``$name(...)`` it is an
operator. A name takes one value per Alpha, however often it is written.

A task freezes its template, each variable's choices and the settings it searches when it is
added. The search then asks, define-by-run: the universe, every variable in the order it is
first written, the neutralization and the investability constraint.
"""

import functools
import math
import operator
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from ..brain.platform import (
    INVESTABILITY,
    POWER_POOL_MAX_FIELDS,
    POWER_POOL_MAX_OPERATORS,
    PV1_OPERATORS,
    REGION_INVESTABILITY,
    UNCOUNTED_OPERATORS,
)
from ..brain.schemas import SimulationRequest, SimulationSettings
from .fastexpr import (
    Node,
    OperatorInfo,
    ParseError,
    operator_count,
    parse,
    walk,
    walk_with_parents,
)
from .fastexpr import data_fields as names_read
from .fastexpr import render as write

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from .params import TemplateParams

#: What a variable named one of these starts as. Only a start: it changes like any other.
PRESETS: dict[str, str] = {
    "lookback": "5, 10, 21, 63, 126, 252",
    "fast_lookback": "5, 10, 21",
    "slow_lookback": "63, 126, 252",
    "group": "market, sector, industry, subindustry",
    "weight": "0.5, 1, 2",
    "power": "0.5, 1, 2",
    "ts_op": "ts_rank, ts_zscore, ts_delta, ts_av_diff",
    "group_op": "group_rank, group_zscore, group_neutralize",
}


def takes_max_position(region: str) -> bool:
    return "max_position" in REGION_INVESTABILITY.get(region, ())


MAX_TEXT = 8000
MAX_VARIABLES = 16
MAX_VALUES = 64
#: Where a template's signal goes. A template holding one can be read and counted, not run.
HOLE = "..."
#: Wraps a typed list so the parser reads it as one call's inputs.
_LIST = "values"


# Above the 2,000 templates a stats request may carry, so one request never evicts its own.
@functools.lru_cache(maxsize=4096)
def program(text: str) -> Node:
    """The template as a tree. Raises :class:`ParseError`."""
    return parse(text)


@functools.lru_cache(maxsize=4096)
def _value(text: str) -> Node:
    return parse(text)


def values(text: str) -> list[str]:
    """A typed list, each value as BRAIN will read it. Raises ValueError saying why not.

    Read as the inputs of one call, so a comma inside brackets or quotes stays in its value:
    ``sector, bucket(rank(cap), range="0, 1, 0.1")`` is two values.
    """
    # A comma left at the end is the next value not typed yet, not a mistake.
    text = text.strip().rstrip(",").rstrip()
    if not text:
        raise ValueError("Type its values, separated by commas.")
    try:
        listed = parse(f"{_LIST}({text})")
    except ParseError as exc:
        # Positions count the wrapper, so they would point at the wrong character.
        raise ValueError(re.sub(r" at \d+", "", str(exc))) from exc
    if listed.kind != "call" or listed.value != _LIST or listed.kwargs or not listed.args:
        raise ValueError("Type its values, separated by commas.")
    if any(n.value.startswith("$") for arg in listed.args for _, n in walk(arg)):
        raise ValueError("A value can't hold another $variable.")
    if any(n.value == HOLE for arg in listed.args for _, n in walk(arg)):
        raise ValueError(f"A value can't be {HOLE}: type the signal itself.")
    found = list(dict.fromkeys(write(arg) for arg in listed.args))
    if len(found) > MAX_VALUES:
        raise ValueError(f"A variable holds at most {MAX_VALUES} values.")
    return found


# --- how a template writes its variables ------------------------------------


@dataclass(slots=True)
class Use:
    """How a template writes one variable."""

    name: str
    #: Written as an operator, ``$name(...)``: each call's input count.
    calls: list[int] = field(default_factory=list)
    #: Written as an input, and of those, directly inside a vector operator.
    inputs: int = 0
    in_vector: int = 0
    #: Written as an option's value, ``driver = $driver``.
    options: int = 0


def uses(tree: Node, vector: Callable[[str], bool]) -> dict[str, Use]:
    """Each variable the template writes, in the order it is first written.

    ``vector`` says whether a call's operator reduces a vector field: one of the account's
    vector operators, or a variable whose every value is one.
    """
    found: dict[str, Use] = {}
    for path, node, parent in walk_with_parents(tree):
        if node.kind not in ("name", "call") or not node.value.startswith("$"):
            continue
        use = found.setdefault(node.value[1:], Use(node.value[1:]))
        if node.kind == "call":
            use.calls.append(len(node.args))
            continue
        if parent is not None and path[-1] >= len(parent.args):
            use.options += 1
            continue
        use.inputs += 1
        if parent is not None and parent.kind == "call" and vector(parent.value):
            use.in_vector += 1
    return found


def assigned(tree: Node) -> set[str]:
    """The names a template's own lines define, ``x = ...;``."""
    return {node.value for _, node in walk(tree) if node.kind == "assign"}


def template_problems(tree: Node, table: dict[str, OperatorInfo]) -> list[str]:
    """What is wrong with the template itself, before any variable is looked at."""
    found = [
        f"{node.value} is chosen by the search, so it can't be defined with =. "
        "Name the step without $."
        for _, node in walk(tree)
        if node.kind == "assign" and node.value.startswith("$")
    ]
    if holes(tree):
        found.append(f"Replace each {HOLE} with a signal.")
    found.extend(_calls(tree, table))
    return list(dict.fromkeys(found))


def holes(tree: Node) -> int:
    """How many ``...`` are still to be filled with a signal."""
    return sum(1 for _, node in walk(tree) if node.value == HOLE and node.kind in ("name", "call"))


def _calls(tree: Node, table: dict[str, OperatorInfo]) -> Iterator[str]:
    for _, node in walk(tree):
        if node.kind != "call" or node.value.startswith("$") or node.value == HOLE:
            continue
        info = table.get(node.value)
        if info is None:
            yield f"{node.value} is not one of your operators."
        elif not _takes(info, len(node.args)):
            yield f"{node.value} does not take {_inputs(len(node.args))}."


def _inputs(count: int) -> str:
    return f"{count} input{'' if count == 1 else 's'}"


def _takes(info: OperatorInfo, count: int) -> bool:
    return info.required <= count and (info.maximum is None or count <= info.maximum)


def values_problems(use: Use, listed: list[str], table: dict[str, OperatorInfo]) -> list[str]:
    """Why a variable typed as values can't be written where the template writes it."""
    if use.calls and (use.inputs or use.options):
        return ["It is written both as an operator and as an input. Give each its own name."]
    found: list[str] = []
    if use.calls:
        for value in listed:
            info = table.get(value)
            if info is None:
                found.append(f"{value} is not one of your operators.")
                continue
            found.extend(
                f"{value} does not take {_inputs(count)}."
                for count in dict.fromkeys(use.calls)
                if not _takes(info, count)
            )
        return list(dict.fromkeys(found))
    for value in listed:
        found.extend(_calls(_value(value), table))
    return list(dict.fromkeys(found))


def field_types(use: Use, chosen: list[str] | None) -> list[str]:
    """The field types searched: as chosen, else what the template's writing implies.

    Only matrix and vector fields are searched; a group field is typed as a value instead.
    """
    if picked := [t for t in ("MATRIX", "VECTOR") if t in (chosen or ())]:
        return picked
    inside = use.inputs > 0 and use.in_vector == use.inputs
    return ["VECTOR"] if inside else ["MATRIX"]


def fields_problems(use: Use, types: list[str], vector: list[str]) -> list[str]:
    """Why a variable of fields can't be written where the template writes it.

    Matrix and vector fields together are read as matrix fields, the vector ones reduced by
    an operator the search chooses. Vector fields alone are the template's to reduce, so
    each place it is written must be inside a vector operator.
    """
    if use.calls:
        return ["Fields can't be an operator. Type operator names as its values instead."]
    if use.options:
        return ["Fields can't be an option's value. Type the values instead."]
    found: list[str] = []
    if types == ["VECTOR"]:
        if use.in_vector < use.inputs:
            found.append(
                f"It holds vector fields only, so write it inside a vector operator, "
                f"e.g. vec_avg(${use.name})."
            )
    elif use.in_vector:
        found.append("It is inside a vector operator, so choose Vector fields only.")
    if types == ["MATRIX", "VECTOR"] and not vector:
        found.append("Choose the vector operators its vector fields are reduced with.")
    return found


def data_names(tree: Node, defined: set[str]) -> list[str]:
    """Fixed data fields a tree reads: not variables, groups, options or the template's steps."""
    return [n for n in names_read(tree) if not n.startswith("$") and n not in defined | {HOLE}]


def value_names(listed: list[str], defined: set[str]) -> list[str]:
    return sorted({n for value in listed for n in data_names(_value(value), defined)})


# --- arithmetic on numbers, and how large a template's Alphas are ------------------

_ARITHMETIC: dict[str, Callable[[float, float], Any]] = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": operator.truediv,
    "^": operator.pow,
}


def _number(node: Node) -> float | None:
    if node.kind == "num":
        return float(node.value)
    if node.kind == "unary" and node.value == "-" and node.args[0].kind == "num":
        return -float(node.args[0].value)
    return None


def fold(node: Node) -> Node:
    """Arithmetic between numbers worked out, so ``$lookback / 2`` reaches BRAIN as ``10.5``
    rather than as an operator Power Pool counts."""
    node = replace(
        node,
        args=tuple(fold(arg) for arg in node.args),
        kwargs=tuple((key, fold(arg)) for key, arg in node.kwargs),
    )
    if node.kind != "binary" or node.value not in _ARITHMETIC:
        return node
    left, right = (_number(arg) for arg in node.args)
    if left is None or right is None:
        return node
    try:
        result = _ARITHMETIC[node.value](left, right)
    except ZeroDivisionError, OverflowError:
        return node
    # A negative number to a fractional power is complex, and nothing BRAIN can read.
    if not isinstance(result, float) or not math.isfinite(result):
        return node
    number = Node("num", f"{abs(result):.10g}")
    return Node("unary", "-", (number,)) if result < 0 else number


@dataclass(frozen=True, slots=True)
class Size:
    """How large a template's Alphas are, as Power Pool counts them: fewest and most."""

    operators: tuple[int, int]
    fields: tuple[int, int]
    #: ``...`` still to be filled with a signal.
    holes: int
    #: Data fields read by name, in the template or in a variable's values.
    read: tuple[str, ...]
    #: Variables standing for a field: defined as fields, or not defined at all.
    slots: tuple[str, ...]

    @property
    def power_pool(self) -> tuple[int, int] | None:
        """Operators and fields every Alpha still has free under Power Pool's limits; ``None``
        once some Alpha is over them."""
        free = (
            POWER_POOL_MAX_OPERATORS - self.operators[1],
            POWER_POOL_MAX_FIELDS - self.fields[1],
        )
        return free if min(free) >= 0 else None


def size(tree: Node, typed: dict[str, list[str]]) -> Size:
    """Operators and unique data fields, fewest and most over the values variables can take.

    A variable not typed as values stands for one field. Written as an operator, it is the
    operator it draws, so one that may be ``ts_backfill`` may cost nothing.
    """
    written = uses(tree, lambda _: False)
    defined = assigned(tree)
    calls = {name for name, use in written.items() if use.calls}

    def fields_of(node: Node) -> set[str]:
        return {n for n in names_read(node) if n not in defined and n != HOLE}

    def weight(name: str, text: str) -> tuple[int, int]:
        if name in calls:
            return int(text not in UNCOUNTED_OPERATORS), 0
        value = fold(_value(text))
        return operator_count(value), len(fields_of(value))

    def counted(pick: Callable[..., str]) -> tuple[int, int]:
        chosen = {
            name: pick(listed, key=functools.partial(weight, name))
            for name, listed in typed.items()
            if listed
        }

        def filled(node: Node) -> Node:
            drawn = chosen.get(node.value[1:]) if node.value.startswith("$") else None
            if node.kind == "name" and drawn is not None:
                return _value(drawn)
            return replace(
                node,
                value=drawn if node.kind == "call" and drawn is not None else node.value,
                args=tuple(filled(arg) for arg in node.args),
                kwargs=tuple((key, filled(arg)) for key, arg in node.kwargs),
            )

        whole = fold(filled(tree))
        return operator_count(whole), len(fields_of(whole))

    (fewest_ops, fewest_fields), (most_ops, most_fields) = counted(min), counted(max)
    read = set(data_names(tree, defined))
    for name, listed in typed.items():
        if name in written and written[name].inputs:
            read.update(value_names(listed, defined))
    return Size(
        operators=(fewest_ops, most_ops),
        fields=(min(fewest_fields, most_fields), max(fewest_fields, most_fields)),
        holes=holes(tree),
        read=tuple(sorted(read)),
        slots=tuple(n for n, use in written.items() if use.inputs and n not in typed),
    )


def single_dataset(
    tree: Node,
    sized: Size,
    datasets: dict[str, str],
    slot_datasets: dict[str, frozenset[str] | None],
) -> bool:
    """Whether every Alpha the template makes reads one dataset: BRAIN's single dataset Alpha.

    One field per Alpha is one dataset whatever it is. Past that, every field's dataset has to
    be known and the same: ``datasets`` by field read, ``slot_datasets`` by variable, ``None``
    where a variable's datasets are not known yet.
    """
    sources: list[frozenset[str] | None] = [
        frozenset({datasets[name]}) if name in datasets else None for name in sized.read
    ]
    sources.extend(slot_datasets.get(slot) for slot in sized.slots)
    if any(n.kind == "call" and n.value in PV1_OPERATORS for _, n in walk(tree)):
        sources.append(frozenset({"pv1"}))
    if sized.holes or not sources:
        return False
    if len(sources) == 1:
        return True
    known = [source for source in sources if source is not None]
    return len(known) == len(sources) and len(frozenset[str]().union(*known)) == 1


# --- the search ---------------------------------------------------------------


def field_choices(space: dict[str, Any]) -> dict[str, dict[str, tuple[str, ...]]]:
    """Per variable of fields, per universe, the fields it can take there.

    Frozen with the task, so every ``$name@universe`` slot keeps one distribution. Tuples,
    which Optuna keeps as given rather than copying for every trial it asks.
    """
    found: dict[str, dict[str, tuple[str, ...]]] = {}
    for variable in space["variables"]:
        if variable["kind"] != "fields":
            continue
        absent = variable.get("absent") or {}
        by_universe: dict[str, tuple[str, ...]] = {}
        for universe in space["universes"]:
            missing = set[str](absent.get(universe) or ())
            by_universe[universe] = tuple(f for f in variable["fields"] if f not in missing)
        found[variable["name"]] = by_universe
    return found


def slots(space: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """Every field slot, by the name :func:`suggest` asks it under, with its choices."""
    return {
        f"${name}@{universe}": choices
        for name, by_universe in field_choices(space).items()
        for universe, choices in by_universe.items()
    }


def _first_fields(space: dict[str, Any]) -> dict[str, Any] | None:
    return next((v for v in space["variables"] if v["kind"] == "fields"), None)


def coverage(space: dict[str, Any]) -> tuple[str, list[str]]:
    """The first variable of fields, whose every field the first pass tries once.

    Only fields a searched universe holds: the pool was read before universes lacking a fixed
    field were left out, and a field found only in one of those cannot be asked anywhere.
    """
    first = _first_fields(space)
    if not first:
        return "", []
    absent = first.get("absent") or {}
    missing = [set[str](absent.get(u) or ()) for u in space["universes"]]
    held = [f for f in first["fields"] if any(f not in m for m in missing)]
    return f"${first['name']}", held


def first_pass(space: dict[str, Any], field_id: str) -> dict[str, Any]:
    """Fixed choices that try a field once, in the first universe that has it."""
    first = _first_fields(space) or {}
    absent = first.get("absent") or {}
    universe = next(
        (u for u in space["universes"] if field_id not in set[str](absent.get(u) or ())),
        space["universes"][0],
    )
    return {"universe": universe, f"${first.get('name')}@{universe}": field_id}


def _ask(trial: Any, name: str, choices: list[Any]) -> Any:
    """Only a real choice is asked, so a single value never becomes a dimension."""
    return choices[0] if len(choices) == 1 else trial.suggest_categorical(name, choices)


def suggest(
    trial: Any, run: TemplateParams, choices: dict[str, dict[str, tuple[str, ...]]]
) -> dict[str, Any]:
    """One point, asked define-by-run in a fixed order so every name keeps one distribution.

    A field is asked from its universe's own slot, ``$name@universe``, because BRAIN scopes
    fields by universe: the universe is chosen first, so a field is never paired with one
    that lacks it.
    """
    space = run.space
    universe = _ask(trial, "universe", list(space["universes"]))
    params: dict[str, Any] = {"universe": universe}
    for variable in space["variables"]:
        key = f"${variable['name']}"
        if variable["kind"] == "fields":
            field_id = trial.suggest_categorical(
                f"{key}@{universe}", choices[variable["name"]][universe]
            )
            params[key] = field_id
            if variable["fields"][field_id] == "VECTOR" and variable.get("vector"):
                params[f"{key}#vector"] = _ask(trial, f"{key}#vector", list(variable["vector"]))
        else:
            params[key] = _ask(trial, key, list(variable["values"]))
    params["neutralization"] = _ask(trial, "neutralization", list(space["neutralizations"]))
    params["investability"] = _ask(trial, "investability", list(space["investability"]))
    return params


def render(text: str, space: dict[str, Any], params: dict[str, Any]) -> str:
    """The Fast Expression for one point of the search.

    The last line goes to BRAIN as an expression: ``alpha = ...`` there is read as what it
    assigns, which is how BRAIN treats the last line whatever it is called.
    """
    kinds = {v["name"]: v["kind"] for v in space["variables"]}

    def chosen(name: str) -> Node:
        value = params[f"${name}"]
        if kinds[name] != "fields":
            return _value(value)
        vector = params.get(f"${name}#vector")
        return Node("call", vector, (Node("name", value),)) if vector else Node("name", value)

    def filled(node: Node) -> Node:
        if node.kind == "name" and node.value.startswith("$"):
            return chosen(node.value[1:])
        name = (
            params[node.value] if node.kind == "call" and node.value.startswith("$") else node.value
        )
        return replace(
            node,
            value=name,
            args=tuple(filled(arg) for arg in node.args),
            kwargs=tuple((key, filled(arg)) for key, arg in node.kwargs),
        )

    tree = fold(filled(program(text)))
    steps = list(tree.args) if tree.kind == "seq" else [tree]
    if steps[-1].kind == "assign":
        steps[-1] = steps[-1].args[0]
    return write(Node("seq", args=tuple(steps)) if len(steps) > 1 else steps[0])


def request_for(params: dict[str, Any], run: TemplateParams) -> SimulationRequest:
    trade, position = INVESTABILITY[params["investability"]]
    return SimulationRequest(
        settings=SimulationSettings(
            region=run.region,
            delay=run.delay,
            universe=params["universe"],
            neutralization=params["neutralization"],
            decay=run.decay,
            truncation=run.truncation,
            pasteurization=run.pasteurization,
            nan_handling=run.nan_handling,
            test_period=run.test_period,
            max_trade=trade,
            max_position=position,
        ),
        regular=render(run.template, run.space, params),
    )


def draw(
    trial: Any, run: TemplateParams, choices: dict[str, dict[str, tuple[str, ...]]]
) -> tuple[dict[str, Any], SimulationRequest | None]:
    """One point and its simulation; none when two variables of fields landed on one field.

    Two names for fields mean two different fields: a template uses one field twice by writing
    the same name twice. Values are not held apart this way, since two operators or two
    lookbacks drawing the same value is a fair Alpha.
    """
    params = suggest(trial, run, choices)
    # A variable with a single field is no choice: refusing it would refuse every draw.
    drawn = [
        params[f"${v['name']}"]
        for v in run.space["variables"]
        if v["kind"] == "fields" and len(v["fields"]) > 1
    ]
    if len(set(drawn)) < len(drawn):
        return params, None
    return params, request_for(params, run)


def combinations(space: dict[str, Any]) -> int:
    """How many different simulations the search space holds."""
    sizes = [
        len(space["universes"]),
        len(space["neutralizations"]),
        len(space["investability"]),
    ]
    for variable in space["variables"]:
        if variable["kind"] == "fields":
            kinds = list(variable["fields"].values())
            wraps = max(1, len(variable.get("vector") or []))
            sizes.append(kinds.count("MATRIX") + kinds.count("VECTOR") * wraps)
        else:
            sizes.append(len(variable["values"]))
    return math.prod(sizes)


# --- templates saved while they were built from blocks --------------------------------

_V1_CHOICE = re.compile(r"\{\s*([a-z][a-z0-9_]*(?:\s+OR\s+[a-z][a-z0-9_]*)+)\s*\}")
_V1_VARIABLE = re.compile(
    r"\b(FAST_LOOKBACK|SLOW_LOOKBACK|LOOKBACK|FIELD|GROUP|WEIGHT|POWER)(?:[ \t]+([A-D]))?\b"
)


def from_blocks(skeleton: str) -> tuple[str, dict[str, dict[str, str]]]:
    """A template saved as blocks, as text, with the variables its choice blocks become.

    ``FIELD A`` is ``$field`` and ``LOOKBACK B`` is ``$lookback_b``; each choice block,
    ``{ts_rank OR ts_zscore}(...)``, becomes ``$op1(...)`` listing its operators.
    """
    variables: dict[str, dict[str, str]] = {}

    def choice(match: re.Match[str]) -> str:
        name = f"op{len(variables) + 1}"
        listed = ", ".join(re.split(r"\s+OR\s+", match.group(1)))
        variables[name] = {"kind": "values", "values": listed}
        return f"${name}"

    def variable(match: re.Match[str]) -> str:
        tag = (match.group(2) or "A").lower()
        return f"${match.group(1).lower()}" + ("" if tag == "a" else f"_{tag}")

    return _V1_VARIABLE.sub(variable, _V1_CHOICE.sub(choice, skeleton)), variables


if __name__ == "__main__":
    import random
    import sys

    from .params import TemplateParams
    from .search import RandomTrial

    listed = values(' 21, sector, bucket(rank(cap), range="0, 1, 0.1"), 21')
    written = uses(
        program("x = vec_avg($v) + $f; $op(x, $d) + q(x, k = $k)"), {"vec_avg"}.__contains__
    )
    space = {
        "universes": ["TOP3000"],
        "neutralizations": ["SECTOR"],
        "investability": ["none", "max_trade"],
        "variables": [
            {
                "name": "a",
                "kind": "fields",
                "fields": {"f1": "MATRIX", "f2": "VECTOR"},
                "vector": ["vec_max"],
            },
            {"name": "b", "kind": "fields", "fields": {"f1": "MATRIX", "f3": "MATRIX"}},
            {"name": "op", "kind": "values", "values": ["ts_rank", "ts_zscore"]},
        ],
    }
    run = TemplateParams(region="USA", delay=1, space=space, template="alpha = $op($a - $b, 5)")
    picks = field_choices(space)
    drawn = [draw(RandomTrial(random.Random(seed)), run, picks) for seed in range(60)]
    sent = [request.regular or "" for _, request in drawn if request is not None]
    power_pool = program(
        "r = rank($field); if_else(abs(ts_arg_min(low, $lookback) - ts_arg_max(high, $lookback))"
        " > $lookback / 2, r, 1 - r)"
    )
    idea = program("if_else(abs(ts_arg_min(low, $d) - ts_arg_max(high, $d)) > $d / 2, ..., ...)")
    lookbacks = {"lookback": ["21"], "d": ["21"]}
    checks = {
        "a Power Pool template is 8 operators and 3 fields": (
            size(power_pool, lookbacks).operators,
            size(power_pool, lookbacks).fields,
        )
        == ((8, 8), (3, 3)),
        "an idea with two signals to fill has 2 operators and 1 field left": (
            size(idea, lookbacks).holes,
            size(idea, lookbacks).power_pool,
        )
        == (2, (2, 1)),
        "arithmetic on numbers is worked out": write(fold(parse("x > 21 / 2 - -1"))) == "x > 11.5",
        "an operator variable costs what it draws": size(
            program("rank($clean($f, 21))"), {"clean": ["ts_backfill", "ts_mean"]}
        ).operators
        == (1, 2),
        "a comma left at the end is not a value": values("5, 21, ") == ["5", "21"],
        "values keep brackets and drop repeats": listed
        == ["21", "sector", 'bucket(rank(cap), range="0, 1, 0.1")'],
        "uses see vector, operator and option places": (
            written["v"].in_vector,
            written["f"].in_vector,
            written["op"].calls,
            written["k"].options,
        )
        == (1, 0, [2], 1),
        "a variable inside a vector operator holds vector fields": field_types(written["v"], None)
        == ["VECTOR"],
        "only two variables on one field are refused": all(
            p["$a"] == p["$b"] == "f1" for p, request in drawn if request is None
        ),
        "a vector field is reduced": any("vec_max(f2)" in text for text in sent),
        "the last line goes as an expression": bool(sent) and not any("alpha" in t for t in sent),
    }
    if failed := [check for check, ok in checks.items() if not ok]:
        raise SystemExit(f"template self-check failed: {'; '.join(failed)}")
    sys.stdout.write(f"{len(sent)} of 60 drawn, e.g. {sent[0]}\n")
