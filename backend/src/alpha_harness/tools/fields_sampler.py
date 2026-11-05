"""Fields Sampler: one Alpha re-run with each of its data fields swapped for others.

One field changes at a time and everything else holds, so a search that would pair every
field with every other is a line through each field instead. Two ways to pick the others:

- **Sweep**: every other field of the same dataset.
- **Similar**: the fields whose descriptions read closest to this one's, the closest few from
  the same dataset, then the same subcategory, the same category, and the whole market.
- **Buckets**: for an Alpha reading several fields of one dataset, every field of it in the
  bucket of the one it is most like (:mod:`.field_buckets`), and every field swapped at once,
  one from each bucket's top picks. Held one at a time, the parts would still add the same
  information.

In Sweep and Similar a variant nothing refused becomes a seed itself, and the task sweeps it
the same way, so a good swap is followed by the next one until the task's simulations run out.

Only what BRAIN itself implies is filtered: a field must exist in the Alpha's market, be of
the same type (a ``vec_`` operator wraps a VECTOR field, nothing a MATRIX one), and not be
one of the dates or symbol identifiers BRAIN advises against.
"""

import asyncio
import re
from contextlib import suppress
from dataclasses import dataclass
from itertools import product, zip_longest
from typing import Any, Literal

from sqlalchemy import func, select

from ..brain.schemas import SimulationRequest, SimulationSettings
from ..brain.settings_schema import chosen_labels
from ..catalog import search
from ..catalog.queries import SCORED_SOURCE, CatalogQueries, Tuple4
from ..db.models import Trial, TrialState
from ..labs import scheduler
from ..labs.fastexpr import (
    Node,
    ParseError,
    data_fields,
    guarded,
    parse,
    render,
    replace_at,
    walk_with_parents,
)
from ..labs.params import FieldsSamplerParams, params_of
from ..labs.search import identity_of
from ..vault.store import same_family
from ..vault.yields import verdict
from .correlation_breaker import held_settings
from .field_buckets import FieldText, split

Mode = Literal["sweep", "similar", "buckets"]
SWEEP: Mode = "sweep"
SIMILAR: Mode = "similar"
BUCKETS: Mode = "buckets"

#: The tiers Similar takes its fields from, nearest first.
TIERS = ("dataset", "subcategory", "category", "market")

#: Words in a field id that name a date or a symbol, not a measurement.
_TECHNICAL = frozenset({"date", "dates", "timestamp", "isin", "cusip", "sedol", "ticker"})
_WORDS = re.compile(r"[^0-9a-z]+")


@dataclass(frozen=True, slots=True)
class Candidate:
    field: str
    dataset: str
    #: Which tier of Similar found it; ``dataset`` for every one a Sweep finds.
    tier: str
    coverage: float | None


@dataclass(frozen=True, slots=True)
class Variant:
    request: SimulationRequest
    field: str
    swap: str
    tier: str


def technical(field_id: str) -> bool:
    return bool(_TECHNICAL & set(_WORDS.split(field_id.lower())))


def slots(tree: Node) -> list[str]:
    """The fields that can be swapped: every data field the expression reads, grouping fields
    and the values of keyword arguments excepted."""
    return data_fields(tree)


def swapped(tree: Node, field: str, swap: str) -> Node:
    """The expression with ``field`` read as ``swap`` everywhere it appears, so a field that is
    used twice stays one field."""
    for path, node, parent in walk_with_parents(tree):
        if node.kind == "name" and node.value == field and not guarded(parent, path):
            tree = replace_at(tree, path, Node("name", swap))
    return tree


def scope_of(settings: dict[str, Any]) -> Tuple4:
    return Tuple4(
        instrument_type=str(settings.get("instrumentType") or "EQUITY"),
        region=str(settings.get("region") or ""),
        delay=int(settings.get("delay") or 0),
        universe=str(settings.get("universe") or ""),
    )


