"""Settings BRAIN refused for a task, learned from its refusals and kept per market.

BRAIN's 400 names the setting it refused (``{"settings": {"testPeriod": [...]}}``) but not the
market it refused it in, and many settings are fine in one market and refused in another. So a
refusal is kept as ``(region, delay, universe, setting, value)``, read from the refused
simulation's own settings, and only a simulation of that market carrying that value is held
back: a setting refused in EUR stays available in USA.

The engine holds back the task's queued simulations the moment a batch is refused; the labs
hold back what they would draw or send next, so a task refused across the board stops after
its first batch rather than spending its whole run on refusals.
"""

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

#: How the engine's message for a refused batch begins (engine.slots).
REFUSED = "BRAIN rejected these settings"

#: ``(region, delay, universe, setting, value as JSON)``.
type Refusal = tuple[str, int, str, str, str]

_NAMED = re.compile(r"\bsettings\.(\w+)")


def _market(settings: Mapping[str, Any]) -> tuple[str, int, str]:
    return (
        str(settings.get("region") or ""),
        int(settings.get("delay") or 0),
        str(settings.get("universe") or ""),
    )


def _value(settings: Mapping[str, Any], name: str) -> str:
    return json.dumps(settings.get(name), sort_keys=True)


def _keys(names: Iterable[str], settings: Mapping[str, Any]) -> set[Refusal]:
    market = _market(settings)
    return {(*market, name, _value(settings, name)) for name in names if name in settings}


def from_fields(fields: Any, settings: Mapping[str, Any]) -> set[Refusal]:
    """The refusals a 400 body (or one child's entry of it) makes of a simulation's settings.

    Only ``settings`` entries count: an expression BRAIN cannot read is that expression's
    fault, not its market's.
    """
    named = fields.get("settings") if isinstance(fields, dict) else None
    return _keys(named, settings) if isinstance(named, dict) else set()


def from_message(message: str | None, settings: Mapping[str, Any] | None) -> set[Refusal]:
    """The refusals a failed trial's message makes of its settings: the message names each
    refused setting as ``settings.<name>``."""
    if not message or not message.startswith(REFUSED) or not settings:
        return set()
    return _keys(dict.fromkeys(_NAMED.findall(message)), settings)


def learned(rows: Iterable[tuple[str | None, Mapping[str, Any] | None]]) -> set[Refusal]:
    """Every refusal among failed trials' ``(message, settings)``."""
    found: set[Refusal] = set()
    for message, settings in rows:
        found |= from_message(message, settings)
    return found


def repeated(settings: Mapping[str, Any], known: set[Refusal]) -> Refusal | None:
    """The refusal a simulation with these settings would repeat, if any."""
    if not known:
        return None
    market = _market(settings)
    for refusal in known:
        if refusal[:3] == market and _value(settings, refusal[3]) == refusal[4]:
            return refusal
    return None


def describe(refusal: Refusal) -> str:
    """One refusal in words: ``testPeriod "P6Y3M0D" in USA D1 TOP3000``."""
    region, delay, universe, name, value = refusal
    return f"{name} {value} in {region} D{delay} {universe}"


def held_back(refusal: Refusal) -> str:
    """The message of a simulation held back for repeating ``refusal``."""
    return f"Not sent: BRAIN refused {describe(refusal)} earlier in this task."


if __name__ == "__main__":
    import sys

    usa = {"region": "USA", "delay": 1, "universe": "TOP3000", "testPeriod": "P6Y3M0D"}
    eur = {**usa, "region": "EUR", "universe": "TOP2500"}
    body = {"settings": {"testPeriod": ["Must be less than or equal to P6Y0M0D"]}}
    message = f"{REFUSED} — settings.testPeriod: Must be less than or equal to P6Y0M0D"
    known = from_fields(body, usa)
    checks = {
        "the named setting, in its own market": known
        == {("USA", 1, "TOP3000", "testPeriod", '"P6Y3M0D"')},
        "read back from a trial's message": learned([(message, usa)]) == known,
        "repeated in the same market": repeated({**usa, "neutralization": "RAM"}, known)
        is not None,
        "not in another market": repeated(eur, known) is None,
        "not with another value": repeated({**usa, "testPeriod": "P2Y0M0D"}, known) is None,
        "an expression's fault is not a market's": from_fields({"regular": ["bad"]}, usa) == set(),
        "other failures teach nothing": learned([("Could not read the alpha back", usa)]) == set(),
        "worded": held_back(next(iter(known)))
        == 'Not sent: BRAIN refused testPeriod "P6Y3M0D" in USA D1 TOP3000 earlier in this task.',
    }
    if failed := [name for name, ok in checks.items() if not ok]:
        raise SystemExit(f"refusals self-check failed: {'; '.join(failed)}")
    sys.stdout.write(f"{len(checks)} cases as expected\n")
