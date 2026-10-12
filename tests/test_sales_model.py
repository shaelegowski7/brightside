import math

from app.sales_model import estimate_with

MODEL = {"all": {"a": 5.0, "b": -1.0}, "categories": {"Toys & Games": {"a": 6.0, "b": -1.0}}}


def test_power_law_per_category_with_fallback():
    assert estimate_with(MODEL, "Toys & Games", 10_000) == 100.0     # 10^6 / 10^4
    assert estimate_with(MODEL, "Garden", 10_000) == 10.0            # all-category fit
    assert math.isclose(estimate_with(MODEL, "Toys & Games", 2_000), 500.0)


def test_no_estimate_without_rank_or_model():
    assert estimate_with(MODEL, "Toys & Games", None) is None
    assert estimate_with(MODEL, "Toys & Games", 0) is None
    assert estimate_with(None, "Toys & Games", 1000) is None
