"""Data Explorer: syncing the catalog and querying it."""

from datetime import date, datetime
from typing import Annotated, Any, Self

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from ..brain.platform import INSTRUMENT_TYPE, REGION_AGNOSTIC_REGION
from ..brain.settings_schema import valid_values
from ..catalog.pyramids import pyramid_grid
from ..catalog.queries import FieldFilter, Tuple4
from ..catalog.sync import SyncTarget
from ..schemas import Out, SyncAllRun
from .deps import State, refuse

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


def scope(
    region: Annotated[str, Query(description="e.g. USA, EUR, GLB")],
    delay: Annotated[int, Query(ge=0, le=1)],
    universe: Annotated[str, Query(description="e.g. TOP3000")],
    instrument_type: Annotated[str, Query(alias="instrumentType")] = INSTRUMENT_TYPE,
) -> Tuple4:
    """The (instrumentType, region, delay, universe) scope every query needs."""
    return Tuple4(instrument_type=instrument_type, region=region, delay=delay, universe=universe)


Scope = Annotated[Tuple4, Depends(scope)]

#: Enough for a focused idea, and short enough to paste whole into any chat LLM.
MAX_OUTLINE_FIELDS = 100
#: Every dataset in the largest category, with room to spare.
MAX_OUTLINE_DATASETS = 1000


# --- wire shapes: DuckDB rows keep their snake_case column names ------------


class Market(Out):
    instrument_type: str
    region: str
    delay: int
    universe: str


class Cancelled(Out):
    cancelled: bool


class PyramidColumn(Out):
    region: str
    delay: int


class PyramidCategory(Out):
    id: str
    name: str


class PyramidCell(Out):
    category_id: str
    region: str
    delay: int
    multiplier: float | None
    alpha_count: int
    #: 3+ alphas this quarter.
    lit: bool
    synced: bool


class Quarter(Out):
    """Calendar quarter in platform time; ``end`` is the next quarter's first day."""

    start: str
    end: str
    today: str


class PyramidGrid(Out):
    columns: list[PyramidColumn]
    categories: list[PyramidCategory]
    cells: list[PyramidCell]
    quarter: Quarter
    #: Submitted Alphas a pyramid needs before BRAIN counts it as formulated.
    alphas_per_pyramid: int


class CatalogScopeRow(BaseModel):
    instrument_type: str
    region: str
    delay: int
    universe: str
    fields: int
    synced_at: datetime | None


class CatalogCounts(BaseModel):
    fields: int
    datasets: int
    categories: int
    subcategories: int


class CatalogSize(BaseModel):
    """What the catalog's data occupies, which is smaller than its file."""

    used_bytes: int


class MonthCount(BaseModel):
    month: date
    fields: int


class CatalogStats(BaseModel):
    coverage_min: float | None = None
    coverage_max: float | None = None
    coverage_median: float | None = None
    alpha_count_min: int | None = None
    alpha_count_max: int | None = None
    user_count_min: int | None = None
    user_count_max: int | None = None
    pyramid_multiplier_min: float | None = None
    pyramid_multiplier_max: float | None = None
    date_coverage_min: float | None = None
    date_coverage_max: float | None = None
    #: Every month a field was added in, oldest first, with how many.
    date_added: list[MonthCount] = []


class DataFieldRow(BaseModel):
    field_id: str
    dataset_id: str | None
    category_id: str | None
    category_name: str | None
    subcategory_id: str | None
    subcategory_name: str | None
    description: str | None
    field_type: str | None
    #: BRAIN's "Instrument Coverage": the share of the universe the field has a value for.
    coverage: float | None
    #: BRAIN's "Date Coverage": the share of the history it has a value for. A field can be
    #: complete on one and threadbare on the other, so neither stands in for the other.
    date_coverage: float | None
    user_count: int | None
    alpha_count: int | None
    pyramid_multiplier: float | None
    #: JSON array text.
    themes: str | None
    #: BRAIN's "Date added": when the field first appeared in this market. Null until the
    #: market is downloaded again, because it was not stored before.
    date_created: date | None


