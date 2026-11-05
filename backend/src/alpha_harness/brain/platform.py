"""BRAIN's own limits, defaults and rules: everything this app takes from the platform.

Each value is BRAIN's, quoted from it or measured against it, never a choice of this app's.
When BRAIN changes one, this is the place to change it. The frontend reads the same values
from ``frontend/src/api/platform.ts``, generated from this module by ``lefthook run format``.

Plain data importing nothing from the app, so any module can depend on it. Run as a module, it
prints the TypeScript the frontend imports.
"""

import json
import sys
from typing import Any

# --- simulation settings ----------------------------------------------------------------

INSTRUMENT_TYPE = "EQUITY"
LANGUAGE = "FASTEXPR"
DEFAULT_DECAY = 0
DEFAULT_NEUTRALIZATION = "NONE"
DEFAULT_TRUNCATION = 0.08
DEFAULT_PASTEURIZATION = "ON"
DEFAULT_UNIT_HANDLING = "VERIFY"
#: BRAIN defaults it OFF; this app always sends ON.
DEFAULT_NAN_HANDLING = "ON"
MAX_DECAY = 512
#: The last two of the ten simulated years are held out.
TEST_PERIOD_YEARS = 2
TEST_PERIOD = f"P{TEST_PERIOD_YEARS}Y0M0D"
#: BRAIN's bounds on the test period: ``P0Y0M0D`` to ``P6Y0M0D``, months 0 to 11.
MAX_TEST_PERIOD_YEARS, MAX_TEST_PERIOD_MONTHS = 6, 11
TEST_PERIOD_PATTERN = r"^P[0-6]Y(?:[0-9]|1[01])M0D$"
#: The history BRAIN simulates.
SIMULATION_YEARS = 10

# --- regions and investability ---------------------------------------------------------

#: The region that means "all of them at once". BRAIN offers it only under the
#: ``REGION_AGNOSTIC`` simulation type, so choosing it *is* choosing that type.
REGION_AGNOSTIC_REGION = "ALL"
#: The region BRAIN meters at double rate: see :data:`GLB_SLOTS`.
GLB_REGION = "GLB"
#: Regions spanning several countries, where one country's move lands on a few names and
#: rides along with every industry group.
MULTI_COUNTRY_REGIONS = frozenset({"GLB", "EUR", "ASI", "AMR"})
#: Where the platform's calendar lives, for both the daily quota and date filters.
PLATFORM_TIMEZONE = "America/New_York"

#: maxTrade and maxPosition for each Investability. BRAIN refuses both ON at once: "Max
#: Position and Max Trade cannot both be set to On simultaneously".
INVESTABILITY: dict[str, tuple[str, str]] = {
    "none": ("OFF", "OFF"),
    "max_trade": ("ON", "OFF"),
    "max_position": ("OFF", "ON"),
}
#: The Investability each region takes. BRAIN's settings schema offers Max Position everywhere
#: but refuses it outside the first five, for every delay and universe. A region missing here
#: is offered no Max Position.
REGION_INVESTABILITY: dict[str, tuple[str, ...]] = {
    "USA": ("none", "max_trade", "max_position"),
    "EUR": ("none", "max_trade", "max_position"),
    "GLB": ("none", "max_trade", "max_position"),
    "ASI": ("none", "max_trade", "max_position"),
    "ALL": ("none", "max_trade", "max_position"),
    "JPN": ("none", "max_trade"),
    "CHN": ("none", "max_trade"),
    "DEU": ("none", "max_trade"),
    "GBR": ("none", "max_trade"),
    "IND": ("none", "max_trade"),
    "AMR": ("none", "max_trade"),
}
#: Where an Alpha without Max Trade must keep :data:`INVESTABILITY_SHARPE_RATIO` of its Sharpe
#: under investability constraints to be submitted: BRAIN's Investability Sharpe test.
MAX_TRADE_REGIONS = frozenset({"ASI", "JPN", "HKG", "TWN", "KOR"})
INVESTABILITY_SHARPE_RATIO = 0.7

#: The regions a region-agnostic Alpha is translated into: "data fields in a Region Agnostic
#: Alpha are translated into GLB, USA, EUR, and ASI Alphas".
RA_REGIONS = ("USA", "EUR", "ASI", "GLB")
#: What each region-agnostic universe trades in each region, as BRAIN maps them.
RA_UNIVERSES: dict[str, dict[str, str]] = {
    "LARGE": {"USA": "TOP3000", "EUR": "TOP2500", "ASI": "MINVOL1M", "GLB": "MINVOL1M"},
    "MEDIUM": {"USA": "TOP2000", "EUR": "TOP1200", "ASI": "MINVOL10M", "GLB": "MINVOL10M"},
    "SMALL": {"USA": "TOP1000", "EUR": "TOP800", "ASI": "TOP500", "GLB": "TOPDIV3000"},
}

