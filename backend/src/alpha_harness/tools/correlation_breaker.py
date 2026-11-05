"""Correlation Breaker: re-shape a submittable Alpha that BRAIN says is already in the pool.

Failing ``PROD_CORRELATION_CHECK`` means the daily PnL is Pearson-correlated at 0.70 or more with an
Alpha already in production, and this one's Sharpe is not 10% above it. The signal is sound; it
is the *exposure* that is shared. So the settings are never touched here — region, delay,
universe, neutralization, decay and truncation are the source Alpha's, exactly — and only the
expression changes.

Every recipe binds the original expression to one name, ``alpha``, and wraps that. Binding is
appended after the reader's own statements, so it cannot disturb them: FastExpr runs statements
in order, and a name rebound at the end has already been read by everything above it.

The recipes come from what the platform's own guidance says actually works — keep the residual
the crowded drivers do not explain, orthogonalize against them, trade on catalysts rather than
every day, neutralize inside finer clusters, trade the innovation rather than the level — and
never from adding noise, which BRAIN forbids outright and which decays out of sample anyway.

A re-shape adds operators and data fields. For a Power Pool Alpha that can cost it the pool's
limits, so each recipe is counted the way Power Pool counts and says when it would cross them.
"""

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..brain.platform import (
    GROUPING_FIELDS,
    INSTRUMENT_TYPE,
    MULTI_COUNTRY_REGIONS,
    POWER_POOL_MAX_FIELDS,
    POWER_POOL_MAX_OPERATORS,
    PROD_CORRELATION_CHECK,
)
from ..brain.schemas import FULL_MODE, SimulationRequest, SimulationSettings
from ..catalog.queries import FieldFilter, Tuple4
from ..labs.fastexpr import Node, ParseError, data_fields, operator_count, parse, render
from ..labs.launch import OPERATORS_UNREAD, account_operators
from ..vault.yields import is_power_pool

if TYPE_CHECKING:
    from collections.abc import Sequence

#: What the original expression is bound to. Appending the binding makes it safe whatever the
#: reader already called their own variables.
ALPHA = "alpha"
#: The catalog's type for a grouping field, which is what the Data screen's Group filter keeps.
GROUP_TYPE = "GROUP"


@dataclass(frozen=True, slots=True)
class Compressed:
    """One Alpha's expression, reduced to statements plus the name holding its value."""

    #: The reader's own statements, canonically rendered, in order.
    prelude: tuple[str, ...]
    #: The line binding :data:`ALPHA`, or empty when the program already ends by assigning it.
    binding: str

    def program(self, tail: str, extra: Sequence[str] = ()) -> str:
        """The whole expression: the reader's work, the binding, any recipe setup, then ``tail``."""
        lines = [*self.prelude]
        if self.binding:
            lines.append(self.binding)
        lines.extend(extra)
        lines.append(tail)
        return ";\n".join(lines)

    @property
    def bound(self) -> str:
        """The reader's own work plus the binding: what ``alpha`` means, shown once.

        The recipes are displayed against this rather than repeating it, so a card carries
        only the line that is actually the idea.
        """
        lines = [*self.prelude, *([self.binding] if self.binding else [])]
        return "\n".join(f"{line};" for line in lines)

    @staticmethod
    def transform(tail: str, extra: Sequence[str] = ()) -> str:
        """Just the re-shape, written against ``alpha``."""
        return ";\n".join([*extra, tail])


def compress(expression: str) -> Compressed:
    """Reduce an expression to its statements and a single name holding its value.

    ``rank(close)`` becomes ``alpha = rank(close)``. A multi-statement program keeps every
    statement and gains one more. A program whose last statement already assigns a name is
    bound from that name rather than from a copy of its right-hand side, so nothing is
    computed twice.
    """
    tree = parse(expression)
    statements: tuple[Node, ...] = tree.args if tree.kind == "seq" else (tree,)
    if not statements:
        raise ParseError("The expression is empty.")

    last = statements[-1]
    if last.kind == "assign":
        # The program's value is whatever it last assigned; keep every statement as written.
        prelude = tuple(render(s) for s in statements)
        binding = "" if last.value == ALPHA else f"{ALPHA} = {last.value}"
        return Compressed(prelude=prelude, binding=binding)

    prelude = tuple(render(s) for s in statements[:-1])
    return Compressed(prelude=prelude, binding=f"{ALPHA} = {render(last)}")