class DataFieldDetail(DataFieldRow):
    instrument_type: str
    region: str
    delay: int
    universe: str
    synced_at: datetime | None


class FieldPage(BaseModel):
    total: int
    limit: int
    offset: int
    results: list[DataFieldRow]


class CategoryFacet(BaseModel):
    id: str
    name: str | None
    n: int


class SubcategoryFacet(CategoryFacet):
    category_id: str | None


class DatasetFacet(BaseModel):
    id: str
    category_id: str | None
    subcategory_id: str | None
    n: int


class TypeFacet(BaseModel):
    id: str
    n: int


class AvailabilityCounts(BaseModel):
    """Fields each availability toggle would show if pressed, under the other filters."""

    region_agnostic: int
    region_exclusive: int


class CatalogFacets(BaseModel):
    categories: list[CategoryFacet]
    subcategories: list[SubcategoryFacet]
    datasets: list[DatasetFacet]
    types: list[TypeFacet]
    availability: AvailabilityCounts
    #: Fields per month added, under every filter but the month range itself.
    date_added: list[MonthCount]


class FieldAvailabilityRow(BaseModel):
    instrument_type: str
    region: str
    delay: int
    universe: str
    coverage: float | None
    alpha_count: int | None
    field_type: str | None


class DatasetRow(BaseModel):
    dataset_id: str
    name: str | None
    description: str | None
    category_id: str | None
    category_name: str | None
    subcategory_id: str | None
    subcategory_name: str | None
    coverage: float | None
    value_score: float | None
    user_count: int | None
    alpha_count: int | None
    field_count: int | None
    pyramid_multiplier: float | None


# --- syncing --------------------------------------------------------------


async def _markets(state: State) -> list[SyncTarget]:
    """Every EQUITY market the account can simulate, from BRAIN's own settings schema.

    A new region or universe is picked up without a code change. Region ``ALL`` is in here
    like any other: it is drawn in the sync matrix so an account that can run region-agnostic
    alphas can see whether it holds them. What it is *not* is part of a sync by default —
    see :func:`start_sync_all`.
    """
    schema = (
        await state.metadata.cached_settings_schema() or await state.metadata.refresh_metadata()
    )
    # EQUITY only, the one instrument type the Data Explorer offers.
    base: dict[str, Any] = {"instrumentType": INSTRUMENT_TYPE}
    return [
        SyncTarget(
            instrument_type=INSTRUMENT_TYPE, region=region, delay=int(delay), universe=universe
        )
        for region in valid_values(schema, "region", base)
        for delay in valid_values(schema, "delay", {**base, "region": region})
        for universe in valid_values(schema, "universe", {**base, "region": region, "delay": delay})
    ]


@router.get("/markets")
async def markets(state: State) -> list[Market]:
    """Every market BRAIN offers: what the sync matrix draws."""
    return [
        Market(
            instrument_type=t.instrument_type, region=t.region, delay=t.delay, universe=t.universe
        )
        for t in await _markets(state)
    ]


@router.post("/sync-all")
async def start_sync_all(state: State) -> SyncAllRun:
    """Download every ordinary market: all fields first, then dataset details.

    Region ``ALL`` is not one of them; it has :func:`start_sync_region_agnostic` to itself.

    Runs in the background; progress, including each market's state, arrives over the
    WebSocket ``sync`` topic.
    """
    targets = [t for t in await _markets(state) if t.region != REGION_AGNOSTIC_REGION]
    if not targets:
        raise refuse(
            503,
            "no_markets",
            "BRAIN's settings list no markets to download. Sign in again and retry.",
        )
    return SyncAllRun.model_validate(await state.sync.start_all(targets))


