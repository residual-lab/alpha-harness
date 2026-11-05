"""One dataset's fields split into a bucket per field an Alpha reads, for swapping them together.

An Alpha that adds two fields of one dataset works because each part carries its own
information, so a variant has to change both parts, each into something of its own kind. Every
field of the dataset goes to the seed field it is most like *in the way the seeds differ*:
with ``..._earnings_...`` and ``..._revenue_...`` as seeds, an EPS field goes with the first and
a sales field with the second, whatever else they share.

1. Each field is a TF-IDF vector of its id's words and, weighted lower, its description's.
   Ids are systematic within a dataset; descriptions are written in several styles, and a
   style is not a meaning.
2. The seeds' contrast is each seed's vector less their mean, words only: a variant number
   (``_3`` against ``_4``) tells nothing apart.
3. A field naming what tells the seeds apart goes with the seed it names. Every other field
   takes its bucket from its neighbours: the contrast spread over a nearest-neighbour graph
   (Zhou et al., "Learning with Local and Global Consistency", 2004), so a family of fields
   stays together.

Each bucket is then ordered for picking: greedily, the field that best represents what the
picks so far do not (facility location, weighted by coverage), so the top few are different
signals rather than one concept at five horizons. On the vault's two-field Alphas, no field
statistic predicted a pair's Sharpe (|rho| < 0.05 within a template), so spreading the picks is
what a short list can do.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Sequence

_TOKENS = re.compile(r"[a-z]+|\d+")
ID_WEIGHT, DESCRIPTION_WEIGHT = 0.8, 0.6
NEIGHBOURS = 15
#: How much of a field's bucket its neighbours decide, against its own words.
SPREAD = 0.8
#: A field whose own contrast is at least this share of the seeds' own is decided by it alone.
DECISIVE = 0.15
#: Most fields ordered as picks; the rest of a bucket follows by coverage.
MOST_PICKS = 50
#: Members a pick's reach is measured on: a fixed sample, so a bucket of thousands stays quick.
REACH_SAMPLE = 1000


@dataclass(frozen=True, slots=True)
class FieldText:
    field: str
    description: str
    coverage: float | None


def _tfidf(docs: list[list[str]]) -> tuple[np.ndarray, np.ndarray]:
    """Rows of unit TF-IDF over the words shared by some but not most fields, and which of
    those columns are words rather than numbers."""
    n = len(docs)
    df = Counter(w for doc in docs for w in set(doc))
    vocab = {w: i for i, w in enumerate(w for w, k in df.items() if 1 < k <= max(1, n // 2))}
    x = np.zeros((n, len(vocab)), np.float32)
    for row, doc in enumerate(docs):
        for w, k in Counter(doc).items():
            if (col := vocab.get(w)) is not None:
                x[row, col] = (1 + math.log(k)) * math.log(n / df[w])
    x /= np.maximum(np.linalg.norm(x, axis=1, keepdims=True), 1e-9)
    return x, np.array([not w.isdigit() for w in vocab], dtype=bool)


def _features(fields: Sequence[FieldText]) -> tuple[np.ndarray, np.ndarray]:
    ids, id_words = _tfidf([_TOKENS.findall(f.field.lower()) for f in fields])
    text, text_words = _tfidf([_TOKENS.findall(f.description.lower()) for f in fields])
    x = np.hstack([ID_WEIGHT * ids, DESCRIPTION_WEIGHT * text])
    return x, np.concatenate([id_words, text_words])


def _neighbours(x: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Edges of the symmetric k-nearest-neighbour graph: from, to, weight."""
    n = len(x)
    k = min(k, n - 1)
    if k < 1:
        return np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0, np.float32)
    to = np.empty((n, k), np.int64)
    weight = np.empty((n, k), np.float32)
    for start in range(0, n, 512):
        sim = x[start : start + 512] @ x.T
        sim[np.arange(len(sim)), np.arange(start, start + len(sim))] = -1
        near = np.argpartition(-sim, k - 1, axis=1)[:, :k]
        to[start : start + 512] = near
        weight[start : start + 512] = np.maximum(np.take_along_axis(sim, near, 1), 0)
    frm = np.repeat(np.arange(n), k)
    return (
        np.concatenate([frm, to.ravel()]),
        np.concatenate([to.ravel(), frm]),
        np.concatenate([weight.ravel(), weight.ravel()]),
    )


def _spread(edges: tuple[np.ndarray, np.ndarray, np.ndarray], own: np.ndarray) -> np.ndarray:
    frm, to, weight = edges
    n = len(own)
    degree = np.bincount(frm, weights=weight, minlength=n)
    degree[degree == 0] = 1
    norm = weight / np.sqrt(degree[frm] * degree[to])
    found = own.copy()
    kept = (1 - SPREAD) * own
    for _ in range(50):
        moved = np.stack(
            [np.bincount(to, found[frm, j] * norm, minlength=n) for j in range(own.shape[1])],
            axis=1,
        )
        found = SPREAD * moved + kept
    return found