@dataclass(frozen=True, slots=True)
class Recipe:
    """One way to re-expose the same signal."""

    id: str
    name: str
    #: Why it breaks correlation, in the reader's terms.
    why: str
    #: Statements the recipe needs before its own line, already using :data:`ALPHA`.
    setup: tuple[str, ...]
    #: The expression the simulation runs.
    tail: str
    operators: tuple[str, ...]
    #: Data and grouping fields it reads, checked against the market before it is offered.
    fields: tuple[str, ...] = ()
    #: What it costs, said before it is run rather than discovered afterwards.
    caution: str = ""
    #: The regions it means something in; empty is every region.
    regions: frozenset[str] = frozenset()
    #: Why this market has nothing to build it from, for a recipe listed so its absence is seen.
    missing: str = ""


#: Ordered as the research ranks them: residualize, orthogonalize, trade the innovation, gate,
#: then regroup; :func:`group_recipes` adds this market's own groupings after these. Reshapes
#: (``signed_power``, a Cauchy ``quantile``, a volume multiplier) are deliberately absent: they
#: move the weights, not the directional bets a PnL correlation is made of.
RECIPES: tuple[Recipe, ...] = (
    Recipe(
        id="resid_returns",
        name="Keep what returns do not explain",
        why=(
            "Regresses the signal on each instrument's own daily returns over a quarter and "
            "keeps only the residual. What the pool shares is often the part of a signal that "
            "is just price reaction; this takes it out one instrument at a time."
        ),
        setup=(),
        tail=f"ts_regression({ALPHA}, returns, 63, rettype = 0)",
        operators=("ts_regression",),
        fields=("returns",),
        caution="A residual moves more day to day than the signal it came from: watch turnover.",
    ),
    Recipe(
        id="resid_size",
        name="Keep what size does not explain",
        why=(
            "Regresses the signal on the instrument's log market cap over a year and keeps the "
            "residual: how the signal drifts with each company's own size, taken out over time "
            "rather than across the market on one day."
        ),
        setup=(),
        tail=f"ts_regression({ALPHA}, log(cap), 252, rettype = 0)",
        operators=("ts_regression", "log"),
        fields=("cap",),
    ),
    Recipe(
        id="neut_momentum",
        name="Orthogonalize against short-term momentum",
        why=(
            "Forces the daily exposure to five-day price momentum to zero. Fundamental and "
            "analyst signals often ride momentum without meaning to."
        ),
        setup=(),
        tail=f"vector_neut({ALPHA}, ts_delta(close, 5))",
        operators=("vector_neut", "ts_delta"),
        fields=("close",),
    ),
    Recipe(
        id="neut_size",
        name="Orthogonalize against size",
        why=(
            "Projects out market capitalisation, the single most shared exposure in the pool. "
            "What is left is the part of the signal that is not a size bet."
        ),
        setup=(),
        tail=f"vector_neut({ALPHA}, log(cap))",
        operators=("vector_neut", "log"),
        fields=("cap",),
    ),
    Recipe(
        id="neut_both",
        name="Orthogonalize against size and momentum, then decay",
        why=(
            "Both crowded vectors projected out in turn, then smoothed. The decay is there to "
            "settle the turnover that orthogonalizing stirs up."
        ),
        setup=(
            f"no_size = vector_neut({ALPHA}, log(cap))",
            "orthogonal = vector_neut(no_size, ts_delta(close, 5))",
        ),
        tail="ts_decay_linear(orthogonal, 3)",
        operators=("vector_neut", "log", "ts_delta", "ts_decay_linear"),
        fields=("cap", "close"),
        caution="Orthogonalizing lifts turnover; the decay is what keeps it inside the limit.",
    ),
    Recipe(
        id="fast_innovation",
        name="Trade the innovation, not the level",
        why=(
            "Subtracts the signal's own twenty-day mean, leaving the part that is new. The "
            "pool is full of models trading the level of the same quantity."
        ),
        setup=(),
        tail=f"ts_av_diff({ALPHA}, 20)",
        operators=("ts_av_diff",),
    ),
    Recipe(
        id="event_gate",
        name="Trade only on volume catalysts",
        why=(
            "Holds its weights between catalysts instead of re-ranking every day. A PnL that "
            "moves on event days cannot track a continuously rebalanced model."
        ),
        setup=("catalyst = volume > 1.25 * adv20",),
        tail=f"trade_when(catalyst, {ALPHA}, -1)",
        operators=("trade_when",),
        fields=("volume", "adv20"),
        caution=(
            "Gating cuts turnover, which must stay above the 1% floor, and a quiet day leaves "
            "few instruments carrying the book: watch the weight test."
        ),
    ),
    Recipe(
        id="cluster_country",
        name="Neutralize inside country and industry",
        why=(
            "Across many countries, industry neutralization alone leaves each country's own "
            "market move in the book. BRAIN's documented double neutralization removes both."
        ),
        setup=("cluster = densify(group_cartesian_product(industry, country))",),
        tail=f"group_neutralize({ALPHA}, cluster)",
        operators=("densify", "group_cartesian_product", "group_neutralize"),
        fields=("industry", "country"),
        regions=MULTI_COUNTRY_REGIONS,
    ),
    Recipe(
        id="cluster_zscore",
        name="Normalize inside sector and size clusters",
        why=(
            "Scores each instrument against peers of the same sector and size band rather than "
            "against the whole market. The pool almost always neutralizes on sector alone."
        ),
        setup=(
            'cap_bucket = bucket(rank(cap), buckets = "0.33, 0.66")',
            "cluster = densify(group_cartesian_product(sector, cap_bucket))",
        ),
        tail=f"group_zscore({ALPHA}, cluster)",
        operators=("bucket", "rank", "densify", "group_cartesian_product", "group_zscore"),
        fields=("cap", "sector"),
        caution=(
            "Three coarse bands on sector, not subindustry: finer clusters leave too few "
            "instruments in a bucket and fail the weight test."
        ),
    ),
    Recipe(
        id="cluster_volatility",
        name="Neutralize inside sector and volatility clusters",
        why=(
            "Compares each instrument only with peers of the same sector and volatility band. "
            "The pool competes in the same static sector buckets; a volatility band cuts the "
            "cross-section along a line they do not."
        ),
        setup=(
            'vol_bucket = bucket(rank(ts_std_dev(returns, 63)), buckets = "0.33, 0.66")',
            "cluster = densify(group_cartesian_product(sector, vol_bucket))",
        ),
        tail=f"group_neutralize({ALPHA}, cluster)",
        operators=(
            "bucket",
            "rank",
            "ts_std_dev",
            "densify",
            "group_cartesian_product",
            "group_neutralize",
        ),
        fields=("returns", "sector"),
        caution="Three bands for the same reason as the size clusters: finer ones starve buckets.",
    ),
)


