"""Reading the catalog: filters, facets, stats and field availability.

Every field is stored once per ``(instrumentType, region, delay, universe)`` tuple, so
browsing a market is a filter on one table and availability is a group-by across tuples.

All values are parameterised; the only interpolated identifiers are whitelisted column
names, because a user-supplied sort key must never reach SQL directly.
"""

import asyncio
from datetime import date
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field

from ..brain.schemas import REGION_AGNOSTIC_REGION
from .search import MATCH, TABLE, query_terms
from .search import ready as search_ready

if TYPE_CHECKING:
    from ..db.duck import Catalog

#: Sortable columns. Anything not in here is rejected rather than interpolated.
SORTABLE = {
    "field_id",
    "dataset_id",
    "category_id",
    "coverage",
    "date_coverage",
    "date_created",
    "user_count",
    "alpha_count",
    "pyramid_multiplier",
    "field_type",
}

#: Ordering by how well a row answers the search, which is not a column on the table.
RELEVANCE = "relevance"

#: ``data_field`` narrowed to the rows the query matches, each carrying its BM25 score.
#: Scoring inside a subquery and filtering on the alias evaluates BM25 once; repeating
#: ``match_bm25(...) IS NOT NULL`` in the WHERE would evaluate it again for every row.
SCORED_SOURCE = (
    f"data_field JOIN (SELECT field_id, score FROM "  # noqa: S608 - module constants
    f"(SELECT field_id, {MATCH} AS score FROM {TABLE})"
    " WHERE score IS NOT NULL) USING (field_id)"
)

#: How ``search`` is read. ``smart`` ranks whole words against the id and the description;
#: ``text`` is the literal substring match, which is still the only way to find a fragment
#: like ``_eps_`` in the middle of an id.
SMART = "smart"


def scope_label(instrument_type: str, region: str, delay: int, universe: str) -> str:
    """``EQUITY/USA/D1/TOP3000``: a catalog scope as logs and screens name it."""
    return f"{instrument_type}/{region}/D{delay}/{universe}"


class Tuple4(BaseModel):
    """A catalog scope."""

    #: Reused predicate; ``params`` supplies its four placeholders in order.
    WHERE: ClassVar[str] = "instrument_type = ? AND region = ? AND delay = ? AND universe = ?"

    instrument_type: str = "EQUITY"
    region: str
    delay: int
    universe: str

    @property
    def params(self) -> list[Any]:
        return [self.instrument_type, self.region, self.delay, self.universe]

    @property
    def label(self) -> str:
        return scope_label(self.instrument_type, self.region, self.delay, self.universe)