def _picks(x: np.ndarray, members: list[int], coverage: np.ndarray) -> list[int]:
    """``members`` in pick order: the first ``MOST_PICKS`` by facility location, the rest by
    coverage."""
    if not members:
        return []
    # Evenly spaced rather than random, so the same Alpha always gets the same picks.
    reach = members[:: max(1, len(members) // REACH_SAMPLE)]
    sim = np.maximum(x[members] @ x[reach].T, 0)
    weight = coverage[members]
    best = np.zeros(len(reach), np.float32)
    order: list[int] = []
    taken = np.zeros(len(members), bool)
    for _ in range(min(MOST_PICKS, len(members))):
        gain = (np.maximum(sim, best[None, :]) - best[None, :]).sum(1) * weight
        gain[taken] = -1
        pick = int(gain.argmax())
        order.append(pick)
        taken[pick] = True
        best = np.maximum(best, sim[pick])
    rest = sorted(np.flatnonzero(~taken), key=lambda i: -weight[i])
    return [members[i] for i in [*order, *rest]]


def split(fields: Sequence[FieldText], seeds: Sequence[str]) -> list[list[str]]:
    """A bucket per seed, in ``seeds`` order, of every other field in pick order. ``fields`` are
    one dataset's, of one type, the seeds among them."""
    index = {f.field: i for i, f in enumerate(fields)}
    at = [index[s] for s in seeds]
    if len(seeds) == 1:
        members = [i for i in range(len(fields)) if i != at[0]]
        x, _ = _features(fields)
        coverage = np.array([f.coverage or 0.0 for f in fields], np.float32)
        return [[fields[i].field for i in _picks(x, members, coverage)]]

    x, words = _features(fields)
    seed_rows = x[at]
    contrast = (seed_rows - seed_rows.mean(0, keepdims=True)) * words
    own = x @ contrast.T
    found = _spread(_neighbours(x, NEIGHBOURS), own)
    ranked = np.sort(own, axis=1)
    margin = ranked[:, -1] - ranked[:, -2]
    seeds_margin = float(margin[at].min())
    decisive = margin > DECISIVE * seeds_margin if seeds_margin > 0 else np.zeros(len(x), bool)
    bucket = np.where(decisive[:, None], own, found).argmax(1)
    bucket[at] = np.arange(len(seeds))

    coverage = np.array([f.coverage or 0.0 for f in fields], np.float32)
    out: list[list[str]] = []
    for b in range(len(seeds)):
        members = [i for i in np.flatnonzero(bucket == b).tolist() if i not in at]
        out.append([fields[i].field for i in _picks(x, members, coverage)])
    return out


if __name__ == "__main__":
    import sys

    def make(name: str, about: str) -> FieldText:
        return FieldText(name, about, 0.9)

    toy = [
        make("est_earnings_short_2", "Estimated earnings, short horizon"),
        make("est_earnings_long_3", "Estimated earnings, long horizon"),
        make("eps_change_30d", "Change in earnings per share over 30 days"),
        make("est_revenue_short_2", "Estimated revenue, short horizon"),
        make("est_revenue_long_4", "Estimated revenue, long horizon"),
        make("sales_growth_long", "Growth in revenue and sales, long horizon"),
        make("est_earnings_medium_3", "Estimated earnings, medium horizon"),
        make("est_revenue_medium_4", "Estimated revenue, medium horizon"),
        make("price_change_30d", "Price change over 30 days"),
        make("price_change_60d", "Price change over 60 days"),
    ]
    earnings_seed, revenue_seed = ["est_earnings_medium_3"], ["est_revenue_medium_4"]
    earnings, revenue = split(toy, earnings_seed + revenue_seed)
    single = split(toy, ["est_earnings_medium_3"])[0]
    checks: dict[str, tuple[object, object]] = {
        "every field once": (
            sorted(earnings + revenue),
            sorted(f.field for f in toy if f.field not in {*earnings_seed, *revenue_seed}),
        ),
        "earnings named": (
            {"est_earnings_short_2", "est_earnings_long_3"} <= set(earnings),
            True,
        ),
        "revenue named": (
            {"est_revenue_short_2", "est_revenue_long_4"} <= set(revenue),
            True,
        ),
        "family together": (
            ("price_change_30d" in earnings) == ("price_change_60d" in earnings),
            True,
        ),
        "one seed takes all": (len(single), len(toy) - 1),
    }
    wrong = [f"{name}: {got!r}" for name, (got, want) in checks.items() if got != want]
    if wrong:
        raise SystemExit(f"field buckets self-check failed: {'; '.join(wrong)}")
    sys.stdout.write(f"{len(checks)} cases as expected\n")