async def bucketable(queries: CatalogQueries, scope: Tuple4, fields: list[str]) -> bool:
    """Whether the Alpha reads several fields, all of one dataset: one Buckets can split."""
    if len(fields) < 2:
        return False
    found = [await queries.field(scope, f) for f in fields]
    return all(found) and len({r["dataset_id"] for r in found if r}) == 1


async def buckets(
    queries: CatalogQueries, scope: Tuple4, fields: list[str]
) -> dict[str, tuple[list[Candidate], str | None]]:
    """Each field's bucket of its dataset, in pick order, with why one is empty."""
    seeds = {f: await queries.field(scope, f) for f in fields}
    missing = [f for f, row in seeds.items() if row is None]
    if missing:
        why = f"{missing[0]} is not in the catalog for {scope.label}. Download it in Data."
        return {f: ([], why) for f in fields}
    dataset = {row["dataset_id"] for row in seeds.values() if row}
    if len(dataset) > 1:
        why = "Buckets needs every field from one dataset."
        return {f: ([], why) for f in fields}
    rows = await queries.catalog.query(
        f"""
        SELECT field_id, dataset_id, field_type, coverage, description FROM data_field
        WHERE {Tuple4.WHERE} AND dataset_id = ?
        ORDER BY field_id
        """,  # noqa: S608 - module constants
        [*scope.params, *dataset],
    )
    own = set(fields)
    usable = [r for r in rows if r["field_id"] in own or not technical(r["field_id"])]
    found: dict[str, tuple[list[Candidate], str | None]] = {}
    # A vec_ operator wraps a VECTOR field and nothing a MATRIX one: each type is split
    # among the seeds of that type.
    for kind in dict.fromkeys(row["field_type"] for row in seeds.values() if row):
        of_kind = [f for f, row in seeds.items() if row and row["field_type"] == kind]
        members = [r for r in usable if r["field_type"] == kind]
        texts = [FieldText(r["field_id"], r["description"] or "", r["coverage"]) for r in members]
        by_id = {r["field_id"]: r for r in members}
        split_up = await asyncio.to_thread(split, texts, of_kind)
        for seed, bucket in zip(of_kind, split_up, strict=True):
            listed = [
                Candidate(f, by_id[f]["dataset_id"], "bucket", by_id[f]["coverage"])
                for f in bucket
                if f not in own
            ]
            found[seed] = (listed, None if listed else f"No other {kind} field is like {seed}.")
    return found


async def candidates(
    queries: CatalogQueries, scope: Tuple4, field: str, mode: Mode, k: int
) -> tuple[list[Candidate], str | None]:
    """The fields ``field`` could be swapped for, with why there are none when there are none.
    Sweep or Similar: a bucket is the whole Alpha's to make, see :func:`buckets`."""
    seed = await queries.field(scope, field)
    if seed is None:
        return [], f"{field} is not in the catalog for {scope.label}. Download it in Data."
    kept = (seed["field_type"],)
    if mode == SWEEP:
        rows = await queries.catalog.query(
            f"""
            SELECT field_id, dataset_id, coverage FROM data_field
            WHERE {Tuple4.WHERE} AND dataset_id = ? AND field_type = ? AND field_id <> ?
            ORDER BY coverage DESC NULLS LAST, field_id
            """,  # noqa: S608 - module constants
            [*scope.params, seed["dataset_id"], *kept, field],
        )
        found = [
            Candidate(r["field_id"], r["dataset_id"], "dataset", r["coverage"])
            for r in rows
            if not technical(r["field_id"])
        ]
        return found, None if found else f"{seed['dataset_id']} has no other {kept[0]} field."

    if not await search.ready(queries.catalog):
        return [], "Similar needs the catalog's search index, which is not built on this computer."
    terms = search.query_terms(f"{field} {seed.get('description') or ''}")
    if terms is None:
        return [], f"{field} has no description to compare."
    rows = await queries.catalog.query(
        f"""
        SELECT field_id, dataset_id, subcategory_id, category_id, coverage FROM {SCORED_SOURCE}
        WHERE {Tuple4.WHERE} AND field_type = ? AND field_id <> ?
        ORDER BY score DESC, field_id
        """,  # noqa: S608 - module constants
        [terms, 0, *scope.params, *kept, field],
    )
    nearest = {tier: [] for tier in TIERS}
    for r in rows:
        if technical(r["field_id"]):
            continue
        tier = (
            "dataset"
            if r["dataset_id"] == seed["dataset_id"]
            else "subcategory"
            if r["subcategory_id"] == seed["subcategory_id"]
            else "category"
            if r["category_id"] == seed["category_id"]
            else "market"
        )
        if len(nearest[tier]) < k:
            nearest[tier].append(Candidate(r["field_id"], r["dataset_id"], tier, r["coverage"]))
    found = [c for tier in TIERS for c in nearest[tier]]
    return found, None if found else f"No field reads like {field}."


