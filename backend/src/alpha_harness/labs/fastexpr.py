"""Fast Expression, parsed into a tree and written back.

Evolution Lab breeds from Alphas the consultant already has, so the grammar covers whatever
BRAIN accepts, not only the shapes this application writes. ``parse(render(tree)) == tree``
for every tree :func:`parse` returns, which is what makes changing a tree and writing it
back safe.

Precedence, loosest first: ternary, ``||``, ``&&``, comparisons, ``+ -``, ``* /``, ``^``
(right-associative), unary ``-``/``!``, calls.
"""

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from ..brain.platform import GROUPING_FIELDS, UNCOUNTED_OPERATORS

if TYPE_CHECKING:
    import random
    from collections.abc import Set as AbstractSet

COMPARISONS = ("==", "!=", "<", "<=", ">", ">=")


class ParseError(ValueError):
    """Not an expression this grammar can read."""


@dataclass(frozen=True, slots=True)
class Node:
    kind: str  # num | str | name | call | unary | binary | ternary | assign | seq
    value: str = ""
    args: tuple[Node, ...] = ()
    kwargs: tuple[tuple[str, Node], ...] = ()


@dataclass(frozen=True, slots=True)
class OperatorInfo:
    name: str
    category: str
    #: Positional inputs without a default.
    required: int
    #: Most positional inputs accepted; ``None`` when it takes any number.
    maximum: int | None
    #: Each parameter as the definition writes it, e.g. ``("x", "d", "constant = 0")``.
    params: tuple[str, ...] = ()


_SIGNATURE = re.compile(r"([A-Za-z_]\w*)\s*\(")
_QUOTES = "\"'“”"


def operator_table(
    operators: list[dict[str, Any]], scope: str = "REGULAR"
) -> dict[str, OperatorInfo]:
    """Arity read from each operator's published definition, e.g. ``ts_rank(x, d, constant = 0)``.

    Only operators whose published ``scope`` includes ``scope`` are kept, so a COMBO-only
    operator is never swapped into a regular Alpha. Only the first signature counts and
    commas inside quotes are not separators; an operator whose definition cannot be read is
    left out, so it never passes validation.
    """
    table: dict[str, OperatorInfo] = {}
    for operator in operators:
        name, definition = operator.get("name"), operator.get("definition")
        if not isinstance(name, str) or not isinstance(definition, str):
            continue
        scopes = operator.get("scope")
        if isinstance(scopes, list) and scopes and scope not in scopes:
            continue
        match = _SIGNATURE.search(definition)
        if match is None or match.group(1) != name:
            continue
        params = _signature_params(definition, match.end())
        if params is None:
            continue
        variadic = any(".." in p for p in params)
        table[name] = OperatorInfo(
            name=name,
            category=str(operator.get("category") or ""),
            required=sum(1 for p in params if "=" not in p and ".." not in p),
            maximum=None if variadic else len(params),
            params=tuple(params),
        )
    return table


def _signature_params(definition: str, start: int) -> list[str] | None:
    params: list[str] = []
    depth, quoted, current = 0, False, []
    for char in definition[start:]:
        if char in _QUOTES:
            quoted = not quoted
        elif not quoted and char == "(":
            depth += 1
        elif not quoted and char == ")":
            if depth == 0:
                tail = "".join(current).strip()
                return [*params, tail] if tail or params else params
            depth -= 1
        elif not quoted and depth == 0 and char == ",":
            params.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    return None


# -- reading ---------------------------------------------------------------

#: Template Lab writes two names BRAIN never sees: a ``$variable``, and ``...`` where a
#: template's signal goes.
_TOKEN = re.compile(
    r"""\s*(?:
        (?P<num>(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)
      | (?P<str>"[^"]*"|'[^']*')
      | (?P<name>\$?[A-Za-z_]\w*|\.\.\.)
      | (?P<op>&&|\|\||==|!=|<=|>=|[-+*/^<>!?:(),=;])
    )""",
    re.VERBOSE,
)

_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
#: ``//`` is Fast Expression's own line comment and ``#`` is what a model writing one reaches
#: for; BRAIN accepts both, and Alphas in the vault carry both.
_LINE_COMMENT = re.compile(r"(?://|#)[^\n]*")

_LEVELS: tuple[tuple[str, ...], ...] = (("||",), ("&&",), COMPARISONS, ("+", "-"), ("*", "/"))


