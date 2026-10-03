"""Basic Template Research's block engine: the user's own expression templates, searched for
the best Sharpe. Frozen as Template Lab had it before the typed editor, which the Advanced
section uses instead; tasks planned here run on ``labs.template_v1``.

A template is a tree of blocks: operators (or a choice of operators that take the same
inputs), variables (FIELD, LOOKBACK, FAST_LOOKBACK, SLOW_LOOKBACK, GROUP, WEIGHT, POWER),
fixed data fields and numbers. A variable carries a letter tag: blocks with the same name and
tag take the same value in an Alpha, and a different tag varies on its own. Operators and
their inputs come from the account's own definitions, so a template only uses what the
account can run.

A task freezes its tree and market when it is added. The search then asks, define-by-run:
the universe, a field for each FIELD tag from that universe's own fields, a value for each
variable tag, an operator for each choice block, and the neutralization.
"""

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..brain.schemas import SimulationRequest, SimulationSettings
from . import search
from .fastexpr import Node, OperatorInfo, operator_table
from .fastexpr import render as write

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .params import TemplateParams

VERSION = 1
FAST_LOOKBACK = (5, 10, 21)
SLOW_LOOKBACK = (63, 126, 252)
LOOKBACK = (*FAST_LOOKBACK, *SLOW_LOOKBACK)
#: Plain numbers a template searches, e.g. ``add(WEIGHT, signed_power(x, POWER))``.
WEIGHT = (0.5, 1, 2)
POWER = (0.5, 1, 2)
#: What each variable other than FIELD can be. Frozen into a task when it is added.
VARIABLES: dict[str, tuple[Any, ...]] = {
    "LOOKBACK": LOOKBACK,
    "FAST_LOOKBACK": FAST_LOOKBACK,
    "SLOW_LOOKBACK": SLOW_LOOKBACK,
    "GROUP": search.GROUPS,
    "WEIGHT": WEIGHT,
    "POWER": POWER,
}
LOOKBACK_VARIABLES = frozenset({"LOOKBACK", "FAST_LOOKBACK", "SLOW_LOOKBACK"})
VARIABLE_NAMES = frozenset({"FIELD", *VARIABLES})
TAGS = ("A", "B", "C", "D")
#: Fixed fields a template may read. Each is checked per universe when a task is added.
DATA_FIELDS = ("close", "open", "high", "low", "vwap", "volume", "adv20", "returns", "cap")
GROUP_FIELDS = search.GROUPS
MAX_NODES = 60
MAX_CHOICES = 8
#: Operators written between their inputs; the arithmetic ones only when no option is set.
SYMBOLS = {
    "add": "+",
    "subtract": "-",
    "multiply": "*",
    "divide": "/",
    "equal": "==",
    "not_equal": "!=",
    "greater": ">",
    "greater_equal": ">=",
    "less": "<",
    "less_equal": "<=",
}
#: Operators whose inputs no block can give. Backfill is a block: a template shows its own.
EXCLUDED = frozenset({"ts_step"})
#: Operators that make a group: every input is a group, and they fit only group inputs.
GROUP_OUTPUT = frozenset({"densify", "group_cartesian_product"})
#: Operators that turn a *signal* into a group, so they take signals and fit group inputs.
#: ``bucket(rank(x), range="0, 1, 0.1")`` is how a continuous signal becomes something
#: ``group_rank`` can group by.
GROUP_MAKERS = frozenset({"bucket"})
#: Vector operators are field preparation, chosen with the task; the rest are not for Alphas.
EXCLUDED_CATEGORIES = frozenset({"Vector", "Special", "Reduce"})
LOOKBACK_PARAMS = frozenset({"d", "lookback"})
GROUP_PARAMS = frozenset({"group", "g", "g1", "g2"})
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
#: An option's name, or an option *value* that names a behaviour, e.g. ``driver = cauchy``.
_WORD = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
#: An option *value* that is a list of numbers, e.g. ``range = "0, 1, 0.1"`` or
#: ``buckets = "2,5,6,7,10"``. Written as one string because that is how BRAIN takes it.
_NUMBER_LIST = re.compile(r"^-?\d+(?:\.\d+)?(?:\s*,\s*-?\d+(?:\.\d+)?)+$")
#: Straight and curly both: the operator reference is typed prose and uses either.
_QUOTES = "\"'\u201c\u201d"
_COMPARISON = re.compile(r"^\s*input1\s*(==|!=|>=|<=|>|<)\s*input2\s*$")


