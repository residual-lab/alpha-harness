"""``vault.metrics.correlations`` against its promises, in float64 or float32 alike."""

import numpy as np
from hypothesis import given
from hypothesis import strategies as st

from alpha_harness.vault.metrics import MIN_OVERLAP, Bools, Floats, correlations


@st.composite
def ledgers(draw: st.DrawFn, min_alphas: int = 1) -> tuple[Floats, Bools]:
    """Daily PnL, days by Alphas and NaN where an Alpha did not trade, and which Alphas move.

    Days straddle the overlap a correlation needs. A moving Alpha is noise around a level
    within one spread of zero, as real PnL is; far beyond that, one-pass moments in float32
    lose the spread to cancellation.
    """
    days = draw(st.integers(MIN_OVERLAP - 5, MIN_OVERLAP + 60))
    alphas = draw(st.integers(min_alphas, 6))
    rng = np.random.default_rng(draw(st.integers(0, 2**32 - 1)))
    pnl = np.full((days, alphas), np.nan)
    moves = np.zeros(alphas, dtype=np.bool_)
    for alpha in range(alphas):
        first, last = draw(st.integers(0, 40)), days - draw(st.integers(0, 40))
        kind = draw(st.sampled_from(["moves", "flat", "never traded"]))
        if kind == "moves":
            spread = draw(st.floats(1e-2, 1e5))
            level = draw(st.floats(-1, 1)) * spread
            pnl[first:last, alpha] = level + spread * rng.standard_normal(last - first)
            moves[alpha] = True
        elif kind == "flat":
            # Any constant: one such as 0.1 leaves a spread of rounding noise behind, which
            # must read as no spread at all.
            pnl[first:last, alpha] = draw(
                st.one_of(st.sampled_from([12.34, 1.1, 0.1, 0.0]), st.floats(-1e6, 1e6))
            )
        if draw(st.booleans()):
            pnl[rng.random(days) < 0.02, alpha] = np.nan
    return pnl, moves


def tolerance(out: Floats) -> float:
    """The worst measured is about ten ulps; this allows four thousand, in either precision."""
    return 4096 * float(np.finfo(out.dtype).eps)


@given(ledgers())
def test_correlations_keep_their_promises(ledger: tuple[Floats, Bools]) -> None:
    pnl, moves = ledger
    before = pnl.copy()
    out = correlations(pnl)
    np.testing.assert_array_equal(pnl, before, "correlations changed its input")

    alphas = pnl.shape[1]
    assert out.shape == (alphas, alphas)
    np.testing.assert_allclose(out, out.T, rtol=0, atol=tolerance(out), equal_nan=True)
    assert np.all(np.abs(out[~np.isnan(out)]) <= 1.0)
    assert np.all(np.diag(out) == 1.0)

    traded = ~np.isnan(pnl)
    overlap = traded.T.astype(int) @ traded.astype(int)
    measured = (overlap >= MIN_OVERLAP) & moves[:, None] & moves[None, :]
    for i in range(alphas):
        for j in range(alphas):
            if i == j:
                continue
            assert np.isnan(out[i, j]) == (not measured[i, j]), (
                f"pair {i},{j}: {overlap[i, j]} days, moves {moves[i]} {moves[j]}, got {out[i, j]}"
            )
            if measured[i, j]:
                both = traded[:, i] & traded[:, j]
                expected = np.corrcoef(pnl[both, i], pnl[both, j])[0, 1]
                assert abs(out[i, j] - expected) <= tolerance(out), (i, j, out[i, j], expected)


@given(ledgers(min_alphas=2), st.data())
def test_against_a_pool_is_that_block_of_the_whole_matrix(
    ledger: tuple[Floats, Bools], data: st.DataObject
) -> None:
    whole, _ = ledger
    split = data.draw(st.integers(1, whole.shape[1] - 1), label="columns judged")
    before = whole.copy()
    # Column slices, not copies: callers pass views like these.
    block = correlations(whole[:, :split], whole[:, split:])
    np.testing.assert_array_equal(whole, before, "correlations changed its input")
    expected = correlations(whole)[:split, split:]
    assert block.shape == expected.shape
    np.testing.assert_allclose(block, expected, rtol=0, atol=tolerance(block), equal_nan=True)