type Identity = tuple[str, str]


def identity(expression: str, settings: dict[str, Any]) -> Identity:
    """What makes two simulations the same, the mode aside: Settings decides that per task."""
    return identity_of(expression, {k: v for k, v in settings.items() if k != "simulationMode"})


def variants(
    expression: str,
    settings: dict[str, Any],
    swaps: dict[str, list[Candidate]],
    seen: set[Identity],
) -> list[Variant]:
    """One simulation per field and candidate, at ``settings`` held, none already in ``seen``.

    Fields take turns, each in its own candidates' order, so a task that cannot run them all
    gives every field its best swaps first. A candidate the expression already reads is
    skipped: ``liabilities / assets`` swapped to ``assets / assets`` says nothing. The mode is
    left to Settings, as for any task. ``seen`` is added to.
    """
    tree = parse(expression)
    own = set(slots(tree))
    held = SimulationSettings.model_validate(
        {k: v for k, v in settings.items() if k != "simulationMode"}
    )
    out: list[Variant] = []
    for turn in zip_longest(*swaps.values()):
        for field, c in zip(swaps, turn, strict=True):
            if c is None or c.field in own:
                continue
            code = render(swapped(tree, field, c.field))
            key = identity(code, settings)
            if key in seen:
                continue
            seen.add(key)
            request = SimulationRequest(settings=held.model_copy(), regular=code)
            out.append(Variant(request, field, c.field, c.tier))
    return out


def grid(
    expression: str,
    settings: dict[str, Any],
    swaps: dict[str, list[Candidate]],
    top: int,
    seen: set[Identity],
) -> list[Variant]:
    """Every field swapped at once, one from each bucket's ``top`` picks, at ``settings`` held.

    Nearest the top of every bucket first: by the sum of the picks' places, so a task that
    cannot run the whole grid still pairs each bucket's best with the other's. ``seen`` is
    added to.
    """
    tree = parse(expression)
    held = SimulationSettings.model_validate(
        {k: v for k, v in settings.items() if k != "simulationMode"}
    )
    fields = list(swaps)
    picks = [swaps[f][:top] for f in fields]
    places = sorted(
        product(*(range(len(p)) for p in picks)), key=lambda at: (sum(at), max(at, default=0), at)
    )
    out: list[Variant] = []
    for at in places:
        chosen = [p[i] for p, i in zip(picks, at, strict=True)]
        varied = tree
        for field, c in zip(fields, chosen, strict=True):
            varied = swapped(varied, field, c.field)
        code = render(varied)
        key = identity(code, settings)
        if key in seen:
            continue
        seen.add(key)
        request = SimulationRequest(settings=held.model_copy(), regular=code)
        out.append(Variant(request, ",".join(fields), ",".join(c.field for c in chosen), "bucket"))
    return out


