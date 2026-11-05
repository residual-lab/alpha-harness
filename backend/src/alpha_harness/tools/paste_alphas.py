"""Paste Alphas: an LLM's output read back into simulations.

The output is expected in the shape Copy Alphas writes: ``Alpha Expression:``, the expression,
then ``Simulation Settings:`` and one ``Title: Value`` line per setting. It is read loosely,
since a model rarely keeps a format to the letter: Markdown bold, headings, bullets and code
fences are ignored, and a setting matches BRAIN's label or its value in any case.
"""

import json
import re
from itertools import batched
from typing import Any

from ..brain.platform import (
    DEFAULT_DECAY,
    DEFAULT_NAN_HANDLING,
    DEFAULT_NEUTRALIZATION,
    DEFAULT_PASTEURIZATION,
    DEFAULT_TRUNCATION,
    INSTRUMENT_TYPE,
    LANGUAGE,
    MAX_DECAY,
    TEST_PERIOD_PATTERN,
)
from ..brain.schemas import SimulationRequest, SimulationSettings
from ..brain.settings_schema import chosen_labels, resolve_options
from ..engine.packer import key_of
from ..labs import fastexpr

#: Every setting a pasted Alpha may give, in the order each is resolved: a market's universes
#: and neutralizations depend on its region and delay, so those two come first.
SETTINGS = (
    ("region", "Region"),
    ("delay", "Delay"),
    ("universe", "Universe"),
    ("language", "Language"),
    ("decay", "Decay"),
    ("truncation", "Truncation"),
    ("neutralization", "Neutralization"),
    ("pasteurization", "Pasteurization"),
    ("nanHandling", "NaN Handling"),
    ("maxTrade", "Max Trade"),
    ("maxPosition", "Max Position"),
    ("testPeriod", "Test Period"),
)
TITLES = dict(SETTINGS)
#: Nothing sensible stands in for these: without them there is no market to run in.
REQUIRED = ("region", "delay", "universe")
#: What a setting left out takes, as BRAIN's own defaults do. Test Period is left to
#: :class:`SimulationRequest`, which holds out the app's own.
DEFAULTS: dict[str, Any] = {
    "language": LANGUAGE,
    "decay": DEFAULT_DECAY,
    "truncation": DEFAULT_TRUNCATION,
    "neutralization": DEFAULT_NEUTRALIZATION,
    "pasteurization": DEFAULT_PASTEURIZATION,
    "nanHandling": DEFAULT_NAN_HANDLING,
    "maxTrade": "OFF",
    "maxPosition": "OFF",
}


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


#: A setting by its title or its BRAIN name, either spelled any way.
_SETTING_KEYS = {_key(t): n for n, t in SETTINGS} | {_key(n): n for n, _ in SETTINGS}
#: Markdown around a label: heading marks, quotes, bullets, numbering, emphasis.
_DECORATION = re.compile(r"^(?:[#>]+\s*|[-*+]\s+|\d+[.)]\s+)*")
#: A label is words only, so ``a ? region : b`` is never read as the Region setting.
_LABEL = re.compile(r"^[A-Za-z][A-Za-z /]*$")


def _label_line(raw: str) -> tuple[str, str] | None:
    """``(label, value)`` when the line is a heading or a ``Title: Value`` line, known or not.
    An unknown one, such as ``Description:``, still ends the expression above it."""
    line = _DECORATION.sub("", raw.strip()).replace("**", "").replace("__", "").strip()
    label, colon, value = line.partition(":")
    if not colon or not _LABEL.match(label.strip()):
        return None
    return _key(label), value.strip().strip("`").strip()


#: A ``Title: value`` pair for any setting, anywhere on a line, through Markdown's bold and
#: backticks: ``**Region:** `USA```, ``Region: USA``, ``maxTrade = ON``.
_PAIR = re.compile(
    r"(?<![A-Za-z])("
    + "|".join(
        sorted({r"\s*".join(t.split()) for _, t in SETTINGS} | {n for n, _ in SETTINGS}, key=len)[
            ::-1
        ]
    )
    + r")\s*\**\s*[:=]\s*\**\s*`?\s*([^|`*,;]+?)\s*`?\s*(?=[|`*,;]|$)",
    re.IGNORECASE,
)


def _pairs(raw: str) -> list[tuple[str, str]]:
    """Every setting written on a line, as ``(BRAIN name, value)``."""
    return [(_SETTING_KEYS[_key(m[1])], m[2].strip()) for m in _PAIR.finditer(raw)]


def _settings_line(raw: str, pairs: list[tuple[str, str]], after_heading: bool) -> bool:
    """Whether a line sets settings rather than mentions one: it starts with a setting or the
    Simulation Settings heading, follows that heading, or holds several. A rationale that says
    ``Decay: 10`` in passing is none of those."""
    if len(pairs) > 1 or (pairs and after_heading):
        return True
    found = _label_line(raw)
    return (
        bool(pairs)
        and found is not None
        and (found[0] in _SETTING_KEYS or found[0] == "simulationsettings")
    )