def tokenize(text: str) -> list[tuple[str, str, int]]:
    tokens: list[tuple[str, str, int]] = []
    # Comments are part of Fast Expression; blanked, not removed, so positions hold and a
    # parse error still points at the character the reader can see.
    blank = lambda m: " " * len(m.group())  # noqa: E731
    text = _LINE_COMMENT.sub(blank, _COMMENT.sub(blank, text)).rstrip()
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None or match.lastgroup is None:
            raise ParseError(f"Cannot read {text[position : position + 12]!r} at {position}.")
        kind = match.lastgroup
        tokens.append((kind, match.group(kind), match.start(kind)))
        position = match.end()
    return tokens


def parse(text: str) -> Node:
    """A tree for the expression, or :class:`ParseError` saying where it stopped making sense."""
    return _Parser(tokenize(text)).program()


class _Parser:
    def __init__(self, tokens: list[tuple[str, str, int]]) -> None:
        self.tokens = tokens
        self.index = 0

    def peek(self, offset: int = 0) -> tuple[str, str, int] | None:
        at = self.index + offset
        return self.tokens[at] if at < len(self.tokens) else None

    def at(self, symbol: str, offset: int = 0) -> bool:
        token = self.peek(offset)
        return token is not None and token[0] == "op" and token[1] == symbol

    def take(self) -> tuple[str, str, int]:
        token = self.peek()
        if token is None:
            raise ParseError("The expression ends too early.")
        self.index += 1
        return token

    def expect(self, symbol: str) -> None:
        token = self.take()
        if token[0] != "op" or token[1] != symbol:
            raise ParseError(f"Expected {symbol!r} at {token[2]}, found {token[1]!r}.")

    def program(self) -> Node:
        statements: list[Node] = []
        while self.peek() is not None:
            if self.at(";"):
                self.take()
                continue
            statements.append(self.statement())
            if self.peek() is not None:
                self.expect(";")
        if not statements:
            raise ParseError("The expression is empty.")
        return statements[0] if len(statements) == 1 else Node("seq", args=tuple(statements))

    def statement(self) -> Node:
        token = self.peek()
        if token is not None and token[0] == "name" and self.at("=", 1):
            self.index += 2
            return Node("assign", token[1], (self.expr(),))
        return self.expr()

    def expr(self) -> Node:
        condition = self.binary(0)
        if not self.at("?"):
            return condition
        self.take()
        yes = self.expr()
        self.expect(":")
        return Node("ternary", args=(condition, yes, self.expr()))

    def binary(self, level: int) -> Node:
        if level == len(_LEVELS):
            return self.power()
        left = self.binary(level + 1)
        while (
            (token := self.peek()) is not None and token[0] == "op" and token[1] in _LEVELS[level]
        ):
            self.take()
            left = Node("binary", token[1], (left, self.binary(level + 1)))
        return left

    def power(self) -> Node:
        base = self.unary()
        if self.at("^"):
            self.take()
            return Node("binary", "^", (base, self.power()))
        return base

    def unary(self) -> Node:
        if self.at("-") or self.at("!"):
            return Node("unary", self.take()[1], (self.unary(),))
        return self.primary()

    def primary(self) -> Node:
        kind, text, position = self.take()
        if kind in ("num", "str"):
            return Node(kind, text)
        if kind == "name":
            return self.call(text) if self.at("(") else Node("name", text)
        if text == "(":
            inner = self.expr()
            self.expect(")")
            return inner
        raise ParseError(f"Unexpected {text!r} at {position}.")

    def call(self, name: str) -> Node:
        self.expect("(")
        args: list[Node] = []
        kwargs: list[tuple[str, Node]] = []
        if self.at(")"):
            self.take()
            return Node("call", name)
        while True:
            token = self.peek()
            if token is not None and token[0] == "name" and self.at("=", 1):
                self.index += 2
                kwargs.append((token[1], self.expr()))
            else:
                args.append(self.expr())
            if self.at(","):
                self.take()
                continue
            self.expect(")")
            return Node("call", name, tuple(args), tuple(kwargs))


# -- writing ---------------------------------------------------------------

_PRECEDENCE = {
    "||": 2,
    "&&": 3,
    **dict.fromkeys(COMPARISONS, 4),
    "+": 5,
    "-": 5,
    "*": 6,
    "/": 6,
    "^": 7,
}
_TERNARY, _UNARY, _ATOM = 1, 8, 9