@router.post("/sync-region-agnostic")
async def start_sync_region_agnostic(state: State) -> SyncAllRun:
    """Download the region-agnostic market: every universe of region ``ALL``.

    Its own route because it is its own download. BRAIN serves this market only fifty fields
    at a time, so it is read dataset by dataset — about ten minutes per universe where an
    ordinary market is seconds — and only an account running region-agnostic alphas needs it.
    """
    targets = [t for t in await _markets(state) if t.region == REGION_AGNOSTIC_REGION]
    if not targets:
        raise refuse(
            403,
            "no_region_agnostic",
            "This account cannot run region-agnostic simulations, so BRAIN offers no "
            "all-regions market to download.",
        )
    return SyncAllRun.model_validate(await state.sync.start_all(targets))


@router.post("/sync/runs/{run_id}/cancel")
async def cancel_run(run_id: int, state: State) -> Cancelled:
    return Cancelled(cancelled=await state.sync.cancel(run_id))


@router.get("/pyramids")
async def pyramids(state: State) -> PyramidGrid:
    """Every pyramid: its multiplier, this quarter's alpha count, and download state."""
    return PyramidGrid.model_validate(await pyramid_grid(state.endpoints, state.catalog))


@router.get("/scopes")
async def scopes(state: State) -> list[CatalogScopeRow]:
    """Which scopes hold data locally, and how much."""
    return [CatalogScopeRow.model_validate(r) for r in await state.queries.synced_tuples()]


# --- reading --------------------------------------------------------------


@router.get("/counts")
async def counts(scope: Scope, state: State) -> CatalogCounts:
    """Datasets / categories / subcategories / fields for one scope."""
    return CatalogCounts.model_validate(await state.queries.counts(scope))


@router.get("/size")
async def size(state: State) -> CatalogSize:
    """How much the catalog's data actually takes up."""
    return CatalogSize(used_bytes=await state.catalog.used_bytes())


@router.get("/stats")
async def stats(scope: Scope, state: State) -> CatalogStats:
    """Value ranges, so filter controls can bound themselves to real data."""
    return CatalogStats.model_validate(await state.queries.stats(scope))


#: Module-level singleton so the default is not rebuilt per request.
DEFAULT_FIELD_FILTER = FieldFilter()


@router.post("/fields")
async def fields(
    scope: Scope,
    state: State,
    filters: Annotated[FieldFilter, Body()] = DEFAULT_FIELD_FILTER,
) -> FieldPage:
    """Filtered, sorted, paginated data fields.

    POST rather than GET because the filter set is large and structured; the operation
    is still a pure read.
    """
    return FieldPage.model_validate(await state.queries.fields(scope, filters))


@router.post("/facets")
async def facets(
    scope: Scope,
    state: State,
    filters: Annotated[FieldFilter, Body()] = DEFAULT_FIELD_FILTER,
) -> CatalogFacets:
    """Categories / subcategories / datasets / types with counts under the other filters."""
    return CatalogFacets.model_validate(await state.queries.facets(scope, filters))


@router.get("/fields/{field_id}")
async def field_detail(field_id: str, scope: Scope, state: State) -> DataFieldDetail:
    row = await state.queries.field(scope, field_id)
    if row is None:
        raise HTTPException(404, f"{field_id} is not in the catalog for {scope.label}")
    return DataFieldDetail.model_validate(row)


@router.get("/fields/{field_id}/anywhere")
async def field_anywhere(field_id: str, state: State) -> DataFieldDetail:
    """A field as any market describes it, for whatever only needs to know what it is."""
    row = await state.queries.field_anywhere(field_id)
    if row is None:
        raise HTTPException(404, f"{field_id} is not in the catalog")
    return DataFieldDetail.model_validate(row)


@router.get("/field-search")
async def field_search(
    state: State,
    search: Annotated[str, Query(min_length=1, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=200)] = 40,
) -> list[DataFieldRow]:
    """Fields named or described by ``search`` in any market, each once, the most used first."""
    return [
        DataFieldRow.model_validate(r) for r in await state.queries.fields_anywhere(search, limit)
    ]


@router.get("/fields/{field_id}/availability")
async def field_availability(field_id: str, state: State) -> list[FieldAvailabilityRow]:
    """Every scope this field exists in.

    Not every field is available in every region/delay/universe, so this is the check
    that stops a template being expanded into simulations that cannot run.
    """
    return [
        FieldAvailabilityRow.model_validate(r)
        for r in await state.queries.field_availability(field_id)
    ]


