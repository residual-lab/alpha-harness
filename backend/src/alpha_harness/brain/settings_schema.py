"""Resolving ``OPTIONS /simulations`` into usable settings options.

The schema returned by the platform is *recursive*. A field's ``choices`` is either a flat
list::

    {"choices": [{"value": "EQUITY", "label": "Equity"}]}

or a dependency node keyed by the field it depends on::

    {"choices": {"instrumentType": {"EQUITY": {"region": {"USA": [ ...universes... ]}}}}}

So the legal universes depend on the region, which depends on the instrument type.
Hardcoding any of it would show users options their account cannot run.
"""

from typing import Any


def resolve_choices(node: Any, settings: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Resolve one field's ``choices`` against the settings chosen so far.

    Returns ``None`` when the field has no enumerated choices (it is free-form), or when
    a dependency has not been chosen yet — the caller should treat that as "cannot offer
    options until you pick the parent field".
    """
    if node is None:
        return None
    if isinstance(node, list):
        return [c for c in node if isinstance(c, dict)]
    if not isinstance(node, dict):
        return None

    # A dependency node: exactly one key, naming the field it depends on.
    for parent_field, branches in node.items():
        if not isinstance(branches, dict):
            continue
        chosen = settings.get(parent_field)
        if chosen is None:
            return None
        # Values arrive as ints (delay) or strings; match on both spellings.
        subtree = branches.get(chosen)
        if subtree is None:
            subtree = branches.get(str(chosen))
        if subtree is None:
            return None
        return resolve_choices(subtree, settings)
    return None


def dependencies(node: Any, found: list[str] | None = None) -> list[str]:
    """Which fields a field's choices depend on, outermost first."""
    found = found if found is not None else []
    if not isinstance(node, dict):
        return found
    for parent_field, branches in node.items():
        if not isinstance(branches, dict):
            continue
        found.append(parent_field)
        first = next(iter(branches.values()), None)
        return dependencies(first, found)
    return found


def resolve_options(
    schema: dict[str, Any], settings: dict[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    """Flatten the whole schema against a partial settings dict.

    Returns one entry per settings field describing its type, label, requiredness, the
    currently valid choices, and what it depends on — everything the form needs.
    """
    settings = settings or {}
    resolved: dict[str, dict[str, Any]] = {}

    for name, node in schema.items():
        if not isinstance(node, dict):
            continue
        raw_choices = node.get("choices")
        choices = resolve_choices(raw_choices, settings)
        depends = dependencies(raw_choices)
        resolved[name] = {
            "name": name,
            "label": node.get("label", name),
            "type": node.get("type"),
            "required": bool(node.get("required", False)),
            "readOnly": bool(node.get("readOnly", False)),
            "choices": choices,
            "dependsOn": depends,
            # True when the field has enumerated choices but a parent is unset.
            "blocked": bool(depends) and choices is None,
            "min": node.get("minValue"),
            "max": node.get("maxValue"),
        }
    return resolved


def chosen_labels(schema: dict[str, Any] | None, settings: dict[str, Any]) -> dict[str, str]:
    """Each setting BRAIN offers as a choice, by the label of the value chosen."""
    return {
        name: str(choice["label"])
        for name, field in resolve_options(schema or {}, settings).items()
        for choice in field["choices"] or []
        if choice.get("value") == settings.get(name) and choice.get("label") is not None
    }


def valid_values(schema: dict[str, Any], field: str, settings: dict[str, Any]) -> list[Any]:
    """The legal values for one field given the rest of the settings."""
    node = schema.get(field)
    if not isinstance(node, dict):
        return []
    choices = resolve_choices(node.get("choices"), settings)
    return [c.get("value") for c in choices] if choices else []


def validate_settings(
    schema: dict[str, Any], settings: dict[str, Any], *, require_all: bool = False
) -> list[str]:
    """Check a settings dict against the schema; returns human-readable problems.

    Catching an impossible combination locally saves a round trip and a slice of the daily
    quota. Only *supplied* values are checked by default, since a half-filled form is not
    an error; missing-required is opt-in via ``require_all``.
    """
    problems: list[str] = []

    for name, node in schema.items():
        if not isinstance(node, dict):
            continue

        value = settings.get(name)

        if value is None:
            if require_all and bool(node.get("required", False)):
                problems.append(f"{node.get('label', name)} is required.")
            continue

        raw_choices = node.get("choices")
        if raw_choices is None:
            continue

        choices = resolve_choices(raw_choices, settings)
        if choices is None:
            # A parent field is missing or itself invalid; that is reported separately.
            continue

        allowed = [c.get("value") for c in choices]
        if value not in allowed:
            label = node.get("label", name)
            depends = dependencies(raw_choices)
            context = ""
            if depends:
                shown = ", ".join(f"{d}={settings.get(d)!r}" for d in depends)
                context = f" for {shown}"
            options = ", ".join(str(a) for a in allowed) or "nothing"
            problems.append(f"{label} {value!r} is not available{context}. Valid: {options}.")

    return problems
