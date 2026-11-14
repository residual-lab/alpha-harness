"""Today: what you have, and what you are about to waste.

One endpoint behind the first screen a consultant sees: simulations left today, what the
account can do, and what is left in the assistant's budget. It also drives the onboarding
flow, which is linear on purpose — sign in, add a key, start. ``/run`` is the Dashboard's one
click: a focused run of today's simulations, now.

The true daily simulation limit reaches us only in the response headers of a simulation
POST, so the figure starts as the configured allowance minus today's runs and is replaced
by the platform's own number once the first batch comes back. ``exact`` says which one you
are looking at.
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..brain.errors import BrainError, BrainNotFound
from ..brain.filters import PLATFORM_TZ
from ..brain.platform import (
    DAILY_SIMULATIONS,
    INSTRUMENT_TYPE,
    MULTI_SIMULATION_SIZE,
    PYRAMID_MIN_ALPHAS,
    REGION_AGNOSTIC_REGION,
)
from ..catalog.pyramids import pyramid_grid
from ..catalog.queries import scope_label
from ..catalog.sync import STARTER
from ..db.models import SyncStatus, utcnow
from ..labs.launch import AddedTask, SearchRequest, add_study, neutralizations_for
from ..llm.budget import seconds_until_reset
from ..schemas import EngineStatus, Out, SyncRunRow
from ..vault import yields
from .deps import State, refuse
from .lab_tasks import queue_tasks
from .llm import LLMBudget
from .search_lab import search_task_args
from .tools import FieldsRequest, fields_task_args

router = APIRouter(prefix="/api/today", tags=["today"])


class You(Out):
    signed_in: bool
    email: str | None
    user_id: str | None
    full_name: str | None
    #: BRAIN's permission codes for the account, as it sends them.
    permissions: list[str]
    can_run_ten_at_once: bool
    verification_url: str | None


class TodaySimulations(Out):
    limit: int
    used: int
    remaining: int
    queued: int
    #: What is genuinely still going to waste: not yet run and not yet claimed.
    unspoken: int
    #: False means "assumed from your allowance", true means "the platform told us".
    exact: bool
    resets_in_seconds: int
    resets_at: str
    engine: EngineStatus
    headline: str


class Assistant(Out):
    keys: int
    enabled_keys: int
    requests_remaining_today: int
    budget: list[LLMBudget]
    #: The soonest any set-up model's day turns over, or null with none set up.
    resets_in_seconds: int | None
    headline: str


class CatalogReadiness(Out):
    scope: str
    synced: bool
    fields: int
    running: SyncRunRow | None
    #: Some other market is downloaded, so the labs are not blocked outright.
    any_synced: bool


class Today(Out):
    step: Literal["sign-in", "add-key", "ready"]
    you: You
    simulations: TodaySimulations
    assistant: Assistant
    catalog: CatalogReadiness


class BarSimulations(Out):
    remaining: int
    limit: int
    exact: bool
    queued: int


class Bar(Out):
    signed_in: bool
    full_name: str | None
    expires_in_seconds: int | None
    simulations: BarSimulations
    resets_in_seconds: int


#: A focused run: about half an hour over eight cores, rather than the hours a whole day takes.
DEFAULT_RUN = 500


class TodayRunRequest(BaseModel):
    simulations: int = Field(default=DEFAULT_RUN, ge=1)


class TodayRun(Out):
    tasks: list[AddedTask]
    #: At most this many: a Fields Sampler stops once no swap leads anywhere new.
    simulations: int
    #: Every fallback and shortfall, each with its reason.
    notes: list[str]


@router.get("")
async def today(
    state: State,
    region: str = "USA",
    delay: int = 1,
    universe: str = "TOP3000",
    # Aliased like the catalog's scope: one spelling on the wire, so the frontend has one
    # scope-to-query-string helper rather than one per route.
    instrument_type: Annotated[str, Query(alias="instrumentType")] = INSTRUMENT_TYPE,
) -> Today:
    """Everything the first screen needs, in one call."""
    session = state.auth.session
    stored_email = await state.auth.stored_email()
    keys = await state.llm.keys.list_keys()
    enabled_keys = [k for k in keys if k.enabled]

    # The linear flow. Each step is a thing to do, never a thing to choose between.
    if not session.authenticated:
        step = "sign-in"
    elif not enabled_keys:
        step = "add-key"
    else:
        step = "ready"

    return Today.model_validate(
        {
            "step": step,
            "you": await _you(state, session, stored_email),
            "simulations": await simulations_today(state),
            "assistant": await _assistant(state, keys, enabled_keys),
            "catalog": await _catalog(state, instrument_type, region, delay, universe),
        }
    )


@router.get("/bar")
async def bar(state: State) -> Bar:
    """The top bar: session time left, simulations left, and the reset countdown.

    Kept apart from ``/api/today`` because it is polled on every screen, and the key and
    catalog lookups that page needs have no business running every thirty seconds.
    """
    session = state.auth.session
    sims = await simulations_today(state)
    return Bar.model_validate(
        {
            "signedIn": session.authenticated,
            "fullName": session.full_name,
            "expiresInSeconds": session.expires_in_seconds,
            "simulations": {k: sims[k] for k in ("remaining", "limit", "exact", "queued")},
            "resetsInSeconds": sims["resetsInSeconds"],
        }
    )


@router.post("/run", status_code=201)
async def run_today(body: TodayRunRequest, state: State) -> TodayRun:
    """Run some of today's unclaimed simulations now, with nothing to choose.

    Half goes to a Fields Sampler swapping the fields of the best Top pick, which builds on
    what already held up; half to Search Lab over the pyramids not yet formulated this
    quarter, so the day does not only clone what exists. Either takes the whole run when the
    other has nothing to do. Every task is planned before any is added.
    """
    left = (await simulations_today(state))["unspoken"]
    if left < 1:
        raise refuse(409, "nothing_left", "Today's simulations are already running or queued.")
    budget = min(left, body.simulations)
    notes: list[str] = []
    if budget < body.simulations:
        notes.append(f"Only {left:,} of today's simulations are unclaimed, so {budget:,} run.")

    market = STARTER
    ranked: list[str] = []
    try:
        grid = await pyramid_grid(state.endpoints, state.catalog)
    except BrainError as exc:
        notes.append(f"BRAIN's pyramids could not be read, so Search Lab sits this run out: {exc}")
    else:
        rows = await state.catalog.query(
            "SELECT DISTINCT dataset_id, category_id FROM data_field "
            "WHERE region = ? AND delay = ? AND dataset_id IS NOT NULL",
            [market.region, market.delay],
        )
        ranked = _open_datasets(grid["cells"], rows, market.region, market.delay)
        if not ranked:
            notes.append(
                f"No synced {market.region} D{market.delay} dataset is in a pyramid still open "
                "this quarter, so Search Lab sits this run out."
            )

    slots = int(state.engine.slots)
    half = max(1, slots // 2)
    split = bool(ranked) and budget >= 2 * MULTI_SIMULATION_SIZE
    share, cores = (budget - budget // 2, half) if split else (budget, slots)
    planned: list[dict[str, Any]] = []
    picks = (
        await yields.submittable(state.db, state.alphas, instrument_type=INSTRUMENT_TYPE, limit=1)
    )["shortlist"]
    for pick in picks:
        alpha_id = str(pick["alphaId"])
        if pick["settings"]["region"] == REGION_AGNOSTIC_REGION:
            notes.append(
                f"{alpha_id} is passed over: a region-agnostic Alpha is submittable only where two "
                "regions hold up, which is no bar to put a one-click run behind."
            )
            continue
        fields = FieldsRequest.model_validate(
            {"alphaId": alpha_id, "budget": share, "cores": cores}
        )
        try:
            planned.append(await fields_task_args(fields, state))
        except HTTPException as exc:
            notes.append(f"{alpha_id} is passed over: {_why(exc)}")
        except BrainNotFound:
            notes.append(f"{alpha_id} is passed over: BRAIN no longer has it.")
        else:
            break
    if not planned:
        notes.append(
            (
                "No Top pick could be built on"
                if picks
                else "No Top pick has held up in the test years yet"
            )
            + (", so Search Lab takes the whole run." if ranked else ".")
        )

    rest = budget - sum(int(p["simulations"]) for p in planned)
    if ranked and rest > 0:
        search = SearchRequest(
            region=market.region,
            delay=market.delay,
            universe=market.universe,
            dataset_ids=ranked[:200],
            neutralizations=await neutralizations_for(state, market.region, market.delay),
            cores=half if planned else slots,
            simulations=rest,
        )
        try:
            planned.append(await search_task_args(search, state))
        except HTTPException as exc:
            notes.append(f"Search Lab could not run: {_why(exc)}")
    if not planned:
        raise refuse(422, "nothing_to_run", " ".join(notes))
    total = sum(int(p["simulations"]) for p in planned)
    if total < budget:
        notes.append(f"{total:,} of the {budget:,} simulations asked for can run.")

    added: list[AddedTask] = []
    for args in planned:
        try:
            added.append(await add_study(state, now=utcnow(), **args))
        except Exception as exc:
            raise refuse(
                500,
                "run_incomplete",
                f"Added {len(added)} of {len(planned)} tasks and started none ({exc}). Run or "
                "remove them in Tasks.",
            ) from exc
    await queue_tasks(state, [task.id for task in added])
    return TodayRun(tasks=added, simulations=total, notes=notes)


def _open_datasets(
    cells: list[dict[str, Any]], rows: list[dict[str, Any]], region: str, delay: int
) -> list[str]:
    """The market's synced datasets in pyramids not yet formulated, highest multiplier first."""
    open_cells = sorted(
        (
            c
            for c in cells
            if c["region"] == region
            and c["delay"] == delay
            and c["synced"]
            and c["alphaCount"] < PYRAMID_MIN_ALPHAS
        ),
        key=lambda c: -(c["multiplier"] or 0),
    )
    rank = {c["categoryId"]: i for i, c in enumerate(open_cells)}
    held = sorted(
        (rank[r["category_id"]], str(r["dataset_id"])) for r in rows if r["category_id"] in rank
    )
    return list(dict.fromkeys(dataset for _, dataset in held))