# --- blocks -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Block:
    """One operator as a block: what goes in each input, and the options it takes."""

    name: str
    category: str
    #: ``signal``, ``lookback`` or ``group`` for each input, in order.
    inputs: tuple[str, ...]
    #: Keyword options with a number or true/false default, e.g. ``{"std": 4}``.
    options: dict[str, float | bool | str]
    #: Written between its two inputs (``x > y``) rather than as a call.
    symbol: str | None = None
    #: What it gives: ``signal``, or ``group`` for an operator that makes a group.
    output: str = "signal"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "inputs": list(self.inputs),
            "options": dict(self.options),
            "symbol": self.symbol,
            "output": self.output,
        }


def blocks(operators: list[dict[str, Any]]) -> dict[str, Block]:
    """The account's operators as blocks, by name.

    Inputs come from each definition's parameters without a default: ``d`` or ``lookback``
    takes a lookback, ``group`` takes a group, anything else a signal. Options are the
    parameters whose default is a number or true/false. Comparisons have no call form in
    their definitions (``input1 > input2``), so they are recognised by that shape.
    """
    found: dict[str, Block] = {}
    for name, info in operator_table(operators, "REGULAR").items():
        if name in EXCLUDED or info.category in EXCLUDED_CATEGORIES:
            continue
        shape = _shape(info)
        if shape is None:
            continue
        if name in GROUP_OUTPUT:
            inputs = tuple("group" for _ in shape[0])
            found[name] = Block(name, info.category, inputs, shape[1], None, "group")
        elif name in GROUP_MAKERS:
            # Its inputs stay signals; only what it *gives* is a group.
            found[name] = Block(name, info.category, shape[0], shape[1], None, "group")
        else:
            found[name] = Block(name, info.category, shape[0], shape[1], SYMBOLS.get(name))
    for operator in operators:
        name, definition = operator.get("name"), operator.get("definition")
        scopes = operator.get("scope")
        if not isinstance(name, str) or not isinstance(definition, str):
            continue
        if isinstance(scopes, list) and scopes and "REGULAR" not in scopes:
            continue
        match = _COMPARISON.match(definition)
        if match and SYMBOLS.get(name) == match.group(1):
            category = str(operator.get("category") or "Logical")
            found[name] = Block(name, category, ("signal", "signal"), {}, match.group(1))
    return dict(sorted(found.items()))


def _shape(info: OperatorInfo) -> tuple[tuple[str, ...], dict[str, float | bool | str]] | None:
    inputs: list[str] = []
    options: dict[str, float | bool | str] = {}
    for raw in info.params:
        key, equals, default = (part.strip() for part in raw.partition("="))
        if equals:
            value = _default(default)
            # A word on a lookback or group parameter is BRAIN's own symbol for the input
            # it takes, not a value to choose: ``ts_backfill(x, lookback = d)`` still takes
            # a lookback, and reading ``d`` as a word would turn that input into an option.
            if isinstance(value, str) and (key in LOOKBACK_PARAMS or key in GROUP_PARAMS):
                value = None
            # ``float("NaN")`` parses, and JSON has no way to write the result. No operator
            # publishes such a default today; one would otherwise break the whole payload.
            if isinstance(value, float) and not math.isfinite(value):
                value = None
            if value is not None and _WORD.match(key):
                options[key] = value
            if value is not None or key not in LOOKBACK_PARAMS:
                continue  # ``lookback = d`` still takes a lookback
        name = key.replace(".", "").strip()
        if not name:
            continue  # the ``..`` of a variadic operator
        if name in LOOKBACK_PARAMS:
            inputs.append("lookback")
        elif name in GROUP_PARAMS:
            inputs.append("group")
        else:
            inputs.append("signal")
    return (tuple(inputs), options) if inputs else None