# --- expressions -----------------------------------------------------------------------

#: Left out of BRAIN's operator count.
UNCOUNTED_OPERATORS = frozenset({"ts_backfill", "group_backfill"})
#: A Power Pool Alpha's most operators and unique data fields.
POWER_POOL_MAX_OPERATORS = 8
POWER_POOL_MAX_FIELDS = 3
POWER_POOL_MIN_SHARPE = 1.0
#: A Power Pool description's least length, and the headings BRAIN's template asks for.
POWER_POOL_DESCRIPTION_MIN_LENGTH = 100
POWER_POOL_DESCRIPTION_HEADINGS = (
    "Idea",
    "Rationale for data used",
    "Rationale for operators used",
)
#: Fields that never count as data, toward Power Pool's field limit or a single dataset.
#: Measured: ``split`` and ``adjfactor`` are MATRIX fields, yet an Alpha reading either keeps
#: both its Power Pool and its single dataset standing, while ``volume`` costs it both.
GROUPING_FIELDS = (
    "market",
    "sector",
    "industry",
    "subindustry",
    "country",
    "exchange",
    "currency",
    "split",
    "adjfactor",
)
#: The grouping fields every market carries, beyond ``market`` itself. The industry ones nest,
#: coarsest first: each sector holds whole industries, each industry whole subindustries.
INDUSTRY_GROUPS = ("sector", "industry", "subindustry")
GEOGRAPHY_GROUPS = ("country", "currency", "exchange")
#: Operators that read pv1 whatever they are given, so BRAIN counts them as using it.
PV1_OPERATORS = frozenset({"inst_pnl", "convert"})

# --- submission checks -----------------------------------------------------------------

#: BRAIN's fitness floors turnover here, so a near-idle Alpha is not rewarded without bound.
FITNESS_TURNOVER_FLOOR = 0.125
#: The consultant Weight test in USA: no stock may hold 8% of the Alpha (10% elsewhere).
MAX_STOCK_WEIGHT_USA = 0.08

#: Checks about the day rather than the Alpha: ``REGULAR_SUBMISSION`` fails once four Alphas
#: have gone in, ``D0_SUBMISSION`` once the D0 allowance is spent. Both reset tomorrow, so a
#: FAIL frozen into an Alpha's row is a fact about an afternoon that outlives it.
QUOTA_CHECKS = frozenset({"REGULAR_SUBMISSION", "D0_SUBMISSION"})
#: Checks that label an Alpha rather than gate it: BRAIN reports them as ``WARNING`` on every
#: Alpha it checks, passing or failing. ``PROD_CORRELATION`` keeps no consultant from
#: submitting either. ``SELF_CORRELATION`` is not one: an Alpha too close to the pool
#: genuinely cannot be submitted.
IGNORED_CHECKS = frozenset(
    {
        "PROD_CORRELATION",
        "MATCHES_COMPETITION",
        "MATCHES_PYRAMID",
        "MATCHES_THEMES",
        "CLUSTER_TEST",
        "OSMOSIS_ALLOCATION",
        "POWER_POOL_DESCRIPTION_LENGTH",
        "POWER_POOL_DESCRIPTION_FORMAT",
    }
)
#: BRAIN attaches this check to an Alpha it considers Power Pool eligible, and to no other.
#: It sits at ``PENDING`` until asked, so its presence is the signal, not its result.
#: Measured: it agrees with the ``Power Pool Alpha`` classification on every submitted Alpha.
POWER_POOL_CHECK = "POWER_POOL_CORRELATION"
PROD_CORRELATION_CHECK = "PROD_CORRELATION"
#: A region-agnostic parent is submittable once this many of its children are.
RA_MIN_PASSING_CHILDREN = 2

#: BRAIN's classifications. Measured: SELF_CORRELATION compares any candidate, Power Pool or
#: not, with every submitted Alpha of its region except those classed Power Pool and nothing
#: else; Power Pool Correlation compares Power Pool ones among themselves.
REGULAR_CLASS = "Regular Alpha"
POWER_POOL_CLASS = "Power Pool Alpha"
REGION_AGNOSTIC_CLASS = "Region Agnostic"

#: ARC2026's Sharpe steps: one point from the first, two from the second.
ARC_ONE_POINT_SHARPE, ARC_TWO_POINTS_SHARPE = 1.58, 2.0

# --- correlation -----------------------------------------------------------------------