def _why(exc: HTTPException) -> str:
    """The sentence :func:`refuse` put in an error."""
    detail: Any = exc.detail
    return (
        str(detail["message"]) if isinstance(detail, dict) and "message" in detail else str(detail)
    )


async def _catalog(
    state: State, instrument_type: str, region: str, delay: int, universe: str
) -> dict[str, Any]:
    """Whether this market has been downloaded, and how far along if it is downloading.

    Every lab that spends the allowance needs a synced scope, so the first screen can say
    so and offer the download rather than show a disabled button with no explanation.
    """
    from ..catalog.sync import serialise_run

    try:
        synced = await state.queries.synced_tuples()
    except Exception:  # noqa: BLE001
        # A catalog that will not open is diagnosed on the data screen; here it simply
        # means nothing is downloaded yet.
        synced = []

    match = next(
        (
            row
            for row in synced
            if row["region"] == region
            and int(row["delay"]) == delay
            and row["universe"] == universe
            and row["instrument_type"] == instrument_type
        ),
        None,
    )

    running = next(
        (r for r in await state.sync.runs(limit=10) if r.status == SyncStatus.RUNNING), None
    )

    return {
        "scope": scope_label(instrument_type, region, delay, universe),
        "synced": match is not None,
        "fields": int(match["fields"]) if match else 0,
        "running": serialise_run(running) if running else None,
        "anySynced": bool(synced),
    }