async def most_used_group(state: Any, scope: Tuple4, *, exclusive: bool) -> dict[str, Any] | None:
    """The ``GROUP`` field most consultants use in this market, the standard groupings aside.

    By users rather than Alphas: one consultant's sweep can multiply an Alpha count, not a
    user count. ``exclusive`` is the Data screen's Region Exclusive filter: a grouping found in
    no other synced region, which no Alpha from elsewhere can have been neutralized on.
    """
    page = await state.queries.fields(
        scope,
        FieldFilter(
            field_types=[GROUP_TYPE],
            region_exclusive=exclusive,
            sort_by="user_count",
            # The standard groupings lead every unfiltered market, so the page has to reach past
            # all of them to hold the first one that is not.
            limit=len(GROUPING_FIELDS) + 10,
        ),
    )
    rows = [r for r in page["results"] if r["field_id"] not in GROUPING_FIELDS]
    return max(rows, key=_usage) if rows else None


def _usage(row: dict[str, Any]) -> tuple[int, int]:
    """Users first; Alphas settle a tie."""
    return int(row.get("user_count") or 0), int(row.get("alpha_count") or 0)


def group_recipes(
    top: dict[str, Any] | None, exclusive: dict[str, Any] | None, region: str
) -> list[Recipe]:
    """Neutralization inside this market's own groupings, alone and paired with sector.

    Paired through ``group_cartesian_product``, never two ``group_neutralize`` in turn: BRAIN's
    double-neutralization article shows the second undoing part of the first, and names this
    pairing (sector with ``sta1_top1000c50``) for markets where country says nothing.
    """
    exclusive_name = "Neutralize inside a region-exclusive grouping"
    out: list[Recipe] = []
    if top is None:
        missing = "no grouping beyond the standard ones covers this market"
        out.append(_unbuilt("group_top", "Neutralize inside the most used grouping", missing))
    else:
        about = f"the grouping most used here after the standard ones ({_users(top)})"
        out.extend(_grouped("group_top", str(top["field_id"]), about))
    if exclusive is None:
        missing = f"no grouping is exclusive to {region} among the synced regions"
        out.append(_unbuilt("group_exclusive", exclusive_name, missing))
    elif top is not None and exclusive["field_id"] == top["field_id"]:
        missing = f"the most used grouping above is already exclusive to {region}"
        out.append(_unbuilt("group_exclusive", exclusive_name, missing))
    else:
        about = (
            f"found in {region} and no other Synced Region, so no Alpha built elsewhere can "
            f"have been neutralized on it ({_users(exclusive)})"
        )
        out.extend(_grouped("group_exclusive", str(exclusive["field_id"]), about))
    return out