def _precedence(node: Node) -> int:
    if node.kind == "binary":
        return _PRECEDENCE[node.value]
    if node.kind == "unary":
        return _UNARY
    if node.kind == "ternary":
        return _TERNARY
    if node.kind in ("assign", "seq"):
        return 0
    return _ATOM


def _wrapped(node: Node, parenthesise: bool) -> str:
    text = render(node)
    return f"({text})" if parenthesise else text


def render(node: Node) -> str:
    match node.kind:
        case "num" | "str" | "name":
            return node.value
        case "call":
            parts = [render(a) for a in node.args] + [f"{k}={render(v)}" for k, v in node.kwargs]
            return f"{node.value}({', '.join(parts)})"
        case "unary":
            child = node.args[0]
            return node.value + _wrapped(child, _precedence(child) < _UNARY)
        case "binary":
            own = _PRECEDENCE[node.value]
            left, right = node.args
            if node.value == "^":  # right-associative
                left_parens, right_parens = _precedence(left) <= own, _precedence(right) < own
            else:
                left_parens, right_parens = _precedence(left) < own, _precedence(right) <= own
            return f"{_wrapped(left, left_parens)} {node.value} {_wrapped(right, right_parens)}"
        case "ternary":
            condition, yes, no = node.args
            test = _wrapped(condition, _precedence(condition) <= _TERNARY)
            return f"{test} ? {render(yes)} : {render(no)}"
        case "assign":
            return f"{node.value} = {render(node.args[0])}"
        case "seq":
            return "; ".join(render(s) for s in node.args)
        case _:
            raise ValueError(f"Unknown node kind {node.kind!r}")


# -- walking ---------------------------------------------------------------

Path = tuple[int, ...]


def children(node: Node) -> list[Node]:
    return [*node.args, *(value for _, value in node.kwargs)]


def walk(node: Node, path: Path = ()) -> list[tuple[Path, Node]]:
    """Every node with the route to it. Keyword values follow positional arguments."""
    found = [(path, node)]
    for index, child in enumerate(children(node)):
        found.extend(walk(child, (*path, index)))
    return found


def walk_with_parents(
    node: Node, path: Path = (), parent: Node | None = None
) -> list[tuple[Path, Node, Node | None]]:
    """:func:`walk` with each node's parent beside it, ``None`` at the root: found on the way
    down rather than by descending from the root again for every node."""
    found = [(path, node, parent)]
    for index, child in enumerate(children(node)):
        found.extend(walk_with_parents(child, (*path, index), node))
    return found


def node_at(node: Node, path: Path) -> Node:
    for index in path:
        node = children(node)[index]
    return node


def relabel(node: Node, values: dict[Path, str]) -> Node:
    """``node`` with the value at each of ``values``' paths replaced, in one pass. The shape
    never changes, so this is :func:`replace_at` once per path, without rebuilding the way down
    from the root again for each."""
    if not values:
        return node
    own = values.get(())
    below: dict[int, dict[Path, str]] = {}
    for path, value in values.items():
        if path:
            below.setdefault(path[0], {})[path[1:]] = value
    if not below:
        return replace(node, value=own) if own is not None else node
    count = len(node.args)
    return replace(
        node,
        value=node.value if own is None else own,
        args=tuple(relabel(a, below[i]) if i in below else a for i, a in enumerate(node.args)),
        kwargs=tuple(
            (key, relabel(v, below[count + i])) if count + i in below else (key, v)
            for i, (key, v) in enumerate(node.kwargs)
        ),
    )


def replace_at(node: Node, path: Path, new: Node) -> Node:
    if not path:
        return new
    index, rest = path[0], path[1:]
    if index < len(node.args):
        args = list(node.args)
        args[index] = replace_at(args[index], rest, new)
        return replace(node, args=tuple(args))
    kwargs = list(node.kwargs)
    key, value = kwargs[index - len(node.args)]
    kwargs[index - len(node.args)] = (key, replace_at(value, rest, new))
    return replace(node, kwargs=tuple(kwargs))


