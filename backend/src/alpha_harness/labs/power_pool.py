"""LLM Power Pool Lab: an LLM writes Power Pool Alphas for one dataset at a time.

While a task runs, a background call asks the chosen model for 20 expressions. Each is
checked offline (operators, fields, at most 8 operators and 3 data fields, counted the way
BRAIN counts them), given a random universe, neutralization and decay, and kept as a
waiting trial until cores are free. Calls never happen inside ``advance``.
"""

import json
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import func, or_, select

from ..brain.platform import (
    GROUPING_FIELDS,
    POWER_POOL_MAX_FIELDS,
    POWER_POOL_MAX_OPERATORS,
    REGION_AGNOSTIC_REGION,
)
from ..brain.schemas import SimulationSettings
from ..catalog.queries import CatalogQueries, FieldFilter, Tuple4
from ..db.models import Study, StudyStatus, Trial, TrialState, utcnow
from ..llm.keys import BudgetExhaustedError, LLMError, LLMUnavailable
from ..llm.prompts import POWER_POOL_LAB
from ..llm.text import FENCE
from ..tasks import spawn
from . import scheduler, search
from .fastexpr import (
    ParseError,
    operator_count,
    operator_table,
    parse,
    render,
    validate,
    walk,
    walk_with_parents,
)
from .objectives import FAILURE
from .params import PowerPoolParams, params_of

if TYPE_CHECKING:  # pragma: no cover
    import asyncio

    from ..db.duck import Catalog
    from .study import Optimizer

log = structlog.get_logger(__name__)

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


async def context_for(
    catalog: Catalog,
    region: str,
    delay: int,
    universes: list[str],
    dataset: str,
    narrow: FieldFilter | None = None,
) -> Context | None:
    marks = ", ".join("?" for _ in universes)
    extra = (*DATA_FIELDS, *GROUPING_FIELDS)
    rows = await catalog.query(
        f"""
        SELECT field_id, dataset_id, field_type, universe, coverage, description,
               region_coverage FROM data_field
        WHERE instrument_type = 'EQUITY' AND region = ? AND delay = ? AND universe IN ({marks})
          AND field_type IN ('MATRIX', 'VECTOR', 'GROUP')
          AND (dataset_id = ? OR field_id IN ({", ".join("?" for _ in extra)}))
        """,  # noqa: S608
        [region, delay, *universes, dataset, *extra],
    )
    if not any(r["dataset_id"] == dataset for r in rows):
        return None
    meta = await catalog.query(
        "SELECT name, description, category_name, subcategory_name FROM data_set "
        "WHERE region = ? AND delay = ? AND dataset_id = ? LIMIT 1",
        [region, delay, dataset],
    )
    held: dict[str, set[str]] = {}
    info: dict[str, dict[str, Any]] = {}
    rank = {u: i for i, u in enumerate(universes)}
    for r in rows:
        field_id = str(r["field_id"])
        held.setdefault(field_id, set()).add(str(r["universe"]))
        best = info.get(field_id)
        if best is None or rank[str(r["universe"])] < rank[str(best["universe"])]:
            info[field_id] = r
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

    def field(f: str) -> Field:
        r = info[f]
        return Field(
            f,
            str(r["field_type"]),
            r["coverage"],
            str(r["description"] or "")[:160],
            r["region_coverage"],
        )

    fields = sorted(
        (field(f) for f in own if f not in GROUPING_FIELDS), key=lambda x: -(x.coverage or 0)
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
        basics=tuple(field(f) for f in DATA_FIELDS if f in info and f not in own),
        groups=tuple(g for g in GROUPING_FIELDS if g in info),
        types={f: str(r["field_type"]) for f, r in info.items()},
        own=frozenset(own),
        names=frozenset(info),
        held={f: frozenset(u) for f, u in held.items()},
    )