#: Against the submitted Alphas of the same region.
SELF_CORRELATION_CEILING = 0.7
PROD_CORRELATION_CEILING = 0.7
#: A Power Pool pair at or above this is refused.
POWER_POOL_CORRELATION_CEILING = 0.5
#: Past the ceiling, BRAIN still takes an Alpha whose Sharpe beats every Alpha it collides
#: with by this factor. Measured: the documented "10% higher than the most correlated" alone
#: admits Alphas BRAIN refuses.
POWER_POOL_ESCAPE = 1.1
#: Correlation is measured over the last four calendar years. Measured: against the full
#: history, 451 of 38,503 pairs land on the other side of the ceiling.
CORRELATION_WINDOW_YEARS = 4

# --- quotas and concurrency ------------------------------------------------------------

#: A consultant's daily allowance, until the day's first POST returns x-ratelimit-* headers.
DAILY_SIMULATIONS = 5000
#: Concurrent simulations BRAIN allows. Accounts without MULTI_SIMULATION get the same
#: slots, one simulation per batch.
SIMULATION_SLOTS = 8
#: Children one multi-simulation may carry.
MULTI_SIMULATION_SIZE = 10
#: Slots one GLB simulation holds: "4 concurrent simulations for GLB Alphas (each simulation
#: now takes 2 slots out of the available 8 slots)", BRAIN announcement 2025-09-23. A
#: multi-simulation is one simulation to BRAIN, so a GLB batch holds two however many it carries.
GLB_SLOTS = 2
#: Most regions a region-agnostic simulation is translated into, and so the most daily
#: simulations it can cost: BRAIN charges one per child region. A ceiling: the expression runs
#: on the intersection of the regions its fields cover, which is not known before it is sent.
RA_CHILDREN = 4
#: Slots a region-agnostic simulation typically holds: "the sum of RA Child Alphas' concurrent
#: quota", GLB counting twice. Measured on this account's RA Alphas: ten cost three, two cost
#: two, one cost five. Guessing low is the cheap direction: going over answers 429 on the POST
#: and nothing is spent.
RA_SLOTS = 3
#: SuperAlpha simulations one person may run at once.
SUPER_ALPHA_SLOTS = 3
#: The least ``selectionLimit`` BRAIN accepts.
SUPER_ALPHA_MIN_SELECTION = 10
#: BRAIN's ceiling on operators across every Alpha a SuperAlpha selects.
SUPER_ALPHA_OPERATOR_LIMIT = 8000

#: The Alpha list's largest page: a bigger ``limit`` is served as 100 anyway.
ALPHA_LIST_PAGE = 100
#: The Alpha list refuses offsets past this; beyond it the listing continues by
#: ``dateCreated`` instead.
ALPHA_LIST_MAX_OFFSET = 1_000
#: The data-field page size region ``ALL`` allows. Measured: 51 is refused outright.
ALL_FIELDS_PAGE = 50
#: Rows the flat field listing hands out for region ``ALL``: ``offset + limit`` past this is a
#: 400, which is why its paging goes dataset by dataset.
ALL_FIELDS_CEILING = 10_000

# --- accounting ------------------------------------------------------------------------

BOOK_SIZE = 20_000_000.0
#: BRAIN annualizes over 250 trading days, measured.
TRADING_DAYS = 250
#: Alphas submitted in a pyramid this quarter before it counts as formulated: "a minimum of
#: 3 Alphas in it", BRAIN Genius.
PYRAMID_MIN_ALPHAS = 3

# --- addresses -------------------------------------------------------------------------

#: Not a setting: the sign-in screen sends the password here, so no file may redirect it.
API_BASE = "https://api.worldquantbrain.com"
PLATFORM_URL = "https://platform.worldquantbrain.com"
#: Singular: ``/alphas/:id`` is the list and its ``:id`` a tab. BRAIN keeps the last 10,000
#: simulations, so an older Alpha's link reads "does not exist".
PLATFORM_ALPHA_URL = f"{PLATFORM_URL}/alpha/"
#: Fallback only, for a 401 that carried no inquiry location.
PLATFORM_SIGN_IN = f"{PLATFORM_URL}/sign-in"
PLATFORM_COMPETITIONS_URL = f"{PLATFORM_URL}/competitions/"


def _plain(value: Any) -> Any:
    """``value`` as JSON can hold it: sets sorted into lists, tuples as lists."""
    if isinstance(value, frozenset | set):
        return sorted(value)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
    if isinstance(value, tuple):
        return [_plain(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    return value


def typescript() -> str:
    """Every value above as an exported ``as const``, for the frontend."""
    lines = [
        "// Generated from backend/src/alpha_harness/brain/platform.py by `lefthook run format`.",
        "// Do not edit: change the Python and regenerate.",
        "",
    ]
    for name, value in vars(sys.modules[__name__]).items():
        if name.isupper():
            lines.append(f"export const {name} = {json.dumps(_plain(value))} as const")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.stdout.write(typescript())