def data_fields(tree: Node, *, grouping: bool = False) -> list[str]:
    """The data fields an expression reads, the way BRAIN counts them.

    Names that are not data: anything assigned earlier in the expression, a keyword value
    (``driver=gaussian`` names an option), a grouping field, and the literals. ``grouping``
    keeps the grouping fields: they do not count, but they still have to exist where the
    expression runs, and some (``currency``, ``split``) are missing from some markets.
    """
    nodes = walk_with_parents(tree)
    assigned = {n.value for _, n, _ in nodes if n.kind == "assign"}
    # In order of first appearance, left to right, as the reader wrote them.
    return list(
        dict.fromkeys(
            n.value
            for path, n, parent in nodes
            if n.kind == "name"
            and not (parent is not None and path[-1] >= len(parent.args))
            and n.value not in assigned
            and (grouping or n.value not in GROUPING_FIELDS)
            and n.value.lower() not in ("true", "false", "nan")
        )
    )


def operator_count(node: Node) -> int:
    """Power Pool's count: every call and every arithmetic or logical operator, backfills excepted.

    A negative number is a constant, not an operator: BRAIN's own count agrees with this on
    every one of 15,000 distinct expressions it has scored, counting backfills aside.
    """
    negative = node.kind == "unary" and node.value == "-" and node.args[0].kind == "num"
    own = int(
        (node.kind == "call" and node.value not in UNCOUNTED_OPERATORS)
        or (node.kind in ("unary", "binary", "ternary") and not negative)
    )
    return own + sum(operator_count(child) for child in children(node))


def protected(tree: Node, path: Path) -> bool:
    """A grouping input or a keyword value: slots that must not receive a signal."""
    return guarded(node_at(tree, path[:-1]) if path else None, path)


def guarded(parent: Node | None, path: Path) -> bool:
    """:func:`protected`, for a node whose ``parent`` is already known."""
    if parent is None:
        return False
    if path[-1] >= len(parent.args):
        return True
    return parent.kind == "call" and parent.value.startswith("group_") and path[-1] >= 1


# -- changing --------------------------------------------------------------


def crossover(rng: random.Random, left: Node, right: Node) -> Node | None:
    """Graft a call from one parent in place of a call inside the other.

    The left root is never replaced (that would just be the other parent), and grouping
    and keyword slots are never exchanged.
    """
    return graft(rng, left, graft_sites(left), grafts(right))


def graft_sites(tree: Node) -> list[Path]:
    """Where :func:`crossover` may graft into ``tree``: any call but the root, outside the
    grouping and keyword slots."""
    return [
        p for p, n, up in walk_with_parents(tree) if p and n.kind == "call" and not guarded(up, p)
    ]


def grafts(tree: Node) -> list[Node]:
    """The calls :func:`crossover` may take from ``tree``."""
    return [n for p, n, up in walk_with_parents(tree) if n.kind == "call" and not guarded(up, p)]


def graft(rng: random.Random, left: Node, sites: list[Path], taken: list[Node]) -> Node | None:
    """:func:`crossover` with both lists already read off the parents."""
    if not sites or not taken:
        return None
    child = replace_at(left, rng.choice(sites), rng.choice(taken))
    return None if child == left else child


def validate(
    tree: Node, table: dict[str, OperatorInfo], known_names: AbstractSet[str]
) -> list[str]:
    """Why BRAIN would reject this tree, or nothing."""
    problems: list[str] = []
    nodes = walk_with_parents(tree)
    assigned = {node.value for _, node, _ in nodes if node.kind == "assign"}
    for path, node, parent in nodes:
        if node.kind == "call":
            info = table.get(node.value)
            if info is None:
                problems.append(f"{node.value} is not an operator on BRAIN.")
                continue
            count = len(node.args)
            supplied = count + len(node.kwargs)
            # A required input may be named rather than placed: kth_element(x, 15, k = 1).
            names = [p.split("=")[0].strip() for p in info.params]
            given = {*names[:count], *(key for key, _ in node.kwargs)}
            required = [
                n for p, n in zip(info.params, names, strict=True) if "=" not in p and ".." not in p
            ]
            short = any(n not in given for n in required) if info.params else count < info.required
            if short or (info.maximum is not None and supplied > info.maximum):
                problems.append(f"{node.value} does not take {supplied} inputs.")
        elif (
            node.kind == "name"
            # A keyword value names an option (``driver=gaussian``), not a data field.
            and not (parent is not None and path[-1] >= len(parent.args))
            and node.value not in known_names
            and node.value not in assigned
            and node.value.lower() not in ("true", "false", "nan")
        ):
            problems.append(f"{node.value} is not a data field here.")
    return problems