async def plan(state: Any, alpha_id: str, mode: Mode, k: int) -> dict[str, Any]:
    """The Alpha, its held settings, and each of its fields with what it could be swapped for.

    Reads BRAIN and the local catalog; simulates nothing.
    """
    problems: list[str] = []
    body = await state.endpoints.alpha_body(alpha_id)
    code = body.get("regular") or body.get("combo") or body.get("selection") or {}
    expression = code.get("code") if isinstance(code, dict) else None
    settings = dict(body.get("settings") or {})
    found = {
        "alphaId": alpha_id,
        "expression": expression or "",
        "settings": held_settings(settings),
        "rawSettings": settings,
        "slots": [],
        "settingLabels": {},
        "others": [],
        "bucketable": False,
        "problems": problems,
    }
    rows: list[dict[str, Any]] = []
    if not expression:
        problems.append(f"{alpha_id} has no expression to sample.")
        return found
    try:
        tree = parse(expression)
    except ParseError as exc:
        problems.append(f"Its expression could not be parsed: {exc}")
        return found
    fields = slots(tree)
    if not fields:
        problems.append(f"{alpha_id} reads no data field that can be swapped.")
    scope = scope_of(settings)
    can_bucket = await bucketable(state.queries, scope, fields)
    if mode == BUCKETS and not can_bucket:
        problems.append("Buckets is for an Alpha reading several fields of one dataset.")
        return found
    swaps = await found_at(state.queries, settings, fields, mode, k)
    for field in fields:
        listed, why = swaps[field]
        rows.append(
            {
                "field": field,
                "candidates": [
                    {"field": c.field, "dataset": c.dataset, "tier": c.tier, "coverage": c.coverage}
                    for c in listed
                ],
                "problem": why or "",
            }
        )
    if fields and not any(r["candidates"] for r in rows):
        problems.append("None of its fields has anything to be swapped for.")
    schema = await state.metadata.cached_settings_schema()
    others = [
        await _other(state.queries, o, fields, mode, k, schema)
        for o in await elsewhere(state, alpha_id, expression, settings)
    ]
    return found | {
        "slots": rows,
        "settingLabels": chosen_labels(schema, settings),
        "others": others,
        "bucketable": can_bucket,
    }


async def elsewhere(
    state: Any, alpha_id: str, expression: str, settings: dict[str, Any]
) -> list[dict[str, Any]]:
    """The same expression's other submitted Alphas, one per set of
    settings: the markets and settings it is already known to work at. Each is read from
    BRAIN for its full settings, which the vault does not keep.

    A region-agnostic Alpha's own family is left out: its children are its own settings run
    region by region, not somewhere else it works."""
    rows = await state.alphas.catalog.query(
        f"""
        SELECT a.alpha_id, a.status FROM alpha a
        LEFT JOIN alpha s ON s.alpha_id = ?
        WHERE a.expression = ? AND a.alpha_id <> ?
          AND NOT coalesce({same_family("a", "s")}, false)
        ORDER BY a.date_created
        """,  # noqa: S608
        [alpha_id, expression, alpha_id],
    )
    proven = [r for r in rows if (r["status"] or "UNSUBMITTED") != "UNSUBMITTED"]
    bodies = await asyncio.gather(
        *(state.endpoints.alpha_body(r["alpha_id"]) for r in proven), return_exceptions=True
    )
    found: list[dict[str, Any]] = []
    known = {identity(expression, settings)}
    for r, body in zip(proven, bodies, strict=True):
        if isinstance(body, BaseException):
            continue
        held = dict(body.get("settings") or {})
        key = identity(expression, held)
        if key in known:
            continue
        known.add(key)
        found.append({"alphaId": r["alpha_id"], "status": r["status"], "rawSettings": held})
    return found