class FieldFilter(BaseModel):
    """Everything the Data Explorer can narrow on.

    Every numeric attribute the API exposes is filterable — hiding a column the platform
    returns would defeat the purpose of the tab.
    """

    search: str | None = None
    dataset_ids: list[str] = Field(default_factory=list)
    #: Single fields, as ticked in the Data Explorer: a lab told to use only these.
    field_ids: list[str] = Field(default_factory=list)
    category_ids: list[str] = Field(default_factory=list)
    field_types: list[str] = Field(default_factory=list)

    coverage_min: float | None = None
    coverage_max: float | None = None
    alpha_count_min: int | None = None
    alpha_count_max: int | None = None
    user_count_min: int | None = None
    user_count_max: int | None = None
    pyramid_multiplier_min: float | None = None
    pyramid_multiplier_max: float | None = None
    date_coverage_min: float | None = None
    date_coverage_max: float | None = None
    #: When BRAIN first offered the field here, both ends inclusive.
    date_created_from: date | None = None
    date_created_to: date | None = None

    #: Region ``ALL`` only: how many regions carry the field, both ends included.
    region_coverage_min: int | None = None
    region_coverage_max: int | None = None
    #: The description holds at least one of these, as letters anywhere, any case.
    keywords: list[str] = Field(default_factory=list)

    #: What a selection of fields leaves out: these datasets, fields added in this date range
    #: (either end may be open), and descriptions holding any of these words. A field with no
    #: date or no description is never left out by the date or the words.
    exclude_dataset_ids: list[str] = Field(default_factory=list)
    exclude_date_min: date | None = None
    exclude_date_max: date | None = None
    exclude_keywords: list[str] = Field(default_factory=list)

    #: Only fields that also exist in region ``ALL`` — the ones an idea could be run
    #: region-agnostically on. Meaningless when the scope already is ``ALL``.
    region_agnostic: bool = False

    #: Only fields found in no other synced region, region ``ALL`` included.
    region_exclusive: bool = False

    #: ``smart`` (ranked words) or ``text`` (literal substring).
    search_mode: str = SMART

    sort_by: str = "alpha_count"
    sort_desc: bool = True
    limit: int = 100
    offset: int = 0

    def terms(self) -> str | None:
        """The search as BM25 terms, or ``None`` when it is not a ranked search at all."""
        if not self.search or self.search_mode != SMART:
            return None
        return query_terms(self.search)

    def order_clause(self, *, scored: bool = False) -> str:
        if self.sort_by == RELEVANCE:
            # Falls back to the default column when there is no score to order by, so a
            # stale relevance sort cannot empty the ordering.
            return "ORDER BY score DESC, field_id ASC" if scored else "ORDER BY alpha_count DESC"
        column = self.sort_by if self.sort_by in SORTABLE else "alpha_count"
        direction = "DESC" if self.sort_desc else "ASC"
        # NULLS LAST keeps unpopulated metrics from crowding the top of a descending sort.
        return f"ORDER BY {column} {direction} NULLS LAST, field_id ASC"

    def where(self, scope: Tuple4, *, search: bool = True) -> tuple[str, list[Any]]:
        """Every filter, narrowing by the search as a substring unless told not to.

        ``search=False`` is for a caller that has joined :data:`SCORED_SOURCE`, which already
        drops the rows that do not match — repeating that here would score the whole index a
        second time for the same answer. Everyone else, including a smart search with no
        index to rank against, still gets the substring match.
        """
        clauses = [Tuple4.WHERE]
        params: list[Any] = list(scope.params)

        if not search:
            pass
        elif self.search:
            # `_` and `%` are LIKE wildcards, and field ids are full of underscores: without
            # escaping, searching `_eps_` matches "steps" and "reps" too.
            escaped = (
                self.search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            needle = f"%{escaped}%"
            clauses.append(
                r"(lower(field_id) LIKE ? ESCAPE '\' OR lower(description) LIKE ? ESCAPE '\')"
            )
            params.extend([needle, needle])

        for column, values in (
            ("field_id", self.field_ids),
            ("dataset_id", self.dataset_ids),
            ("category_id", self.category_ids),
            ("field_type", self.field_types),
        ):
            if values:
                placeholders = ", ".join("?" for _ in values)
                clauses.append(f"{column} IN ({placeholders})")
                params.extend(values)

        for column, value, op in (
            ("coverage", self.coverage_min, ">="),
            ("coverage", self.coverage_max, "<="),
            ("alpha_count", self.alpha_count_min, ">="),
            ("alpha_count", self.alpha_count_max, "<="),
            ("user_count", self.user_count_min, ">="),
            ("user_count", self.user_count_max, "<="),
            ("pyramid_multiplier", self.pyramid_multiplier_min, ">="),
            ("pyramid_multiplier", self.pyramid_multiplier_max, "<="),
            ("date_coverage", self.date_coverage_min, ">="),
            ("date_coverage", self.date_coverage_max, "<="),
            ("date_created", self.date_created_from, ">="),
            ("date_created", self.date_created_to, "<="),
        ):
            if value is not None:
                clauses.append(f"{column} {op} ?")
                params.append(value)

        if self.region_coverage_min is not None:
            clauses.append("region_coverage >= ?")
            params.append(self.region_coverage_min)
        if self.region_coverage_max is not None:
            clauses.append("region_coverage <= ?")
            params.append(self.region_coverage_max)
        if words := _words(self.keywords):
            clauses.append(_any_word(len(words)))
            params.extend(words)

        if self.exclude_dataset_ids:
            placeholders = ", ".join("?" for _ in self.exclude_dataset_ids)
            clauses.append(f"(dataset_id IS NULL OR dataset_id NOT IN ({placeholders}))")
            params.extend(self.exclude_dataset_ids)
        window = [
            (op, value)
            for op, value in ((">=", self.exclude_date_min), ("<=", self.exclude_date_max))
            if value is not None
        ]
        if window:
            inside = " AND ".join(f"date_created {op} ?" for op, _ in window)
            clauses.append(f"(date_created IS NULL OR NOT ({inside}))")
            params.extend(value for _, value in window)
        if words := _words(self.exclude_keywords):
            clauses.append(f"(description IS NULL OR NOT {_any_word(len(words))})")
            params.extend(words)

        # By field id, across delays: whether a field exists elsewhere is a question about
        # the field, not about the delay it is read at. Never across instrument types.
        if self.region_agnostic and scope.region != REGION_AGNOSTIC_REGION:
            clauses.append(
                "field_id IN (SELECT field_id FROM data_field "
                "WHERE instrument_type = ? AND region = ?)"
            )
            params.extend([scope.instrument_type, REGION_AGNOSTIC_REGION])

        if self.region_exclusive:
            clauses.append(
                "field_id NOT IN (SELECT field_id FROM data_field "
                "WHERE instrument_type = ? AND region <> ?)"
            )
            params.extend([scope.instrument_type, scope.region])

        return " AND ".join(clauses), params


def _words(words: list[str]) -> list[str]:
    """Each word as a LIKE pattern: lower case, wildcards escaped, blanks dropped."""
    return [
        "%" + w.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        for w in (w.strip() for w in words)
        if w
    ]


def _any_word(n: int) -> str:
    return "(" + " OR ".join(r"lower(description) LIKE ? ESCAPE '\'" for _ in range(n)) + ")"


class CatalogQueries:
    """Read-side of the catalog."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    # -- overview --------------------------------------------------------

    async def counts(self, scope: Tuple4) -> dict[str, int]:
        """Headline numbers for the Data Explorer's counts strip."""
        rows = await self.catalog.query(
            f"""
            SELECT
                (SELECT count(*) FROM data_field WHERE {Tuple4.WHERE})            AS fields,
                (SELECT count(DISTINCT dataset_id) FROM data_field WHERE {Tuple4.WHERE})
                                                                                  AS datasets,
                (SELECT count(DISTINCT category_id) FROM data_field
                    WHERE {Tuple4.WHERE} AND category_id IS NOT NULL)             AS categories,
                (SELECT count(DISTINCT subcategory_id) FROM data_field
                    WHERE {Tuple4.WHERE} AND subcategory_id IS NOT NULL)          AS subcategories
            """,  # noqa: S608
            scope.params * 4,
        )
        row = rows[0] if rows else {}
        return {
            key: int(row.get(key) or 0)
            for key in ("fields", "datasets", "categories", "subcategories")
        }

    async def synced_tuples(self) -> list[dict[str, Any]]:
        """Which scopes have data, and how much."""
        return await self.catalog.query(
            """
            SELECT instrument_type, region, delay, universe,
                   count(*) AS fields, max(synced_at) AS synced_at
            FROM data_field
            GROUP BY 1, 2, 3, 4
            ORDER BY region, delay, universe
            """
        )

    # -- fields ----------------------------------------------------------

    async def _all_words_match(
        self, scope: Tuple4, filters: FieldFilter, terms: str | None
    ) -> bool:
        """Whether requiring every word of the search still finds something.

        `fields` learns this from the total it already computes; the facets have no such
        total, so they ask directly — the same question, and so the same answer.
        """
        if not terms or " " not in terms:
            return True
        where, params = filters.where(scope, search=False)
        found = await self.catalog.scalar(
            f"SELECT 1 FROM {SCORED_SOURCE} WHERE {where} LIMIT 1",  # noqa: S608
            [terms, 1, *params],
        )
        return bool(found)

    async def _page(
        self, scope: Tuple4, filters: FieldFilter, terms: str | None, *, conjunctive: bool
    ) -> dict[str, Any]:
        """One page of fields and the total behind it, both narrowed the same way."""
        ranked = terms is not None
        source = SCORED_SOURCE if ranked else "data_field"
        head: list[Any] = [terms, int(conjunctive)] if ranked else []
        where, params = filters.where(scope, search=not ranked)
        scored = ranked and filters.sort_by == RELEVANCE

        total = await self.catalog.scalar(
            f"SELECT count(*) FROM {source} WHERE {where}",  # noqa: S608
            [*head, *params],
        )
        rows = await self.catalog.query(
            f"""
            SELECT field_id, dataset_id, category_id, category_name,
                   subcategory_id, subcategory_name, description, field_type,
                   coverage, date_coverage, user_count, alpha_count, pyramid_multiplier,
                   themes, date_created
            FROM {source}
            WHERE {where}
            {filters.order_clause(scored=scored)}
            LIMIT ? OFFSET ?
            """,  # noqa: S608
            [*head, *params, filters.limit, filters.offset],
        )
        return {
            "total": int(total or 0),
            "limit": filters.limit,
            "offset": filters.offset,
            "results": rows,
        }

    async def fields(self, scope: Tuple4, filters: FieldFilter) -> dict[str, Any]:
        """A filtered, sorted, paginated page of fields plus the total match count.

        Where the index exists the search is answered by joining it, which narrows and ranks
        in one pass. Requiring every word is what makes a multi-word search mean something,
        so that is tried first; the count it produces is also the test of whether it found
        anything, and only an empty one costs a second round with any-of-the-words.
        """
        terms = filters.terms()
        if not (terms and await search_ready(self.catalog)):
            return await self._page(scope, filters, None, conjunctive=True)
        page = await self._page(scope, filters, terms, conjunctive=True)
        if page["total"] == 0 and " " in terms:
            return await self._page(scope, filters, terms, conjunctive=False)
        return page

    async def field(self, scope: Tuple4, field_id: str) -> dict[str, Any] | None:
        rows = await self.catalog.query(
            f"SELECT * FROM data_field WHERE {Tuple4.WHERE} AND field_id = ?",  # noqa: S608
            [*scope.params, field_id],
        )
        return rows[0] if rows else None

    async def fields_by_id(
        self, scope: Tuple4, field_ids: list[str], *, whole_datasets: bool = False
    ) -> list[dict[str, Any]]:
        """The named fields in ``scope`` with their dataset's name and description.

        ``whole_datasets`` reads the ids as datasets and takes every field in them. Unknown
        ids are left out.
        """
        if not field_ids:
            return []
        column = "f.dataset_id" if whole_datasets else "f.field_id"
        on_scope = " AND ".join(
            f"d.{c} = f.{c}" for c in ("instrument_type", "region", "delay", "universe")
        )
        return await self.catalog.query(
            f"""
            SELECT f.field_id, f.description, f.field_type, f.coverage, f.date_coverage,
                   f.category_name, f.subcategory_name, f.dataset_id,
                   d.name AS dataset_name, d.description AS dataset_description
            FROM data_field f
            LEFT JOIN data_set d ON d.dataset_id = f.dataset_id AND {on_scope}
            WHERE f.instrument_type = ? AND f.region = ? AND f.delay = ? AND f.universe = ?
              AND {column} IN ({", ".join("?" for _ in field_ids)})
            """,  # noqa: S608
            [*scope.params, *field_ids],
        )

    async def field_availability(self, field_id: str) -> list[dict[str, Any]]:
        """Every scope this field appears in.

        The check the Template Studio needs before expanding a template: a field that
        exists in USA/D1 may simply not exist in EUR/D0, and simulating it there fails.
        """
        return await self.catalog.query(
            """
            SELECT instrument_type, region, delay, universe, coverage, alpha_count, field_type
            FROM data_field WHERE field_id = ?
            ORDER BY region, delay, universe
            """,
            [field_id],
        )

    async def universes_holding(
        self, region: str, delay: int, field_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Which universes of one equity market hold each of these fields, in one read.

        One query rather than one per field: each is a scan of the whole table, and the
        Template Lab asks on every preview.
        """
        if not field_ids:
            return []
        return await self.catalog.query(
            f"""
            SELECT field_id, universe FROM data_field
            WHERE instrument_type = 'EQUITY' AND region = ? AND delay = ?
              AND field_id IN ({", ".join("?" for _ in field_ids)})
            """,  # noqa: S608 - placeholders only
            [region, delay, *field_ids],
        )

    # -- datasets & facets -----------------------------------------------

    async def datasets(self, scope: Tuple4) -> list[dict[str, Any]]:
        return await self.catalog.query(
            f"""
            SELECT dataset_id, name, description, category_id, category_name,
                   subcategory_id, subcategory_name, coverage, value_score,
                   user_count, alpha_count, field_count, pyramid_multiplier
            FROM data_set WHERE {Tuple4.WHERE}
            ORDER BY value_score DESC NULLS LAST, dataset_id
            """,  # noqa: S608
            list(scope.params),
        )

    async def facets(self, scope: Tuple4, filters: FieldFilter | None = None) -> dict[str, Any]:
        """Distinct values with counts under the other active filters, for the filter controls.

        A facet ignores its own selection, and a level of the Category → Subcategory →
        Dataset hierarchy also ignores the levels below it, so choosing MATRIX still shows
        how many VECTOR fields the rest of the filter would match.
        """
        active = filters or FieldFilter()
        # The same matcher and the same all-of-the-words decision as the table, or a count
        # beside a chip contradicts the rows underneath it.
        terms = active.terms()
        ranked = bool(terms) and await search_ready(self.catalog)
        conjunctive = await self._all_words_match(scope, active, terms) if ranked else True
        table = SCORED_SOURCE if ranked else "data_field"
        head: list[Any] = [terms, int(conjunctive)] if ranked else []

        async def group(
            column: str, ignore: tuple[str, ...], *extra: tuple[str, str]
        ) -> list[dict[str, Any]]:
            """Distinct ``column`` values with counts, plus ``any_value`` of each extra column."""
            narrowed = active.model_copy(update={name: [] for name in ignore})
            where, params = narrowed.where(scope, search=not ranked)
            select_extra = "".join(f", any_value({source}) AS {alias}" for source, alias in extra)
            return await self.catalog.query(
                f"""
                SELECT {column} AS id{select_extra}, count(*) AS n
                FROM {table}
                WHERE {where} AND {column} IS NOT NULL
                GROUP BY {column} ORDER BY n DESC
                """,  # noqa: S608
                [*head, *params],
            )

        # Independent reads, each on its own cursor: run together rather than one by one.
        categories, subcategories, datasets, types, availability, months = await asyncio.gather(
            group("category_id", ("category_ids", "dataset_ids"), ("category_name", "name")),
            # Each level names its parent, so the filters can be picked top-down:
            # Category → Subcategory → Dataset.
            group(
                "subcategory_id",
                ("dataset_ids",),
                ("subcategory_name", "name"),
                ("category_id", "category_id"),
            ),
            group(
                "dataset_id",
                ("dataset_ids",),
                ("category_id", "category_id"),
                ("subcategory_id", "subcategory_id"),
            ),
            group("field_type", ("field_types",)),
            self._availability(scope, active, table, head, ranked),
            self._months(scope, active, table, head, ranked),
        )
        return {
            "categories": categories,
            "subcategories": subcategories,
            "datasets": datasets,
            "types": types,
            "availability": availability,
            "date_added": months,
        }

    async def _months(
        self, scope: Tuple4, active: FieldFilter, table: str, head: list[Any], ranked: bool
    ) -> list[dict[str, Any]]:
        """Fields per month added under every other filter, as the month choices count them.

        The month range is left out, like a chip ignores its own selection, so a month outside
        the range still says what choosing it would show.
        """
        where, params = active.model_copy(
            update={"date_created_from": None, "date_created_to": None}
        ).where(scope, search=not ranked)
        return await self.catalog.query(
            f"""
            SELECT date_created AS month, count(*) AS fields FROM {table}
            WHERE {where} AND date_created IS NOT NULL
            GROUP BY 1 ORDER BY 1
            """,  # noqa: S608
            [*head, *params],
        )

    async def _availability(
        self, scope: Tuple4, active: FieldFilter, table: str, head: list[Any], ranked: bool
    ) -> dict[str, int]:
        """How many fields each availability toggle would show, pressed on its own.

        Like a chip count: everything else stays as filtered, and the toggle's own state is
        what pressing it would make it. Region Agnostic and Region Exclusive cannot both hold
        (a field in region ALL is in another region), so pressing one lets go of the other.
        """
        pressed = {
            "region_agnostic": {"region_agnostic": True, "region_exclusive": False},
            "region_exclusive": {"region_exclusive": True, "region_agnostic": False},
        }

        async def count(update: dict[str, bool]) -> int:
            where, params = active.model_copy(update=update).where(scope, search=not ranked)
            n = await self.catalog.scalar(
                f"SELECT count(*) FROM {table} WHERE {where}",  # noqa: S608
                [*head, *params],
            )
            return int(n or 0)

        found = await asyncio.gather(*(count(update) for update in pressed.values()))
        return dict(zip(pressed, found, strict=True))

    async def stats(self, scope: Tuple4) -> dict[str, Any]:
        """Distribution summary — lets the UI set sensible filter ranges."""
        rows = await self.catalog.query(
            f"""
            SELECT
                min(coverage) AS coverage_min, max(coverage) AS coverage_max,
                median(coverage) AS coverage_median,
                min(alpha_count) AS alpha_count_min,
                max(alpha_count) AS alpha_count_max,
                min(user_count) AS user_count_min,
                max(user_count) AS user_count_max,
                min(pyramid_multiplier) AS pyramid_multiplier_min,
                max(pyramid_multiplier) AS pyramid_multiplier_max,
                min(date_coverage) AS date_coverage_min,
                max(date_coverage) AS date_coverage_max
            FROM data_field WHERE {Tuple4.WHERE}
            """,  # noqa: S608
            scope.params,
        )
        # BRAIN dates a field by month (always the 1st), so the months are the choices.
        months = await self.catalog.query(
            f"""
            SELECT date_created AS month, count(*) AS fields FROM data_field
            WHERE {Tuple4.WHERE} AND date_created IS NOT NULL
            GROUP BY 1 ORDER BY 1
            """,  # noqa: S608
            scope.params,
        )
        return {**(rows[0] if rows else {}), "date_added": months}
