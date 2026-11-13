from alpha_harness.tools.correlation_breaker import custom_recipe

PASTED = (
    "My Re-shape 1: Rolling Gaussian Quantile Transformation Maps rolling 20-day historical "
    "percentile ranks into a standard Gaussian normal distribution."
)


def test_a_bad_name_or_code_blocks_only_its_own_card() -> None:
    assert "keep it to 120" in custom_recipe("a", PASTED, "rank(alpha)").missing
    assert "no name" in custom_recipe("a", "", "rank(alpha)").missing
    assert "keep it to 4000" in custom_recipe("a", "Mine", "rank(alpha) " * 400).missing
    assert not custom_recipe("a", PASTED[:55], "rank(alpha)").missing