async def _other(
    queries: CatalogQueries,
    other: dict[str, Any],
    fields: list[str],
    mode: Mode,
    k: int,
    schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """One other set of settings, with how many swaps each field has in its own market."""
    held = other["rawSettings"]
    swaps = await swaps_at(queries, held, fields, mode, k)
    counts = {f: len(found) for f, found in swaps.items()}
    return other | {
        "settings": held_settings(held),
        "settingLabels": chosen_labels(schema, held),
        "swaps": [{"field": f, "count": n} for f, n in counts.items()],
    }


async def found_at(
    queries: CatalogQueries, settings: dict[str, Any], fields: list[str], mode: Mode, k: int
) -> dict[str, tuple[list[Candidate], str | None]]:
    """Each field's swaps in the market ``settings`` names, with why it has none."""
    scope = scope_of(settings)
    if mode == BUCKETS:
        return await buckets(queries, scope, fields)
    return {f: await candidates(queries, scope, f, mode, k) for f in fields}


async def swaps_at(
    queries: CatalogQueries, settings: dict[str, Any], fields: list[str], mode: Mode, k: int
) -> dict[str, list[Candidate]]:
    """Each field's swaps in the market ``settings`` names."""
    found = await found_at(queries, settings, fields, mode, k)
    return {f: listed for f, (listed, _) in found.items()}


def taking_turns(groups: list[list[Variant]], size: int) -> list[Variant]:
    """Each set of settings in turn, a multi-simulation's worth at a time: a task that cannot
    run them all still reaches every one, and each batch keeps to one market."""
    chunks = [[g[i : i + size] for i in range(0, len(g), size)] for g in groups]
    return [v for turn in zip_longest(*chunks, fillvalue=[]) for chunk in turn for v in chunk]


def seed_trials(chosen: list[Variant]) -> Any:
    """The seed's own sweep, written with the task: the first generation."""

    def build(study_id: int) -> list[Trial]:
        return [_parked(study_id, n, v, parent=None, generation=1) for n, v in enumerate(chosen)]

    return build


def _parked(
    study_id: int, number: int, v: Variant, *, parent: int | None, generation: int
) -> Trial:
    return Trial(
        study_id=study_id,
        number=number,
        params={"parent": parent, "field": v.field, "swap": v.swap, "tier": v.tier},
        distributions={},
        expression=v.request.regular,
        settings=v.request.settings.model_dump(by_alias=True, exclude_none=True),
        state=TrialState.PRUNED,
        message=scheduler.PENDING_SEND,
        generation=generation,
    )


def winner(trial: Trial) -> bool:
    """A finished Full-mode trial nothing refused. A Quick one waits for its run in Full."""
    result = trial.result or {}
    if trial.state != TrialState.COMPLETE or result.get("quick"):
        return False
    return verdict(list(result.get("checks") or [])) in ("submittable", "pending")


async def expand(optimizer: Any, row: Any) -> int:
    """Sweep each new winner the way the seed was swept, within the task's simulations.

    Every finished trial is read once and marked, winner or not: this runs on every scheduler
    tick, and a task can hold thousands of trials.
    """
    run = params_of(row, FieldsSamplerParams)
    if run.mode == BUCKETS:
        # The grid is the task: a winner's own grid would be the same buckets again.
        return 0
    async with optimizer.db.session() as session:
        unread = (
            await session.scalars(
                select(Trial)
                .where(
                    Trial.study_id == row.id,
                    Trial.state == TrialState.COMPLETE,
                    func.json_extract(Trial.params, "$.read").is_(None),
                )
                .order_by(Trial.number)
            )
        ).all()
        if not unread:
            return 0
        for t in unread:
            t.params = {**(t.params or {}), "read": True}
        winners = [t for t in unread if winner(t)]
        added = 0
        if winners:
            queries = CatalogQueries(optimizer.alphas.catalog)
            count, number = (
                await session.execute(
                    select(func.count(), func.max(Trial.number)).where(Trial.study_id == row.id)
                )
            ).one()
            room = row.max_trials - int(count)
            tried = (
                await session.execute(
                    select(Trial.expression, Trial.settings).where(
                        Trial.study_id == row.id, Trial.expression.is_not(None)
                    )
                )
            ).all()
            seen = {identity(e or "", dict(st or {})) for e, st in tried}
            # The source Alphas are no trials, so a winner swapped back would re-run one. Each
            # set of settings the task runs at is one source's.
            with suppress(ParseError):
                source = render(parse(row.template_source or ""))
                seen |= {identity(source, dict(st or {})) for _, st in tried}
            number = int(number or 0)
            for parent in winners:
                if room <= 0:
                    break
                try:
                    tree = parse(parent.expression or "")
                except ParseError:
                    continue
                settings = dict(parent.settings or {})
                swaps = await swaps_at(queries, settings, slots(tree), run.mode, run.k)
                for v in variants(parent.expression or "", settings, swaps, seen)[:room]:
                    number += 1
                    room -= 1
                    added += 1
                    session.add(
                        _parked(
                            row.id,
                            number,
                            v,
                            parent=parent.number,
                            generation=(parent.generation or 1) + 1,
                        )
                    )
        await session.commit()
    return added


async def refill(optimizer: Any, row: Any, want: int, waiting: bool) -> int:
    """Grow the tree from any new winner, then drain it like every task written up front."""
    from . import settings_sampler  # imported here, as the scheduler imports it

    await expand(optimizer, row)
    return await settings_sampler.refill(optimizer, row, want, waiting)


if __name__ == "__main__":
    import sys

    tree = parse("group_neutralize(liabilities / ts_mean(liabilities, 20) + assets, subindustry)")
    vec = parse("rank(ts_zscore(vec_avg(scl12_buzz), 20))")
    picks = {
        "liabilities": [Candidate("debt", "fnd6", "dataset", 1.0)] * 2
        + [Candidate("assets", "fnd6", "dataset", 1.0)],
    }
    turns = variants(
        "liabilities / assets",
        {"region": "USA", "delay": 1, "universe": "TOP3000"},
        {
            "liabilities": [Candidate(f, "d", "dataset", 1.0) for f in ("a", "b")],
            "assets": [Candidate("c", "d", "dataset", 1.0)],
        },
        set(),
    )
    seen: set[Identity] = set()
    usa = {"region": "USA", "delay": 1, "universe": "TOP3000"}
    first = variants("liabilities / assets", usa, picks, seen)
    again = variants("liabilities / assets", usa, picks, seen)
    checks: dict[str, tuple[object, object]] = {
        "slots": (slots(tree), ["liabilities", "assets"]),
        "every place": (
            render(swapped(tree, "liabilities", "debt")),
            "group_neutralize(debt / ts_mean(debt, 20) + assets, subindustry)",
        ),
        "vec kept": (
            render(swapped(vec, "scl12_buzz", "scl12_sentiment")),
            "rank(ts_zscore(vec_avg(scl12_sentiment), 20))",
        ),
        "technical": (
            (technical("fnd6_rdq_date"), technical("isin"), technical("fnd6_acodo")),
            (True, True, False),
        ),
        "once each": ([v.request.regular for v in first], ["debt / assets"]),
        "seen skipped": (again, []),
        "fields take turns": (
            [v.request.regular for v in turns],
            ["a / assets", "liabilities / c", "b / assets"],
        ),
    }
    eur = {"region": "EUR", "delay": 0, "universe": "TOP2500"}
    both: set[Identity] = set()
    at_usa = variants("liabilities / assets", usa, picks, both)
    at_eur = variants("liabilities / assets", eur, picks, both)
    checks["each settings its own"] = (len(at_usa) + len(at_eur), 2)
    g1 = [Variant(at_usa[0].request, str(n), "", "") for n in range(3)]
    g2 = [Variant(at_eur[0].request, str(n), "", "") for n in range(3, 5)]
    checks["turns by batch"] = (
        [v.field for v in taking_turns([g1, g2], 2)],
        ["0", "1", "3", "4", "2"],
    )
    pair = grid(
        "rank(earnings) + rank(revenue)",
        usa,
        {
            "earnings": [Candidate(f, "d", "bucket", 1.0) for f in ("eps", "pe", "roe")],
            "revenue": [Candidate(f, "d", "bucket", 1.0) for f in ("sales", "ebitda")],
        },
        2,
        set(),
    )
    checks["both change, best first"] = (
        [v.request.regular for v in pair],
        [
            "rank(eps) + rank(sales)",
            "rank(eps) + rank(ebitda)",
            "rank(pe) + rank(sales)",
            "rank(pe) + rank(ebitda)",
        ],
    )
    wrong = [f"{name}: {got!r}" for name, (got, want) in checks.items() if got != want]
    if wrong:
        raise SystemExit(f"fields sampler self-check failed: {'; '.join(wrong)}")
    sys.stdout.write(f"{len(checks)} cases as expected\n")
