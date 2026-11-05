"""The alpha pool on BRAIN: summary, the Alpha page, check, correlate, edit properties.

**There is no submit endpoint, deliberately.** Submitting an alpha is irreversible, so the
guard against doing it by accident is that no route, service or client wrapper for it
exists. Correlations are slow, rate-limited jobs, so their answers are kept in
``brain_cache`` until the user refreshes.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, RootModel
from sqlalchemy import delete

from .. import preferences
from ..brain.errors import BrainError
from ..brain.platform import PLATFORM_ALPHA_URL
from ..brain.settings_schema import chosen_labels
from ..db.models import BrainCache, utcnow
from ..labs.fastexpr import ParseError, data_fields, parse
from ..llm.prompts import POWER_POOL_DESCRIPTION
from ..schemas import Out
from ..tools import description
from ..vault.yields import checks_of
from .deps import State, refuse

router = APIRouter(prefix="/api/alphas", tags=["alphas"])

CorrelationKind = Literal["self", "prod", "power-pool"]
Refresh = Annotated[bool, Query(description="Ask BRAIN again instead of reading the cache.")]
CachedOnly = Annotated[
    bool, Query(description="Answer only from the cache; never spend BRAIN's budget.")
]
#: The series BRAIN's Alpha page draws, by the key each copy is kept under.
RECORDSETS = {"pnl": "pnl", "sharpe": "sharpe", "turnover": "turnover", "yearly-stats": "yearly"}
Recordset = Literal["pnl", "sharpe", "turnover", "yearly-stats"]
#: BRAIN adds a submitted Alpha's out-of-sample days as they pass, so its series age.
GROWING_SERIES_AGE = timedelta(hours=12)
#: The periods BRAIN reports figures for, each its own block of the Alpha.
PERIODS = ("train", "test", "is", "os", "prod")


class BrainPayload(RootModel[dict[str, Any]]):
    """BRAIN's own JSON, passed through as the platform sent it."""


class AlphaClassification(Out):
    id: str
    name: str


class AlphaInfo(Out):
    alpha_id: str
    type: str | None
    author: str | None
    code: str | None
    #: A Super Alpha's two expressions; null on any other.
    selection: str | None
    combo: str | None
    description: str | None
    name: str | None
    category: str | None
    color: str | None
    tags: list[str]
    stage: str | None
    status: str | None
    grade: str | None
    date_created: str | None
    date_modified: str | None
    #: Every simulation setting BRAIN reports, as it named them.
    settings: dict[str, Any]
    #: Each setting chosen from a list, by BRAIN's label for it: ``Subindustry``.
    setting_labels: dict[str, str]
    classifications: list[AlphaClassification]
    themes: list[dict[str, Any]]
    pyramid_themes: dict[str, Any] | None
    #: BRAIN's block for each period it reports (train, test, is, os, prod), as sent. The IS
    #: checks are the last Check Submission's when there was one.
    periods: dict[str, dict[str, Any]]
    #: A region-agnostic parent's Alphas, one per region.
    children: list[str]
    brain_url: str


class AlphaView(Out):
    alpha: AlphaInfo
    #: Anything that could not be loaded, said plainly.
    problems: list[str]


class AlphaDescription(Out):
    #: In BRAIN's Power Pool template, ready to save.
    description: str
    #: The model that wrote it.
    model: str
    tokens: int


class AlphaProperties(BaseModel):
    name: str | None = None
    category: str | None = None
    color: str | None = None
    tags: list[str] = Field(default_factory=list)
    description: str | None = None


# -- cache ---------------------------------------------------------------


type Fetch = Callable[[], Awaitable[dict[str, Any]]]


async def _stored(state: State, key: str) -> BrainCache | None:
    async with state.db.session() as session:
        return await session.get(BrainCache, key)


async def _store(state: State, key: str, body: dict[str, Any]) -> datetime:
    now = utcnow()
    async with state.db.session() as session:
        await session.merge(BrainCache(key=key, body=body, fetched_at=now))
    return now


async def _forget(state: State, *keys: str) -> None:
    async with state.db.session() as session:
        await session.execute(delete(BrainCache).where(BrainCache.key.in_(keys)))


