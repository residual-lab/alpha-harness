"""Truncation Agent: a truncation for each market, from fixed rules on its region, delay and
universe.

BRAIN recommends 0.05 to 0.1 and fails an Alpha whose largest weight in one stock reaches 10%,
or 8% in USA for consultants, so nothing here goes above 0.08. Within that, a broad universe
gets room for conviction, while a narrow one, a multi-country region or Delay 0 is held
tighter so a few names cannot carry the book.
"""

import re

from ..brain.platform import MAX_STOCK_WEIGHT_USA, MULTI_COUNTRY_REGIONS, REGION_AGNOSTIC_REGION

CEILING = MAX_STOCK_WEIGHT_USA
BROAD, MID, NARROW = 0.08, 0.06, 0.05
#: All Regions spans every country at once.
MULTI_COUNTRY = MULTI_COUNTRY_REGIONS | {REGION_AGNOSTIC_REGION}
TIGHT = 0.05


def breadth(universe: str) -> int | None:
    """How many names a ``TOP…`` universe holds, e.g. 500 for TOPSP500; ``None`` otherwise."""
    found = re.fullmatch(r"TOP[A-Z]*(\d+)[A-Z]*", universe.upper())
    return int(found.group(1)) if found else None


def truncation_for(region: str, delay: int, universe: str) -> float:
    """The truncation the agent sets for one market."""
    names = breadth(universe)
    # Liquidity-filtered universes (MINVOL1M) list no size; they are thousands of names.
    if names is None or names >= 2000:
        value = BROAD
    elif names >= 1000:
        value = MID
    else:
        value = NARROW
    if region.upper() in MULTI_COUNTRY or delay == 0:
        value = min(value, TIGHT)
    return min(value, CEILING)


if __name__ == "__main__":
    import sys

    cases = {
        ("USA", 1, "TOP3000"): 0.08,
        ("USA", 1, "TOP1000"): 0.06,
        ("USA", 1, "TOP200"): 0.05,
        ("USA", 1, "TOPSP500"): 0.05,
        ("USA", 0, "TOP3000"): 0.05,
        ("CHN", 1, "TOP2000U"): 0.08,
        ("JPN", 1, "TOP1600"): 0.06,
        ("EUR", 1, "TOPCS1600"): 0.05,
        ("GLB", 1, "MINVOL1M"): 0.05,
        ("GBR", 1, "TOP700"): 0.05,
        ("ALL", 1, "LARGE"): 0.05,
    }
    wrong = [
        f"{region} D{delay} {universe}: {truncation_for(region, delay, universe)}, not {want}"
        for (region, delay, universe), want in cases.items()
        if truncation_for(region, delay, universe) != want
    ]
    if wrong:
        raise SystemExit(f"truncation self-check failed: {'; '.join(wrong)}")
    sys.stdout.write(f"{len(cases)} markets as expected\n")