def check(text: str, ctx: Context, table: dict[str, Any]) -> tuple[str, frozenset[str]]:
    """The canonical expression and the fields it uses, or :class:`Rejected`."""
    try:
        tree = parse(text)
    except ParseError as exc:
        raise Rejected(f"Could not be read: {exc}") from exc
    if problems := validate(tree, table, set(ctx.names)):
        raise Rejected(problems[0])
    if (count := operator_count(tree)) > POWER_POOL_MAX_OPERATORS:
        raise Rejected(f"{count} operators; Power Pool allows {POWER_POOL_MAX_OPERATORS}.")
    used = frozenset(n.value for _, n in walk(tree) if n.kind == "name" and n.value in ctx.names)
    data = sorted(used - set(GROUPING_FIELDS))
    if len(data) > POWER_POOL_MAX_FIELDS:
        raise Rejected(f"{len(data)} data fields ({', '.join(data)}); Power Pool allows 3.")
    if not used & (ctx.own - set(GROUPING_FIELDS)):
        raise Rejected(f"Uses no field of {ctx.id}.")
    for _, node, parent in walk_with_parents(tree):
        if (
            node.kind == "name"
            and ctx.types.get(node.value) == "VECTOR"
            and (parent is None or parent.kind != "call" or not parent.value.startswith("vec_"))
        ):
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