def _table_row(raw: str) -> tuple[str, str] | None:
    """``| Region | USA |``: a two-cell table row naming a setting."""
    cells = [c.strip().replace("**", "").strip("`").strip() for c in raw.split("|") if c.strip()]
    if raw.strip().startswith("|") and len(cells) == 2 and _key(cells[0]) in _SETTING_KEYS:
        return _SETTING_KEYS[_key(cells[0])], cells[1]
    return None


def parse(text: str) -> list[dict[str, Any]]:
    """Each Alpha in an LLM's output, with the settings written for it, however it is laid out.

    An Alpha is an ``Alpha Expression:`` label with the expression on it or under it, or a fenced
    code block: one waiting under a label fills it, any other starts the next Alpha, and one
    holding whole Alphas is read as its own output. Settings are read wherever a line sets
    them, the first value of each kept, for the Alpha in hand until a heading or a rule closes
    it. Settings with no open Alpha wait for the next one, and stand only if it writes none of
    its own: output that puts them above the code reads right, and prose before the first
    Alpha cannot override it. A code block nothing sets settings for is an example rather than
    an Alpha, and is left out.
    """
    drafts: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    reading = False
    fence: list[str] | None = None
    after_heading = False
    pending: dict[str, str] = {}

    def start(lines: list[str], *, fenced: bool) -> dict[str, Any]:
        draft = {"lines": lines, "given": {}, "held": dict(pending), "fenced": fenced}
        pending.clear()
        drafts.append(draft)
        return draft

    for raw in text.splitlines():
        if raw.strip().startswith("```"):
            if fence is None:
                fence = []
                continue
            labels = (_label_line(line) for line in fence)
            if any(found and found[0] == "alphaexpression" for found in labels):
                for whole in parse("\n".join(fence)):
                    current = start(whole["expression"].splitlines(), fenced=False)
                    current["given"] = whole["given"]
                current = None
            elif current is not None and reading and not current["lines"]:
                current["lines"] = fence
            else:
                current = start(fence, fenced=True)
            fence, reading = None, False
            continue
        if fence is not None:
            fence.append(raw.rstrip())
            continue
        label = _label_line(raw)
        if label is not None and label[0] == "alphaexpression":
            current = start([label[1]] if label[1] else [], fenced=False)
            reading, after_heading = True, False
            continue
        if raw.lstrip().startswith("#") and label is None:
            # A heading opens the next Alpha, unless it heads this one's settings.
            after_heading = "setting" in _key(raw)
            if not after_heading:
                current = None
            reading = False
            continue
        pairs = _pairs(raw)
        row = _table_row(raw)
        if row is not None:
            pairs = [row]
        if row or _settings_line(raw, pairs, after_heading):
            given = current["given"] if current is not None else None
            fresh = given is not None and any(name not in given for name, _ in pairs)
            target = given if fresh and given is not None else pending
            for name, value in pairs:
                target.setdefault(name, value)
            reading = False
            continue
        if label is not None:
            reading = False
            after_heading = label[0] == "simulationsettings" and not label[1]
            continue
        if raw.strip() in {"---", "***", "___"}:
            current, reading, after_heading = None, False, False
        elif current is not None and reading:
            current["lines"].append(raw.rstrip().strip("`"))
    for d in drafts:
        d["given"] = d["given"] or d["held"]
    return [
        {"expression": "\n".join(d["lines"]).strip(), "given": d["given"]}
        for d in drafts
        if d["given"] or not d["fenced"]
    ]


def _choice(
    options: dict[str, dict[str, Any]], name: str, value: str
) -> tuple[Any, list[Any]] | None:
    """BRAIN's value for ``value`` by value or label, and every value offered; ``None`` when
    BRAIN offers no choices for this setting here."""
    offered = options.get(name, {}).get("choices")
    if not offered:
        return None
    wanted = value.casefold()
    for c in offered:
        if wanted in {str(c.get("value")).casefold(), str(c.get("label")).casefold()}:
            return c.get("value"), []
    return None, [c.get("label") or c.get("value") for c in offered]


