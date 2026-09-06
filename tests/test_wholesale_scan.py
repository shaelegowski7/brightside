"""tools/wholesale_scan.py's pack-size handling.

Regression cover for the bug that put every Pharmazon candidate on the
report with a phantom 300-550% ROI (2026-09-06): the feed sells one unit,
the ASIN is a 5- or 6-pack, and one unit's cost was being credited against
the multipack's sale price. Titles below are the real ones, read off the
live listings.
"""
import pytest

from tools.wholesale_scan import _bundle_units, _pack_multiplier


# The five that reached the report unflagged, with their true pack sizes.
_REAL_MULTIPACK_TITLES = [
    ("Euthymol Original Toothpaste Pack of 5", 5),
    ("Aquafresh Big Teeth Toothpaste, for Kids Teeth, 6-8 Years, 50ml - Pack of 6", 6),
    ("6 x Deep Heat Heat Rub 100g", 6),
    ("Cymex Cream for Cold Sores x 6", 6),
    ("6 x Valupak Vit B Tablets", 6),
]


@pytest.mark.parametrize("title,expected", _REAL_MULTIPACK_TITLES)
def test_real_multipack_titles_are_read_correctly(title, expected):
    assert _pack_multiplier(title) == expected


def test_missing_title_is_unknown_not_one():
    """The actual defect: None compared equal to a feed's implied 1, so the
    check passed silently instead of admitting it had nothing to read."""
    assert _pack_multiplier(None) is None
    assert _pack_multiplier("") is None
    assert _pack_multiplier("Euthymol Original Toothpaste 75ml") == 1


def test_nested_pack_language_multiplies():
    assert _pack_multiplier("Case of 8 Packs of 12") == 96


@pytest.mark.parametrize(
    "text",
    [
        "Aquafresh Big Teeth Toothpaste, for Kids Teeth, 6-8 Years, 50ml",
        "Deep Heat Heat Rub 100g",
    ],
    ids=["age_range", "weight"],
)
def test_numbers_that_are_not_pack_sizes_are_ignored(text):
    """"6-8 Years" and "100g" must not be read as pack counts."""
    assert _pack_multiplier(text) == 1


@pytest.mark.parametrize(
    "feed,asin,expected",
    [
        (1, 6, 6),      # single feed unit -> 6-pack ASIN: buy six, bundle
        (1, 5, 5),
        (1, 1, 1),      # both singles, nothing to do
        (12, 12, 1),    # feed already sells the pack the ASIN lists
        (2, 6, 3),      # feed 2-pack -> ASIN 6-pack: three of them
        (2, 3, 1),      # not a whole multiple -- unassemblable, leave alone
        (6, 1, 1),      # feed pack bigger than ASIN: splitting, not bundling
        (None, 6, 1),   # unknown feed side
        (1, None, 1),   # unknown ASIN side -- the missing-title case
        (None, None, 1),
    ],
)
def test_bundle_units(feed, asin, expected):
    assert _bundle_units(feed, asin) == expected


def test_bundle_repricing_turns_a_phantom_win_into_a_loss():
    """Valupak Vit B as the scan reported it vs as it really is.

    Unit cost 112p against a 6-pack's 1194p sale price looked like 548% ROI.
    The same numbers at the real cost base clear nothing.
    """
    unit_cost, sell, fees = 112, 1194, 468
    units = _bundle_units(_pack_multiplier("Valupak Vitamin B Complex Oad Tablet"),
                          _pack_multiplier("6 x Valupak Vit B Tablets"))
    assert units == 6

    phantom_roi = (sell - fees - unit_cost) / unit_cost
    assert phantom_roi > 5.0                      # what the report showed

    bundle_cost = unit_cost * units
    real_roi = (sell - fees - bundle_cost) / bundle_cost
    assert real_roi < 0.30                        # under the min_roi floor