async def _cached(
    state: State, key: str, fetch: Fetch, *, refresh: bool
) -> tuple[dict[str, Any], datetime]:
    """A cached BRAIN answer, fetched and stored when missing or when ``refresh``."""
    if not refresh and (row := await _stored(state, key)) is not None:
        return row.body, row.fetched_at
    body = await fetch()
    return body, await _store(state, key, body)


def _copy(row: BrainCache) -> str:
    # Stored in UTC; unlabelled it read as local.
    return f"showing the copy from {row.fetched_at:%b %d, %H:%M} UTC."


async def _kept(
    state: State, key: str, fetch: Fetch, *, refresh: bool, cached_only: bool
) -> BrainPayload:
    """A kept answer stamped with ``fetchedAt``, or ``{"cached": false}`` when ``cached_only``
    finds none."""
    if cached_only:
        row = await _stored(state, key)
        if row is None:
            return BrainPayload({"cached": False})
        body, fetched = row.body, row.fetched_at
    else:
        body, fetched = await _cached(state, key, fetch, refresh=refresh)
    return BrainPayload(body | {"cached": True, "fetchedAt": fetched.isoformat()})


# -- reading BRAIN's shapes ---------------------------------------------------


async def _is_checks(
    state: State, alpha_id: str, simulated: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The last Check Submission's answer, quota check and all. Before one is kept, the
    Alpha's own checks, each replaced by the vault's resolved copy where it holds one."""
    if (row := await _stored(state, f"check:{alpha_id}")) is not None:
        return [c for c in (row.body.get("is") or {}).get("checks") or [] if isinstance(c, dict)]
    stored = (await state.alphas.by_ids([alpha_id])).get(alpha_id) or {}
    resolved = checks_of(stored.get("checks"))
    merged: list[dict[str, Any]] = []
    for check in simulated:
        found = next((r for r in resolved if r.get("name") == check.get("name")), check)
        if found is not check:
            resolved.remove(found)
        merged.append(found)
    return merged + resolved


async def _info(state: State, alpha_id: str, body: dict[str, Any]) -> AlphaInfo:
    regular, combo, selection = (body.get(k) for k in ("regular", "combo", "selection"))
    # An older Alpha sends its expression as a bare string.
    code = regular.get("code") if isinstance(regular, dict) else regular
    described = next((p for p in (regular, combo, selection) if isinstance(p, dict)), {})
    settings = body.get("settings") or {}
    periods = {p: dict(block) for p in PERIODS if isinstance(block := body.get(p), dict)}
    if "is" in periods:
        simulated = [c for c in periods["is"].get("checks") or [] if isinstance(c, dict)]
        periods["is"]["checks"] = await _is_checks(state, alpha_id, simulated)
    return AlphaInfo(
        alpha_id=alpha_id,
        type=body.get("type"),
        author=body.get("author"),
        code=code if isinstance(code, str) else None,
        selection=selection.get("code") if isinstance(selection, dict) else None,
        combo=combo.get("code") if isinstance(combo, dict) else None,
        description=described.get("description"),
        name=body.get("name"),
        category=body.get("category"),
        color=body.get("color"),
        tags=[t for t in body.get("tags") or [] if isinstance(t, str)],
        stage=body.get("stage"),
        status=body.get("status"),
        grade=body.get("grade"),
        date_created=body.get("dateCreated"),
        date_modified=body.get("dateModified"),
        settings=settings,
        setting_labels=chosen_labels(await state.metadata.cached_settings_schema(), settings),
        classifications=[
            AlphaClassification.model_validate(c)
            for c in body.get("classifications") or []
            if isinstance(c, dict) and c.get("id") and c.get("name")
        ],
        themes=[t for t in body.get("themes") or [] if isinstance(t, dict)],
        pyramid_themes=body.get("pyramidThemes") or None,
        periods=periods,
        children=[str(c) for c in body.get("children") or []],
        brain_url=f"{PLATFORM_ALPHA_URL}{alpha_id}",
    )


# -- routes --------------------------------------------------------------------


@router.get("/summary")
async def summary(state: State) -> BrainPayload:
    """Counts by stage and status — the shape of your pool at a glance."""
    return BrainPayload(await state.endpoints.alphas_summary())


@router.get("/{alpha_id}/page")
async def page(alpha_id: str, state: State) -> AlphaView:
    """The Alpha as BRAIN's own page shows it; its series come from :func:`recordset`.

    Read from BRAIN on every open, since its checks and properties change, falling back to
    the last copy when BRAIN cannot answer.
    """
    problems: list[str] = []
    try:
        body, _ = await _cached(
            state,
            f"alpha:{alpha_id}",
            lambda: state.endpoints.alpha_body(alpha_id),
            refresh=True,
        )
    except BrainError as exc:
        stale = await _stored(state, f"alpha:{alpha_id}")
        if stale is None:
            raise
        body = stale.body
        problems.append(f"BRAIN did not answer ({exc.message}); {_copy(stale)}")
    return AlphaView(alpha=await _info(state, alpha_id, body), problems=problems)


@router.get("/{alpha_id}/recordsets/{name}")
async def recordset(alpha_id: str, name: Recordset, state: State) -> BrainPayload:
    """One of the Alpha's series as BRAIN sends it, for the chart and the yearly table.

    An in-sample series never changes, so it is read once. A submitted Alpha's grows a day
    at a time, so its copy is read again once it is :data:`GROWING_SERIES_AGE` old. A copy
    that could not be refreshed comes back with ``problem`` saying so.
    """
    key = f"{RECORDSETS[name]}:{alpha_id}"
    kept = await _stored(state, key)
    alpha = await _stored(state, f"alpha:{alpha_id}")
    growing = alpha is not None and alpha.body.get("stage") not in (None, "IS")
    if kept is not None and not (growing and utcnow() - kept.fetched_at > GROWING_SERIES_AGE):
        return BrainPayload(kept.body)
    try:
        body = await state.endpoints.recordset_body(alpha_id, name)
    except BrainError as exc:
        if kept is None:
            raise
        return BrainPayload(
            kept.body | {"problem": f"BRAIN did not answer ({exc.message}); {_copy(kept)}"}
        )
    await _store(state, key, body)
    return BrainPayload(body)


@router.patch("/{alpha_id}")
async def update_properties(alpha_id: str, body: AlphaProperties, state: State) -> AlphaInfo:
    """Save the Alpha's name, category, colour, tags and description on BRAIN.

    Normalised the way the platform's own client sends them: a blank or ``NONE`` colour,
    a blank name or category become null; an empty description is left out.
    """
    blank = lambda v: v if v and v.strip() and v != "NONE" else None  # noqa: E731
    patch: dict[str, Any] = {
        "name": blank(body.name),
        "category": blank(body.category),
        "color": blank(body.color),
        "tags": [t.strip() for t in body.tags if t.strip()],
    }
    if body.description and body.description.strip():
        patch["regular"] = {"description": body.description}
    updated = await state.endpoints.update_alpha(alpha_id, patch)
    if not updated.get("id"):
        updated = await state.endpoints.alpha_body(alpha_id)
    await _store(state, f"alpha:{alpha_id}", updated)
    return await _info(state, alpha_id, updated)


@router.post("/{alpha_id}/description")
async def write_description(alpha_id: str, state: State) -> AlphaDescription:
    """A Power Pool description in BRAIN's template, by the model chosen in Settings.

    The model sees the expression and only the fields and operators it uses, each with BRAIN's
    own description. Nothing is saved: the page puts it in the editor, and Save sends it.
    Automatic, the choice when none is made, takes whichever model a Key can run that has the
    most requests left today.
    """
    runnable = await state.llm.runnable()
    chosen = (await preferences.load(state.db)).description_model
    if not runnable:
        raise refuse(
            400,
            "no_api_key",
            "Write with AI needs a model one of your Keys can run. Add a Key, and set up a "
            "model for it, under LLM Integration.",
        )
    if chosen and chosen not in {m["ref"] for m in runnable}:
        info = state.llm.registry.get(chosen)
        raise refuse(
            400,
            "no_api_key",
            f"{chosen} is no longer set up. Choose another model in Settings › Alphas."
            if info is None
            else f"{info.id} can't run: no enabled Key for it. Add one under LLM Integration, "
            "or choose another model in Settings › Alphas.",
        )
    body, _ = await _cached(
        state, f"alpha:{alpha_id}", lambda: state.endpoints.alpha_body(alpha_id), refresh=False
    )
    code = (body.get("regular") or {}).get("code")
    try:
        tree = parse(code) if isinstance(code, str) else None
    except ParseError:
        tree = None
    if tree is None or not isinstance(code, str):
        raise refuse(
            422,
            "unreadable_expression",
            f"{alpha_id} has no expression this app can read, so there is nothing to describe.",
        )
    fields = data_fields(tree, grouping=True)
    known = await state.queries.described(fields, str((body.get("settings") or {}).get("region")))
    listed = await state.metadata.cached_operators() or await state.metadata.refresh_operators()
    by_name = {str(o.get("name")): o for o in listed}
    user = description.message(
        code,
        [
            {
                "name": name,
                "type": (known.get(name) or {}).get("field_type"),
                "description": (known.get(name) or {}).get("description"),
            }
            for name in fields
        ],
        [by_name.get(name) or {"name": name} for name in description.operators_in(tree)],
    )
    tokens = 0
    # A part too short is asked for once more, naming it: free models often stop early.
    retried = False
    while True:
        answer = await state.llm.generate(
            system=POWER_POOL_DESCRIPTION,
            user=user,
            model_ref=chosen or str(runnable[0]["ref"]),
            response_schema=description.SCHEMA,
            temperature=0.3,
        )
        tokens += answer.total_tokens
        written = description.read(answer.text)
        if written is None:
            raise refuse(
                502,
                "not_in_template",
                f"{answer.model} did not answer in the template. Try again, or choose another "
                "model in Settings › Alphas.",
            )
        if not (short := description.short(written)):
            break
        if retried:
            raise refuse(
                502,
                "too_short",
                f"{answer.model} wrote fewer than {description.MIN_PART} characters for "
                f"{', '.join(short)}, twice. Try again, or choose another model in "
                "Settings › Alphas.",
            )
        retried = True
        user += (
            f"\n\nYour last answer was too short for {', '.join(short)}. Each part needs at "
            f"least {description.MIN_PART} characters."
        )
    return AlphaDescription(description=written, model=answer.model, tokens=tokens)


@router.get("/{alpha_id}/check")
async def check_alpha(alpha_id: str, state: State) -> BrainPayload:
    """Re-run the submission checks without submitting.

    Changes nothing on the platform. It does update the local copy, so an alpha that has
    just resolved appears on the submit screen without waiting for the next backfill.
    """
    body = await state.endpoints.check_alpha(alpha_id)
    checks = ((body.get("is") or {}).get("checks")) or []
    if checks:
        await state.alphas.save_checks(alpha_id, checks)
        # Kept whole for the Alpha page, which shows it as BRAIN's own page does after a check.
        await _store(state, f"check:{alpha_id}", body)
    # A region-agnostic parent answers for its children too, each under its own id: saved, so
    # every child carries BRAIN's final word rather than the checks it was simulated with.
    for child_id, child_checks in ((body.get("is") or {}).get("subregions") or {}).items():
        if isinstance(child_checks, list) and child_checks:
            await state.alphas.save_checks(str(child_id), child_checks)
            await _store(state, f"check:{child_id}", {"is": {"checks": child_checks}})
            await _forget(state, f"alpha:{child_id}")
    await _forget(state, f"alpha:{alpha_id}")
    return BrainPayload(body)


@router.get("/{alpha_id}/correlations/{kind}")
async def correlations(
    alpha_id: str,
    kind: CorrelationKind,
    state: State,
    refresh: Refresh = False,
    cached_only: CachedOnly = False,
) -> BrainPayload:
    """Correlation against your own submitted Alphas, the Power Pool, or production.

    A slow, rate-limited job, so the answer is kept and returned with ``fetchedAt``; only
    ``refresh`` asks BRAIN again. ``cachedOnly`` answers ``{"cached": false}`` when nothing
    is kept, so a page can show old answers without asking.
    """
    return await _kept(
        state,
        f"correlation:{kind}:{alpha_id}",
        lambda: state.endpoints.correlations(alpha_id, kind),
        refresh=refresh,
        cached_only=cached_only,
    )


@router.get("/{alpha_id}/performance")
async def performance(
    alpha_id: str, state: State, refresh: Refresh = False, cached_only: CachedOnly = False
) -> BrainPayload:
    """Your pool's stats before and after this Alpha joins it, kept until refreshed."""
    return await _kept(
        state,
        f"performance:{alpha_id}",
        lambda: state.endpoints.before_and_after(alpha_id),
        refresh=refresh,
        cached_only=cached_only,
    )