def resolve(schema: dict[str, Any], given: dict[str, str]) -> dict[str, Any]:
    """BRAIN settings for one pasted Alpha: what it gave, BRAIN's defaults for what it left
    out, and what stops it running."""
    settings: dict[str, Any] = {"instrumentType": INSTRUMENT_TYPE}
    defaulted: list[str] = []
    problems: list[str] = []
    for name, title in SETTINGS:
        value = given.get(name, "").strip()
        if not value:
            if name in REQUIRED:
                problems.append(f"No {title} given.")
            elif name in ("decay", "truncation"):
                settings[name] = DEFAULTS[name]
                defaulted.append(title)
            elif name in DEFAULTS:
                offered = resolve_options(schema, settings).get(name, {}).get("choices")
                # Left out where BRAIN does not offer the setting at all: Max Position is
                # not everywhere.
                if offered and DEFAULTS[name] in [c.get("value") for c in offered]:
                    settings[name] = DEFAULTS[name]
                    defaulted.append(title)
                elif offered:
                    problems.append(f"No {title} given, and BRAIN's default is not offered here.")
            continue
        if name == "decay":
            if not value.isdigit() or int(value) > MAX_DECAY:
                problems.append(f"Decay {value!r} is not a whole number from 0 to {MAX_DECAY}.")
            else:
                settings[name] = int(value)
            continue
        if name == "truncation":
            try:
                number = float(value.rstrip("%")) / (100 if value.endswith("%") else 1)
            except ValueError:
                number = -1.0
            if not 0 <= number <= 1:
                problems.append(f"Truncation {value!r} is not a number from 0 to 1.")
            else:
                settings[name] = number
            continue
        if name == "testPeriod":
            if re.match(TEST_PERIOD_PATTERN, value.upper()):
                settings[name] = value.upper()
            else:
                problems.append(f"Test Period {value!r} is not like P2Y0M0D.")
            continue
        found = _choice(resolve_options(schema, settings), name, value)
        if found is None:
            # Its parent was refused, which is already said; or BRAIN has no list for it.
            if name in REQUIRED and not problems:
                problems.append(f"BRAIN offers no {title} choices here.")
            continue
        chosen, offered = found
        if chosen is None:
            where = f" in {settings.get('region')} delay {settings.get('delay')}"
            problems.append(
                f"{title} {value!r} is not offered"
                + (where if name not in ("region", "delay") else "")
                + f". BRAIN offers {', '.join(map(str, offered))}."
            )
        else:
            settings[name] = chosen
    if settings.get("maxTrade") == "ON" and settings.get("maxPosition") == "ON":
        problems.append("Max Trade and Max Position cannot both be On.")
    return {"settings": settings, "defaulted": defaulted, "problems": problems}


def fields_of(expression: str) -> list[str]:
    """The data fields an expression reads, grouping fields aside; none if it can't be read,
    which BRAIN will say when it runs."""
    try:
        return fastexpr.data_fields(fastexpr.parse(expression))
    except fastexpr.ParseError:
        return []


def request_of(expression: str, settings: dict[str, Any]) -> SimulationRequest:
    return SimulationRequest(
        settings=SimulationSettings.model_validate(settings), regular=expression
    )


def plan(schema: dict[str, Any], text: str) -> dict[str, Any]:
    """Every Alpha in the pasted text, resolved, with the simulations it would queue."""
    alphas: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    requests: list[SimulationRequest] = []
    for number, draft in enumerate(parse(text), start=1):
        found = resolve(schema, draft["given"])
        problems = found["problems"]
        if not draft["expression"]:
            problems.insert(0, "No expression under Alpha Expression.")
        duplicate = None
        if not problems:
            request = request_of(draft["expression"], found["settings"])
            identity = json.dumps(
                [request.regular, request.settings.model_dump(by_alias=True, exclude_none=True)],
                sort_keys=True,
            )
            duplicate = seen.setdefault(identity, number)
            if duplicate == number:
                duplicate = None
                requests.append(request)
        alphas.append(
            {
                "number": number,
                "expression": draft["expression"],
                "settings": found["settings"],
                "labels": chosen_labels(schema, found["settings"]),
                "defaulted": found["defaulted"],
                "problems": problems,
                "duplicate_of": duplicate,
            }
        )
    queued = ordered(requests)
    return {"alphas": alphas, "requests": queued, "batches": len(_chunks(queued))}


def _chunks(requests: list[SimulationRequest]) -> list[tuple[SimulationRequest, ...]]:
    """The Multi-Simulations these requests fill: every child shares BRAIN's batch key."""
    groups: dict[Any, list[SimulationRequest]] = {}
    for request in requests:
        key = key_of(request.model_dump(by_alias=True, exclude_none=True))
        groups.setdefault(key, []).append(request)
    return [
        chunk
        for key, members in groups.items()
        for chunk in batched(members, key.max_batch, strict=False)
    ]


def ordered(requests: list[SimulationRequest]) -> list[SimulationRequest]:
    """The order to send them in for the soonest finish.

    Batched by BRAIN's batch key so every Multi-Simulation is as full as it can be, then the
    slowest first: a region-agnostic or GLB batch started last would leave every other core
    idle while it finishes. Part-full batches go last, as the Settings Sampler sends them: the
    engine reads the queue a window at a time, and a short one in the middle splits the
    window's full ones in two.
    """

    def cost(chunk: tuple[SimulationRequest, ...]) -> int:
        return key_of(chunk[0].model_dump(by_alias=True, exclude_none=True)).cost

    def full(chunk: tuple[SimulationRequest, ...]) -> bool:
        key = key_of(chunk[0].model_dump(by_alias=True, exclude_none=True))
        return len(chunk) == key.max_batch

    chunks = sorted(_chunks(requests), key=lambda c: (not full(c), -cost(c), -len(c)))
    return [request for chunk in chunks for request in chunk]