async def _you(state: State, session: Any, stored_email: str | None) -> dict[str, Any]:
    granted = list(session.permissions or [])
    # Falling back to the local part of the email keeps the greeting working when the
    # profile is unavailable, rather than showing an account id to someone new.
    full_name = session.full_name
    if not full_name and session.authenticated:
        await state.auth.get_user_profile()
        full_name = session.full_name
    if not full_name and stored_email:
        full_name = stored_email.split("@")[0].replace(".", " ").title()

    return {
        "signedIn": session.authenticated,
        "email": stored_email,
        "userId": session.user_id,
        "fullName": full_name or session.user_id,
        "permissions": granted,
        # Surfaced separately because it is the one that decides whether a day's work
        # takes hours or weeks.
        "canRunTenAtOnce": "MULTI_SIMULATION" in granted,
        "verificationUrl": session.verification_url,
    }


async def simulations_today(state: State) -> dict[str, Any]:
    allowance = DAILY_SIMULATIONS
    used = await state.tracker.used_today()
    snapshot = await state.tracker.latest_quota()

    # The platform's own figure wins whenever we have one from today.
    exact = False
    limit, remaining = allowance, max(0, allowance - used)
    if snapshot is not None and snapshot.remaining is not None:
        observed = snapshot.observed_at
        if observed and _same_platform_day(observed):
            exact = True
            limit = snapshot.limit_total or allowance
            remaining = snapshot.remaining

    resets_in = seconds_until_reset(tz=PLATFORM_TZ)
    engine = await state.engine.status()
    # Queued work has not been sent, so it is not in ``used`` — but it is spoken for, and
    # counting it as waste would tell someone who just queued their day they did nothing.
    queued = int(engine.get("queuedTotal") or 0)
    unspoken = max(0, remaining - queued)

    return {
        "limit": limit,
        "used": used if not exact else max(0, limit - remaining),
        "remaining": remaining,
        "queued": queued,
        "unspoken": unspoken,
        "exact": exact,
        "resetsInSeconds": round(resets_in),
        "resetsAt": "midnight US Eastern",
        "engine": engine,
        "headline": _headline(remaining, limit, queued),
    }