def _default(text: str) -> float | bool | str | None:
    """One option's default, as its published definition writes it.

    Words and lists of numbers count, not only single numbers and flags. Several operators
    choose behaviour by name — ``quantile(x, driver = gaussian)`` — and ``bucket`` takes the
    edges of its buckets as one string, ``range = "0, 1, 0.1"``. Read as numbers alone, both
    kinds were dropped here, so the block never offered them: no template could ask for
    ``cauchy``, and ``bucket`` had no usable form at all.
    """
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        number = float(text)
    except ValueError:
        value = text.strip().strip(_QUOTES).strip()
        return value if _WORD.match(value) or _NUMBER_LIST.match(value) else None
    return int(number) if number.is_integer() else number


# --- the tree ---------------------------------------------------------------


def load(doc: Any) -> dict[str, Any]:
    """A template as stored: its structure checked, empty slots allowed. Raises ValueError."""
    if not isinstance(doc, dict) or doc.get("version") != VERSION:
        raise ValueError("That is not a Template Lab template.")
    return {"version": VERSION, "root": _load(doc.get("root"), 0, [0])}


def _load(slot: Any, depth: int, count: list[int]) -> dict[str, Any] | None:
    if slot is None:
        return None
    count[0] += 1
    if not isinstance(slot, dict) or depth > 30 or count[0] > MAX_NODES * 2:
        raise ValueError("The template is too large or malformed.")
    kind = slot.get("kind")
    if kind == "op":
        ops = slot.get("ops")
        if (
            not isinstance(ops, list)
            or not 1 <= len(ops) <= MAX_CHOICES
            or not all(isinstance(name, str) and _NAME.match(name) for name in ops)
        ):
            raise ValueError("An operator block names no operator.")
        args = slot.get("args")
        if not isinstance(args, list) or len(args) > 8:
            raise ValueError(f"{ops[0]} has malformed inputs.")
        options = slot.get("options") or {}
        if not isinstance(options, dict) or not all(
            isinstance(key, str) and _WORD.match(key) and _is_value(value)
            for key, value in options.items()
        ):
            raise ValueError(f"{ops[0]} has malformed options.")
        node: dict[str, Any] = {
            "kind": "op",
            "ops": list(dict.fromkeys(ops)),
            "args": [_load(child, depth + 1, count) for child in args],
        }
        if options:
            node["options"] = dict(options)
        return node
    if kind == "var":
        name, tag = slot.get("name"), slot.get("tag")
        if name not in VARIABLE_NAMES or tag not in TAGS:
            raise ValueError("A variable block is malformed.")
        return {"kind": "var", "name": name, "tag": tag}
    if kind == "data":
        name = slot.get("name")
        if name not in DATA_FIELDS and name not in GROUP_FIELDS:
            raise ValueError(f"{name!r} is not a data field a template can read.")
        return {"kind": "data", "name": name}
    if kind == "num":
        value = slot.get("value")
        if not _is_value(value) or isinstance(value, bool):
            raise ValueError("A number block holds no number.")
        return {"kind": "num", "value": value}
    raise ValueError("The template has a block of an unknown kind.")