@router.get("/datasets")
async def datasets(scope: Scope, state: State) -> list[DatasetRow]:
    return [DatasetRow.model_validate(r) for r in await state.queries.datasets(scope)]


class OutlineRequest(BaseModel):
    """Fields picked one by one, or whole datasets: a category or subcategory is its datasets."""

    field_ids: list[str] = Field(default_factory=list, max_length=MAX_OUTLINE_FIELDS)
    dataset_ids: list[str] = Field(default_factory=list, max_length=MAX_OUTLINE_DATASETS)

    @model_validator(mode="after")
    def _one_kind(self) -> Self:
        if not self.field_ids and not self.dataset_ids:
            raise ValueError("Give field_ids or dataset_ids.")
        if self.field_ids and self.dataset_ids:
            raise ValueError("Give field_ids or dataset_ids, not both.")
        return self


class FieldOutline(Out):
    text: str
    #: How many fields ``text`` holds.
    fields: int
    #: Chosen ids this market does not carry; they are left out of ``text``.
    missing: list[str]


def _flat(text: Any) -> str:
    """One line: a description's line breaks would read as new keys."""
    return " ".join(str(text or "").split())


def _pct(value: Any) -> str:
    return "unknown" if value is None else f"{float(value) * 100:.0f}%"


def outline(scope: Tuple4, rows: list[dict[str, Any]]) -> str:
    """Key-value Markdown: Category, Subcategory and Dataset headings, each written once,
    then one short block per field.

    The universe is left out: a field is the same field in every universe of its region.
    Categories and subcategories carry only a name, because BRAIN sends no description for
    either.
    """
    tree: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    for r in rows:
        category = _flat(r["category_name"]) or "Uncategorised"
        subcategory = _flat(r["subcategory_name"]) or "Uncategorised"
        tree.setdefault(category, {}).setdefault(subcategory, {}).setdefault(
            str(r["dataset_id"]), []
        ).append(r)
    lines = ["# Data Fields", f"Region: {scope.region}", f"Delay: {scope.delay}"]

    def heading(text: str) -> None:
        # A blank line only after content: stacked headings read as one path.
        if not lines[-1].startswith("#"):
            lines.append("")
        lines.append(text)

    for category, subcategories in sorted(tree.items()):
        heading(f"## Category: {category}")
        for subcategory, datasets in sorted(subcategories.items()):
            heading(f"### Subcategory: {subcategory}")
            for dataset_id, fields in sorted(datasets.items()):
                heading(f"#### Dataset: {dataset_id}")
                name = _flat(fields[0]["dataset_name"])
                if name and name != dataset_id:
                    lines.append(f"Name: {name}")
                if about := _flat(fields[0]["dataset_description"]):
                    lines.append(f"Description: {about}")
                lines.append("")
                for f in sorted(fields, key=lambda f: str(f["field_id"])):
                    lines += [
                        f"- Field: {f['field_id']}",
                        f"  Description: {_flat(f['description']) or 'none'}",
                        f"  Type: {f['field_type'] or 'unknown'}",
                        f"  Instrument Coverage: {_pct(f['coverage'])}",
                        f"  Date Coverage: {_pct(f['date_coverage'])}",
                    ]
    return "\n".join(lines)


@router.post("/fields/outline")
async def field_outline(scope: Scope, body: OutlineRequest, state: State) -> FieldOutline:
    """The chosen fields as compact text for an LLM, grouped as the catalog is."""
    by_dataset = bool(body.dataset_ids)
    ids = body.dataset_ids if by_dataset else body.field_ids
    rows = await state.queries.fields_by_id(scope, ids, whole_datasets=by_dataset)
    found = {r["dataset_id" if by_dataset else "field_id"] for r in rows}
    return FieldOutline(
        text=outline(scope, rows), fields=len(rows), missing=[i for i in ids if i not in found]
    )