def _headline(remaining: int, limit: int, queued: int = 0) -> str:
    """The sentence at the top of the Dashboard.

    Names what is still unused while there is some, and stops nagging once the day is
    claimed. No clock in it — the reset countdown is shown on its own.
    """
    if limit <= 0:
        return "No simulations available today."
    if remaining <= 0:
        return "You have used every simulation today. Nothing was wasted."

    unspoken = max(0, remaining - queued)
    if unspoken <= 0:
        return "Every simulation left today is queued. Nothing is going to waste."

    share = unspoken / limit
    tail = f" {queued:,} more are already queued." if queued else ""
    if share > 0.9:
        return (
            f"{unspoken:,} simulations are unused today. They cannot be saved for tomorrow.{tail}"
        )
    if share > 0.5:
        return f"{unspoken:,} simulations left today.{tail}"
    return f"{unspoken:,} left of {limit:,}. Good day so far.{tail}"


def _same_platform_day(moment: datetime) -> bool:
    from datetime import UTC

    now = datetime.now(UTC).astimezone(PLATFORM_TZ)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(PLATFORM_TZ).date() == now.date()


async def _assistant(state: State, keys: list[Any], enabled: list[Any]) -> dict[str, Any]:
    """The Google AI Studio side: also a daily budget, also wasted if unused.

    Reported per key as well as in total, because the whole reason to add a second
    account is that the allowance is per account and simply doubles.
    """
    budget = []
    remaining_total = 0
    if enabled:
        status = await state.llm.keys.status(state.llm.registry)
        budget = status["budget"]
        remaining_total = sum(b["remainingToday"] for b in budget)

    return {
        "keys": len(keys),
        "enabledKeys": len(enabled),
        "requestsRemainingToday": remaining_total,
        "budget": budget,
        "resetsInSeconds": min((b["resetInSeconds"] for b in budget), default=None),
        "headline": _assistant_headline(len(enabled), len(budget), remaining_total),
    }


def _assistant_headline(enabled: int, models: int, remaining: int) -> str:
    if enabled == 0:
        return (
            "No assistant key yet. It is free, takes a minute, and it is what explains "
            "the data to you in plain English."
        )
    if models == 0:
        return (
            "The assistant has a key but no model. Set one up in LLM Integration, with the "
            "limits your provider shows you."
        )
    if remaining <= 0:
        return (
            "The assistant has used its requests for today. Each model's allowance comes back "
            "at midnight in its own reset time zone."
        )
    plural = "" if enabled == 1 else "s"
    return f"{remaining:,} assistant requests left today across {enabled} key{plural}."


if __name__ == "__main__":
    import sys

    def cell(category: str, multiplier: float, **extra: Any) -> dict[str, Any]:
        found = {"categoryId": category, "region": "USA", "delay": 1, "multiplier": multiplier}
        return found | {"alphaCount": 0, "synced": True} | extra

    cells = [
        cell("pv", 1.2),
        cell("analyst", 1.5),
        cell("news", 2.0, alphaCount=PYRAMID_MIN_ALPHAS),
        cell("model", 3.0, synced=False),
        cell("other", 2.5, region="EUR"),
    ]
    rows = [
        {"dataset_id": dataset, "category_id": category}
        for dataset, category in [
            ("pv1", "pv"),
            ("analyst4", "analyst"),
            ("news12", "news"),
            ("model16", "model"),
            ("other1", "other"),
        ]
    ]
    checks = {
        "only open, synced pyramids of the market, richest first": _open_datasets(
            cells, rows, "USA", 1
        )
        == ["analyst4", "pv1"],
        "another delay has none of them": _open_datasets(cells, rows, "USA", 0) == [],
    }
    if failed := [name for name, ok in checks.items() if not ok]:
        raise SystemExit(f"today self-check failed: {'; '.join(failed)}")
    sys.stdout.write(f"{len(checks)} cases as expected\n")