def _users(row: dict[str, Any]) -> str:
    return f"{int(row.get('user_count') or 0):,} consultants"


def _grouped(id_: str, field: str, about: str) -> list[Recipe]:
    return [
        Recipe(
            id=id_,
            name=f"Neutralize inside {field}",
            why=(
                f"Compares each instrument only with peers in {field}: {about}. The pool mostly "
                "neutralizes on sector and industry, so peers drawn by another rule move the "
                "book along a line it does not share."
            ),
            setup=(),
            tail=f"group_neutralize({ALPHA}, densify({field}))",
            operators=("group_neutralize", "densify"),
        ),
        Recipe(
            id=f"{id_}_sector",
            name=f"Neutralize inside sector and {field}",
            why=(
                f"Neutralizes inside every pair of sector and {field} at once, the double "
                "neutralization BRAIN suggests where country and industry are one market."
            ),
            setup=(f"cluster = densify(group_cartesian_product(sector, {field}))",),
            tail=f"group_neutralize({ALPHA}, cluster)",
            operators=("densify", "group_cartesian_product", "group_neutralize"),
            fields=("sector",),
            caution=(
                f"If {field} already sits inside sectors the pair is {field} again, and this "
                "repeats the one above."
            ),
        ),
    ]


def _unbuilt(id_: str, name: str, missing: str) -> Recipe:
    return Recipe(id=id_, name=name, why="", setup=(), tail="", operators=(), missing=missing)


def offered(
    recipes: Sequence[Recipe], operators: set[str], fields: set[str], region: str
) -> list[tuple[Recipe, str]]:
    """Each recipe with the reason it cannot run, empty when it can.

    Reported rather than filtered away: a recipe missing because the market has no ``adv20``
    is worth knowing about, and a silently shorter list reads as if there were fewer ideas.
    """
    out: list[tuple[Recipe, str]] = []
    for recipe in recipes:
        missing_ops = [o for o in recipe.operators if o not in operators]
        missing_fields = [f for f in recipe.fields if f not in fields]
        if recipe.missing:
            out.append((recipe, recipe.missing))
        elif recipe.regions and region not in recipe.regions:
            # ``country`` exists in single-country markets too, where this is plain industry.
            out.append(
                (
                    recipe,
                    f"only for regions spanning countries: {', '.join(sorted(recipe.regions))}",
                )
            )
        elif missing_ops:
            out.append((recipe, f"needs {', '.join(missing_ops)}, not on this account"))
        elif missing_fields:
            out.append((recipe, f"needs {', '.join(missing_fields)}, not in this market"))
        else:
            out.append((recipe, ""))
    return out


def requests(
    compressed: Compressed, recipes: Sequence[Recipe], settings: dict[str, Any]
) -> list[SimulationRequest]:
    """One simulation per recipe, every one at the source Alpha's own settings.

    The settings are copied whole and never edited: the whole point is to hold everything
    the Alpha was judged on and vary only what it is exposed to. Always in Full mode, the
    only one that runs the production-correlation check this tool is here to pass.
    """
    held = SimulationSettings.model_validate(settings | {"simulationMode": FULL_MODE})
    return [
        SimulationRequest(settings=held.model_copy(), regular=compressed.program(r.tail, r.setup))
        for r in recipes
    ]


