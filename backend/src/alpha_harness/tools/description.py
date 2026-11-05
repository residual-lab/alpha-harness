"""The description BRAIN requires before it takes a Power Pool Alpha, written by a model.

BRAIN's own client accepts one only in its template: each heading starting a line, text after
every one, the whole at least 100 characters. The model is asked for the three parts alone and
the template is put together here, so its shape never rests on the model.
"""

import re
from typing import TYPE_CHECKING, Any

from ..brain.platform import POWER_POOL_DESCRIPTION_HEADINGS
from ..labs.fastexpr import walk
from ..llm.text import loads_or

if TYPE_CHECKING:
    from ..labs.fastexpr import Node

#: The parts the model returns, in the template's order.
PARTS = ("idea", "data", "operators")

#: The fewest characters each part may have.
MIN_PART = 100

#: BRAIN's category for add, divide, abs and the like: a model knows them without being told.
ARITHMETIC = "Arithmetic"

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {part: {"type": "string"} for part in PARTS},
    "required": list(PARTS),
}

#: BRAIN's operator for each symbol Fast Expression writes between its inputs.
SYMBOLS = {
    ("binary", "+"): "add",
    ("binary", "-"): "subtract",
    ("binary", "*"): "multiply",
    ("binary", "/"): "divide",
    ("binary", "^"): "power",
    ("binary", ">"): "greater",
    ("binary", ">="): "greater_equal",
    ("binary", "<"): "less",
    ("binary", "<="): "less_equal",
    ("binary", "=="): "equal",
    ("binary", "!="): "not_equal",
    ("binary", "&&"): "and",
    ("binary", "||"): "or",
    ("unary", "-"): "reverse",
    ("unary", "!"): "not",
    ("ternary", ""): "if_else",
}

#: The template as BRAIN's client checks it, which also reads one a model wrote as plain text.
_TEMPLATE = re.compile(
    r"^\s*idea:\s+(?P<idea>.+?)\n\s*rationale for data used:\s+(?P<data>.+?)\n"
    r"\s*rationale for operators used:\s+(?P<operators>.+)",
    re.IGNORECASE | re.DOTALL,
)


def operators_in(tree: Node) -> list[str]:
    """Every operator the expression applies, by BRAIN's name, once each in order of appearance.

    Symbols included: ``x / y`` is BRAIN's ``divide``. A minus sign on a number is the number.
    """
    found: list[str] = []
    for _, node in walk(tree):
        if node.kind == "call":
            found.append(node.value)
        elif (node.kind, node.value) in SYMBOLS and not (
            node.kind == "unary" and node.args[0].kind == "num"
        ):
            found.append(SYMBOLS[node.kind, node.value])
    return list(dict.fromkeys(found))


def message(expression: str, fields: list[dict[str, Any]], operators: list[dict[str, Any]]) -> str:
    """What the model is shown: the expression, then only its own fields and operators, each
    with BRAIN's description, then the template to fill. Arithmetic operators are left out."""
    operators = [o for o in operators if o.get("category") != ARITHMETIC]
    lines = [
        f"{f['name']} ({f.get('type') or 'UNKNOWN'}): {f.get('description') or 'No description'}"
        for f in fields
    ] + [
        f"{o.get('definition') or o['name']}: {o.get('description') or 'No description'}"
        for o in operators
    ]
    template = "\n".join(f"{heading}: " for heading in POWER_POOL_DESCRIPTION_HEADINGS)
    return (
        f"{expression.strip()}\n\n"
        + "\n".join(lines)
        + "\n\nAlpha Description following the given Template:\n"
        + f"```\n{template}\n```"
    )


def read(answer: str) -> str | None:
    """The description in BRAIN's template, from the JSON asked for or, failing that, from a
    template the model wrote out itself. ``None`` when a part is missing."""
    payload = loads_or(answer)
    parts = [payload.get(part) for part in PARTS]
    if not all(isinstance(p, str) and p.strip() for p in parts):
        found = _TEMPLATE.search(answer)
        if found is None:
            return None
        parts = [found.group(part) for part in PARTS]
    texts = [
        _plain(heading, str(part))
        for heading, part in zip(POWER_POOL_DESCRIPTION_HEADINGS, parts, strict=True)
    ]
    if not all(texts):
        return None
    return "\n".join(
        f"{heading}: {text}"
        for heading, text in zip(POWER_POOL_DESCRIPTION_HEADINGS, texts, strict=True)
    )


def _plain(heading: str, text: str) -> str:
    """One line, as the template is laid out, and without its heading if the model wrote that
    too: it would read twice."""
    text = " ".join(text.split())
    if text.lower().startswith(heading.lower()):
        text = text[len(heading) :].lstrip(" :")
    return text


def short(written: str) -> list[str]:
    """The headings of a written description whose text has fewer than :data:`MIN_PART`
    characters."""
    found = _TEMPLATE.search(written)
    return [
        heading
        for heading, part in zip(POWER_POOL_DESCRIPTION_HEADINGS, PARTS, strict=True)
        if found is None or len(found.group(part).strip()) < MIN_PART
    ]