def memory_text(done: list[Any], waiting: list[Any], thrown: list[Any]) -> str:
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
) -> tuple[str, int]:
    """The user turn, and how many field lines it shows."""
    head = "\n\n".join(
        [
            f"MARKET\n{run.region} · Delay {run.delay} · Universes {', '.join(run.universes)}"
            + (REGION_AGNOSTIC_BRIEF if run.region == REGION_AGNOSTIC_REGION else ""),
            "OPERATORS\n" + operators_text(operators),
            f"DATASET\n{ctx.id} · {ctx.name} · {ctx.category}\n{ctx.description}",
        ]
    )
    tail = "\n\n".join(
        [
            "PRICE AND VOLUME FIELDS · count as data fields\n"
            + "\n".join(_line(f) for f in ctx.basics),
            "GROUPING_FIELDS FIELDS · not counted\n" + ", ".join(ctx.groups),
            f"YOUR EARLIER ALPHAS ON {ctx.id}\n{memory}",
            f"Write {PER_CALL} new Power Pool Alphas that use {ctx.id}.",
        ]
    )
    room = budget * 4 - len(POWER_POOL_LAB) - len(head) - len(tail) - 200
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
    title = (
        f"FIELDS OF {ctx.id} · {start + 1}-{start + len(lines)} of {total:,}, most complete first"
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
    cap = max(3, 2 * -(-row.max_trials // PER_CALL))
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
        await scheduler.finish(optimizer, row.id, StudyStatus.COMPLETE, f"Stopped: {stop}.")
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
        ids = run.dataset_ids
        dataset = min(ids, key=lambda d: (by.get(d, {}).get("calls", 0), ids.index(d)))
        # The task's model first; with it out of budget or its provider down, the next one a
        # Key can run, most requests left first, so the run never sits waiting for a reset.
        refs = [run.model]
        if not run.only_model:
            refs += [str(m["ref"]) for m in await optimizer.llm.runnable()]
        models = [m for ref in dict.fromkeys(refs) if (m := optimizer.llm.registry.get(ref))]
        operators = await optimizer.metadata.cached_operators() or []
        ctx = await context_for(
            optimizer.alphas.catalog,
            run.region,
            run.delay,
            run.universes,
            dataset,
            FieldFilter.model_validate(run.field_filter) if run.field_filter else None,
        )
        if not models:
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
                f"{dataset} is not in the downloaded catalog. Sync it, then resume.",
            )

        memory = await memory_of(optimizer, study_id, dataset)
        offset = int(by.get(dataset, {}).get("offset", 0))
        items: list[dict[str, Any]] = []
        answered = False
        failing: str | None = None
        spent: list[BudgetExhaustedError] = []
        down: LLMUnavailable | None = None
        entry: dict[str, Any] = {}
        model = models[0]
        shown = 0
        for model in models:
            user, shown = user_prompt(ctx, operators, run, memory, offset, model.prompt_tokens)
            entry = {
                "at": utcnow().isoformat(),
                "dataset": dataset,
                "model": model.id,
                "fields": shown,
            }
            try:
                answer = await optimizer.llm.generate(
                    system=POWER_POOL_LAB,
                    user=user,
                    model_ref=model.ref,
                    response_schema=SCHEMA,
                    temperature=1.0,
                )
            except BudgetExhaustedError as exc:
                spent.append(exc)
                continue
            except LLMUnavailable as exc:
                down = exc
                continue
            except LLMError as exc:
                _retry[study_id] = time.monotonic() + 60.0
                entry["error"] = str(exc)[:300]
                llm["failed"] = int(llm.get("failed") or 0) + 1
                if llm["failed"] >= 3:
                    # Saved as none, so a resumed task counts its failures afresh.
                    llm["failed"] = 0
                    failing = f"The LLM failed 3 times in a row: {exc}"
                break
            answered = True
            items = parse_alphas(answer.text)[:40]
            entry["tokens"] = answer.total_tokens
            if not items:
                entry["error"] = "The answer was not the JSON asked for: " + answer.text[:200]
            break
        else:
            if down is None:
                # Every model is out of budget: wait for the soonest to come back.
                _retry[study_id] = time.monotonic() + max(5.0, min(e.retry_after for e in spent))
                if any(e.daily for e in spent):
                    reason = (
                        str(spent[0])
                        if len(spent) == 1
                        else f"all {len(spent)} models a Key can run have used their daily "
                        "allowance. The task carries on when the first one resets."
                    )
                    await _note(optimizer, study_id, f"Waiting for LLM budget: {reason}")
                return None
            # A busy provider passes: tried again in a minute, never counted toward a pause.
            _retry[study_id] = time.monotonic() + 60.0
            entry["error"] = str(down)[:300]

        table = operator_table(operators)
        rng = random.Random()
        async with optimizer.lock(study_id), optimizer.db.session() as session:
            stored = await session.get(Study, study_id)
            if stored is None:
                return None
            # The two columns alone: whole rows carry each trial's JSON, and a long task has
            # thousands of them.
            seen = set(
                (
                    await session.scalars(
                        select(Trial.expression).where(Trial.study_id == study_id)
                    )
                ).all()
            )
            last = await session.scalar(
                select(func.max(Trial.number)).where(Trial.study_id == study_id)
            )
            number = -1 if last is None else last
            valid = rejected = 0
            for item in items:
                text = str(item.get("expression") or "").strip()
                number += 1
                try:
                    expression, used = check(text, ctx, table)
                    if expression in seen:
                        raise Rejected("Already written in this task.")
                    settings = draw(rng, used, ctx, run)
                    seen.add(expression)
                    valid += 1
                    session.add(
                        Trial(
                            study_id=study_id,
                            number=number,
                            params={"dataset": dataset, "model": model.id},
                            distributions={},
                            expression=expression,
                            settings=settings,
                            state=TrialState.PRUNED,
                            message=PROPOSED,
                        )
                    )
                except Rejected as exc:
                    rejected += 1
                    session.add(
                        Trial(
                            study_id=study_id,
                            number=number,
                            params={"dataset": dataset, "model": model.id, "rejected": True},
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
                tally = dict(llm.get("byModel") or {})
                tally[model.id] = int(tally.get(model.id) or 0) + 1
                llm["byModel"] = tally
            entry |= {"valid": valid, "rejected": rejected}
            params.llm = llm
            params.calls = [*params.calls, entry][-100:]
            stored.sampler_params = params.dump()
            if valid and stored.message and stored.message.startswith("Waiting for LLM budget"):
                stored.message = None
        await optimizer.notify()
        if failing:
            await _pause(optimizer, study_id, failing)
    except Exception:
        log.exception("power_pool.write_failed", study_id=study_id)
        _retry[study_id] = time.monotonic() + 60.0


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