def _correlation(checks: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """BRAIN's own production-correlation check, as it last reported it."""
    for check in checks:
        if str(check.get("name", "")).upper() == PROD_CORRELATION_CHECK:
            return check
    return None


async def plan(state: Any, alpha_id: str) -> dict[str, Any]:
    """Everything the screen shows: the Alpha, its settings, and each recipe's expression.

    Reads BRAIN and the local catalog; simulates nothing.
    """
    problems: list[str] = []
    body = await state.endpoints.alpha_body(alpha_id)
    code = body.get("regular") or body.get("combo") or body.get("selection") or {}
    expression = code.get("code") if isinstance(code, dict) else None
    settings = dict(body.get("settings") or {})
    checks = ((body.get("is") or {}).get("checks")) or []

    if not expression:
        problems.append(f"{alpha_id} has no expression to re-shape.")
        return _empty(alpha_id, settings, checks, problems)
    try:
        compressed = compress(expression)
    except ParseError as exc:
        problems.append(f"Its expression could not be parsed, so it cannot be re-shaped: {exc}")
        return _empty(alpha_id, settings, checks, problems)

    region, delay = str(settings.get("region") or ""), int(settings.get("delay") or 0)
    universe = str(settings.get("universe") or "")
    operators = {
        str(o.get("name")) for o in await account_operators(state, refresh=False) if o.get("name")
    }
    if not operators:
        problems.append(OPERATORS_UNREAD)

    # The Alpha's own market, delay and universe: which groupings are used, and which are
    # exclusive to a region, differ from one to the next.
    scope = Tuple4(
        instrument_type=str(settings.get("instrumentType") or INSTRUMENT_TYPE),
        region=region,
        delay=delay,
        universe=universe,
    )
    top, only_here = await asyncio.gather(
        most_used_group(state, scope, exclusive=False),
        most_used_group(state, scope, exclusive=True),
    )
    recipes = (*RECIPES, *group_recipes(top, only_here, region))

    # A recipe reads fields of its own; one the market does not carry is said so, not hidden.
    wanted = sorted({f for recipe in recipes for f in recipe.fields})
    held = await asyncio.gather(*(state.queries.field_availability(f) for f in wanted))
    fields = {
        name
        for name, rows in zip(wanted, held, strict=True)
        if any(
            r["region"] == region and int(r["delay"]) == delay and r["universe"] == universe
            for r in rows
        )
    }

    power_pool = is_power_pool([c for c in checks if isinstance(c, dict)])
    rows = []
    for recipe, blocked in offered(recipes, operators, fields, region):
        used, read = (
            (0, 0) if recipe.missing else counted(compressed.program(recipe.tail, recipe.setup))
        )
        rows.append(
            {
                "id": recipe.id,
                "name": recipe.name,
                "why": recipe.why,
                "caution": recipe.caution,
                "transform": ""
                if recipe.missing
                else compressed.transform(recipe.tail, recipe.setup),
                "blocked": blocked,
                "operators": used,
                "dataFields": read,
                "overPowerPool": over_power_pool(used, read) if power_pool else "",
            }
        )
    used, read = counted(expression)
    return {
        "alphaId": alpha_id,
        "expression": expression,
        #: What ``alpha`` stands for, shown once above the re-shapes.
        "bound": compressed.bound,
        "settings": held_settings(settings),
        #: The Alpha's settings whole, which is what every simulation is built from.
        "rawSettings": settings,
        "correlation": _correlation(checks),
        "powerPool": power_pool,
        "operators": used,
        "dataFields": read,
        "recipes": rows,
        #: What the task is built from: the fixed recipes and this market's own groupings.
        "recipeSet": recipes,
        "problems": problems,
    }


def counted(program: str) -> tuple[int, int]:
    """Operators and data fields, the way Power Pool counts them."""
    tree = parse(program)
    return operator_count(tree), len(data_fields(tree))


def over_power_pool(operators: int, fields: int) -> str:
    """Why a re-shape would stop being a Power Pool Alpha, empty when it would not."""
    over = [
        *(
            [f"{operators} operators (limit {POWER_POOL_MAX_OPERATORS})"]
            if operators > POWER_POOL_MAX_OPERATORS
            else []
        ),
        *(
            [f"{fields} data fields (limit {POWER_POOL_MAX_FIELDS})"]
            if fields > POWER_POOL_MAX_FIELDS
            else []
        ),
    ]
    return " and ".join(over)


def held_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """The settings every simulation runs at, which are the Alpha's own and are not editable."""
    return {
        "region": settings.get("region"),
        "delay": settings.get("delay"),
        "universe": settings.get("universe"),
        "neutralization": settings.get("neutralization"),
        "decay": settings.get("decay"),
        "truncation": settings.get("truncation"),
    }


def _empty(
    alpha_id: str, settings: dict[str, Any], checks: Sequence[dict[str, Any]], problems: list[str]
) -> dict[str, Any]:
    return {
        "alphaId": alpha_id,
        "expression": "",
        "bound": "",
        "settings": held_settings(settings),
        "rawSettings": settings,
        "correlation": _correlation(checks),
        "powerPool": is_power_pool(list(checks)),
        "operators": None,
        "dataFields": None,
        "recipes": [],
        "problems": problems,
    }