def _is_value(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        # A bare word or a list of numbers, nothing else: the value names a behaviour BRAIN
        # publishes or the edges of its buckets, and it is written straight into an
        # expression.
        return bool(_WORD.match(value) or _NUMBER_LIST.match(value))
    return isinstance(value, int | float) and math.isfinite(value)


def _walk(slot: dict[str, Any] | None, path: str = "r") -> Iterator[tuple[str, dict[str, Any]]]:
    """Every block with its path (``r``, ``r.0``, ``r.0.1``), parents first."""
    if slot is None:
        return
    yield path, slot
    if slot["kind"] == "op":
        for index, child in enumerate(slot["args"]):
            yield from _walk(child, f"{path}.{index}")


def operators(doc: dict[str, Any]) -> set[str]:
    """Every operator a template names, choices included."""
    return {
        name for _, node in _walk(doc.get("root")) if node["kind"] == "op" for name in node["ops"]
    }


def missing(doc: dict[str, Any], table: dict[str, Block]) -> list[str]:
    """The operators a template names that the account does not have as blocks."""
    return sorted(operators(doc) - table.keys())


def problems(doc: dict[str, Any], table: dict[str, Block]) -> list[str]:
    """Why a template can't become a task yet, each said once. Empty when it can."""
    root = doc.get("root")
    if root is None:
        return ["The template is empty."]
    nodes = list(_walk(root))
    found: list[str] = []
    empty = sum(
        1 for _, node in nodes if node["kind"] == "op" for child in node["args"] if child is None
    )
    if empty:
        found.append("1 slot is empty." if empty == 1 else f"{empty} slots are empty.")
    if len(nodes) > MAX_NODES:
        found.append(f"A template holds at most {MAX_NODES} blocks.")
    if not any(node["kind"] == "var" and node["name"] == "FIELD" for _, node in nodes):
        found.append("Use FIELD so the template reads your datasets.")
    found.extend(_fits(root, "signal", "", 0, table))
    for _, node in nodes:
        if node["kind"] == "op":
            found.extend(_operator_problems(node, table))
    return list(dict.fromkeys(found))


def _operator_problems(node: dict[str, Any], table: dict[str, Block]) -> Iterator[str]:
    known = [table[name] for name in node["ops"] if name in table]
    for name in node["ops"]:
        if name not in table:
            yield f"{name} is not one of your operators."
    if not known:
        return
    first = known[0]
    for other in known[1:]:
        if other.inputs != first.inputs:
            yield (
                f"{first.name} and {other.name} take different inputs, so they can't share a block."
            )
    for key in node.get("options") or {}:
        for block in known:
            if key not in block.options:
                yield f"{block.name} has no {key} option."
    if len(node["args"]) != len(first.inputs):
        count = len(first.inputs)
        yield f"{first.name} takes {count} input{'' if count == 1 else 's'}."
        return
    for index, (socket, child) in enumerate(zip(first.inputs, node["args"], strict=True)):
        if child is not None:
            yield from _fits(child, socket, first.name, index, table)


def _fits(
    node: dict[str, Any], socket: str, owner: str, index: int, table: dict[str, Block]
) -> Iterator[str]:
    kind, name = node["kind"], node.get("name")
    block = table.get(node["ops"][0]) if kind == "op" else None
    group = (
        (kind == "var" and name == "GROUP")
        or (kind == "data" and name in GROUP_FIELDS)
        or (block is not None and block.output == "group")
    )
    if socket == "lookback":
        if not (
            (kind == "var" and name in LOOKBACK_VARIABLES)
            or (kind == "num" and _whole(node["value"]))
        ):
            yield f"{owner} needs a lookback in input {index + 1}."
    elif socket == "group":
        if not group:
            yield f"{owner} needs a group in input {index + 1}."
    elif kind == "var" and name in LOOKBACK_VARIABLES:
        yield f"{name} can only go in a lookback input."
    elif group:
        yield f"{name or node['ops'][0]} can only go in a group input."


def _whole(value: Any) -> bool:
    return not isinstance(value, bool) and float(value).is_integer() and value > 0


# --- what the search asks for -----------------------------------------------


@dataclass(frozen=True, slots=True)
class Used:
    """What a template asks the search for."""

    #: FIELD tags in order; the first is the field the first pass covers.
    fields: tuple[str, ...]
    #: ``(variable, tag)`` for every other variable, sorted.
    values: tuple[tuple[str, str], ...]
    #: ``(path, operators)`` for every choice block, parents first.
    choices: tuple[tuple[str, tuple[str, ...]], ...]
    #: Fixed data fields read (grouping fields are not checked per universe).
    data: tuple[str, ...]


def used(doc: dict[str, Any]) -> Used:
    fields: set[str] = set()
    values: set[tuple[str, str]] = set()
    data: set[str] = set()
    choices: list[tuple[str, tuple[str, ...]]] = []
    for path, node in _walk(doc.get("root")):
        if node["kind"] == "var":
            if node["name"] == "FIELD":
                fields.add(node["tag"])
            else:
                values.add((node["name"], node["tag"]))
        elif node["kind"] == "op" and len(node["ops"]) > 1:
            choices.append((path, tuple(node["ops"])))
        elif node["kind"] == "data" and node["name"] in DATA_FIELDS:
            data.add(node["name"])
    return Used(tuple(sorted(fields)), tuple(sorted(values)), tuple(choices), tuple(sorted(data)))


def field_key(tag: str, fields: tuple[str, ...]) -> str:
    """The first FIELD tag is ``field``, as in Search Lab, so the first pass covers it."""
    return "field" if fields and tag == fields[0] else f"field_{tag}"


def vector_key(key: str) -> str:
    return "vector_op" if key == "field" else f"vector_op_{key.removeprefix('field_')}"


def _tree(run: TemplateParams) -> dict[str, Any]:
    """The block tree a classic task was planned with; the typed editor's tasks have none."""
    if run.tree is None:
        raise ValueError("This task was not planned from blocks.")
    return run.tree


def suggest(trial: Any, run: TemplateParams, choices: dict[str, list[str]]) -> dict[str, Any]:
    """One point, asked define-by-run in a fixed order so every name keeps one distribution."""
    space, use = run.space, used(_tree(run))
    universes = list(space["universes"])
    universe = (
        universes[0] if len(universes) == 1 else trial.suggest_categorical("universe", universes)
    )
    params: dict[str, Any] = {"universe": universe}
    for tag in use.fields:
        key = field_key(tag, use.fields)
        params[key] = trial.suggest_categorical(f"{key}@{universe}", choices[universe])
        if space["fields"][params[key]] == "VECTOR":
            vector = vector_key(key)
            params[vector] = trial.suggest_categorical(vector, list(space["vector"]))
    for name, tag in use.values:
        key = f"{name}#{tag}"
        params[key] = trial.suggest_categorical(key, list(space["variables"][name]))
    for path, ops in use.choices:
        params[f"op@{path}"] = trial.suggest_categorical(f"op@{path}", list(ops))
    neutralizations = list(space["neutralizations"])
    params["neutralization"] = (
        neutralizations[0]
        if len(neutralizations) == 1
        else trial.suggest_categorical("neutralization", neutralizations)
    )
    return params


def render(doc: dict[str, Any], params: dict[str, Any]) -> str:
    """The Fast Expression for one point of the search."""
    return write(_written(doc["root"], "r", params, used(doc)))


def _written(node: dict[str, Any], path: str, params: dict[str, Any], use: Used) -> Node:
    kind = node["kind"]
    if kind == "op":
        name = node["ops"][0] if len(node["ops"]) == 1 else params[f"op@{path}"]
        args = tuple(
            _written(child, f"{path}.{index}", params, use)
            for index, child in enumerate(node["args"])
        )
        options = node.get("options") or {}
        if name in SYMBOLS and not options and len(args) == 2:
            return Node("binary", SYMBOLS[name], args)
        return Node("call", name, args, tuple((k, _literal(v)) for k, v in options.items()))
    if kind == "var":
        if node["name"] == "FIELD":
            key = field_key(node["tag"], use.fields)
            inner = Node("name", params[key])
            vector = params.get(vector_key(key))
            if vector:
                inner = Node("call", vector, (inner,))
            return inner
        value = params[f"{node['name']}#{node['tag']}"]
        return Node("name", str(value)) if node["name"] == "GROUP" else _literal(value)
    if kind == "data":
        return Node("name", node["name"])
    return _literal(node["value"])


def _literal(value: float | bool | str) -> Node:
    if isinstance(value, bool):
        return Node("name", "true" if value else "false")
    if isinstance(value, str):
        # Quoted, as the operator reference writes it, so the word can never be mistaken
        # for a data field of the same name.
        return Node("str", f'"{value}"')
    number = float(value)
    text = str(int(abs(number))) if number.is_integer() else repr(abs(number))
    return Node("unary", "-", (Node("num", text),)) if number < 0 else Node("num", text)


def skeleton(doc: dict[str, Any]) -> str:
    """The template with its names in, e.g. ``{ts_rank OR ts_zscore}(FIELD A, LOOKBACK A)``."""
    return write(_shaped(doc.get("root")))


def _shaped(slot: dict[str, Any] | None) -> Node:
    if slot is None:
        return Node("name", "?")
    kind = slot["kind"]
    if kind == "op":
        ops, options = slot["ops"], slot.get("options") or {}
        args = tuple(_shaped(child) for child in slot["args"])
        if len(ops) == 1 and ops[0] in SYMBOLS and not options and len(args) == 2:
            return Node("binary", SYMBOLS[ops[0]], args)
        name = ops[0] if len(ops) == 1 else "{" + " OR ".join(ops) + "}"
        return Node("call", name, args, tuple((k, _literal(v)) for k, v in options.items()))
    if kind == "var":
        return Node("name", f"{slot['name']} {slot['tag']}")
    if kind == "data":
        return Node("name", slot["name"])
    return _literal(slot["value"])


def request_for(params: dict[str, Any], run: TemplateParams) -> SimulationRequest:
    return SimulationRequest(
        settings=SimulationSettings(
            region=run.region,
            delay=run.delay,
            universe=params["universe"],
            neutralization=params["neutralization"],
            decay=run.decay,
            truncation=search.TRUNCATION,
        ),
        regular=render(_tree(run), params),
    )


def draw(
    trial: Any, run: TemplateParams, choices: dict[str, list[str]]
) -> tuple[dict[str, Any], SimulationRequest | None]:
    """One point and its simulation; none when two FIELD or two GROUP tags landed on one value."""
    params = suggest(trial, run, choices)
    use = used(_tree(run))
    fields = [params[field_key(tag, use.fields)] for tag in use.fields]
    groups = [params[f"GROUP#{tag}"] for name, tag in use.values if name == "GROUP"]
    if len(set(fields)) < len(fields) or len(set(groups)) < len(groups):
        return params, None
    return params, request_for(params, run)


# --- presets ----------------------------------------------------------------


def _op(names: str, *args: dict[str, Any] | None, **options: float | bool) -> dict[str, Any]:
    node: dict[str, Any] = {"kind": "op", "ops": names.split("|"), "args": list(args)}
    if options:
        node["options"] = options
    return node


def _var(name: str, tag: str = "A") -> dict[str, Any]:
    return {"kind": "var", "name": name, "tag": tag}


def _data(name: str) -> dict[str, Any]:
    return {"kind": "data", "name": name}


def _num(value: float) -> dict[str, Any]:
    return {"kind": "num", "value": value}


@dataclass(frozen=True, slots=True)
class Preset:
    slug: str
    name: str
    description: str
    root: dict[str, Any]
    #: Where the template comes from, e.g. ``AIRS``. Starred on its card.
    source: str | None = None

    @property
    def id(self) -> str:
        return f"preset:{self.slug}"

    @property
    def doc(self) -> dict[str, Any]:
        return {"version": VERSION, "root": self.root}


_F, _F2 = _var("FIELD"), _var("FIELD", "B")
_LB, _FAST, _SLOW, _G, _G2 = (
    _var("LOOKBACK"),
    _var("FAST_LOOKBACK"),
    _var("SLOW_LOOKBACK"),
    _var("GROUP"),
    _var("GROUP", "B"),
)
_COMPARE = "ts_rank|ts_zscore|ts_delta|ts_av_diff"

PRESETS: tuple[Preset, ...] = (
    Preset(
        "own_history",
        "Own History",
        "Where a field stands against its own past.",
        _op(_COMPARE, _F, _LB),
    ),
    Preset(
        "peer_history",
        "Peer History",
        "Its own-history score, ranked against its peers.",
        _op("group_rank", _op("ts_rank|ts_zscore", _F, _LB), _G),
    ),
    Preset(
        "smoothed_peer_score",
        "Smoothed Peer Score",
        "A score against peers, smoothed over time.",
        _op(
            "ts_mean|ts_decay_linear", _op("group_rank|group_zscore|group_neutralize", _F, _G), _LB
        ),
    ),
    Preset(
        "four_step_recipe",
        "Four-Step Recipe",
        "Clip outliers, compare over time, rank within peers, then smooth.",
        _op(
            "ts_decay_linear",
            _op("group_rank", _op(_COMPARE, _op("winsorize", _F, std=4), _SLOW), _G),
            _FAST,
        ),
    ),
    Preset(
        "fast_versus_slow",
        "Fast Versus Slow",
        "A short average against a long one.",
        _op("rank", _op("subtract", _op("ts_mean", _F, _FAST), _op("ts_mean", _F, _SLOW))),
    ),
    Preset(
        "fade_the_jump",
        "Fade the Jump",
        "Leans against a recent move, within peers.",
        _op("group_rank", _op("reverse", _op("ts_delta|ts_av_diff", _F, _FAST)), _G),
    ),
    Preset(
        "busy_days_only",
        "Busy Days Only",
        "Trades only while volume runs above its average.",
        _op(
            "trade_when",
            _op("greater", _data("volume"), _data("adv20")),
            _op("ts_rank|ts_zscore", _F, _LB),
            _num(-1),
        ),
    ),
    Preset(
        "against_the_price",
        "Against the Price",
        "Favours stocks whose field moves against their price.",
        _op("reverse", _op("ts_corr", _F, _data("close"), _SLOW)),
    ),
    Preset(
        "two_field_ratio",
        "Two-Field Ratio",
        "One field over another, compared over time and within peers.",
        _op("group_rank", _op("ts_zscore", _op("divide", _F, _F2), _SLOW), _G),
    ),
    Preset(
        "lean_on_trading_activity",
        "Lean on Trading Activity",
        "A centred rank within peers, leaning towards heavily traded stocks.",
        _op(
            "multiply",
            _op("subtract", _op("group_rank", _F, _G), _num(0.5)),
            _op("ts_rank", _data("volume"), _FAST),
        ),
    ),
    Preset(
        "rank_within_group_pairs",
        "Rank Within Group Pairs",
        "Its own-history rank, ranked against peers that share two groups.",
        _op(
            "group_rank",
            _op("ts_rank", _op("ts_backfill", _F, _num(21)), _LB),
            _op("densify", _op("group_cartesian_product", _G, _G2)),
        ),
        source="AIRS",
    ),
    Preset(
        "clipped_two_field_ratio",
        "Clipped Two-Field Ratio",
        "One field over another with outliers clipped, compared over time and within peers.",
        _op("group_rank", _op("ts_zscore", _op("winsorize", _op("divide", _F, _F2)), _LB), _G),
        source="AIRS",
    ),
    Preset(
        "normalized_peer_score",
        "Normalized Peer Score",
        "Its own-history score, scored within one group, normalized within another, then clipped.",
        _op(
            "winsorize",
            _op("group_normalize", _op("group_zscore", _op("ts_zscore", _F, _LB), _G), _G2),
            std=4,
        ),
        source="AIRS",
    ),
    Preset(
        "weighted_smoothed_field",
        "Weighted Smoothed Field",
        "A clipped, smoothed field, leaning towards stocks that rank high on a second field.",
        _op(
            "group_neutralize",
            _op(
                "multiply",
                _op(
                    "ts_decay_linear",
                    _op("ts_backfill", _op("winsorize", _F, std=4), _num(21)),
                    _LB,
                ),
                _op("add", _var("WEIGHT"), _op("signed_power", _op("rank", _F2), _var("POWER"))),
            ),
            _G,
        ),
        source="AIRS",
    ),
)
