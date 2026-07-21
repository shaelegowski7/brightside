"""Model-number regex extraction (spec priority #2) — deliberately
conservative: letter-prefixed alphanumeric codes only, so voltage/wattage/
size specs and prices in noisy HUKD titles don't get treated as model
numbers (see model_number.py's module docstring for why)."""
from app.matching.model_number import extract_model_number


def test_extracts_spec_example_code():
    assert extract_model_number("DEWALT 18V Drill AF300UK - Amazing Deal") == "AF300UK"


def test_extracts_hyphenated_code_from_real_title():
    # Real title observed live 2026-07-21.
    title = "108° - DEWALT 18V XR Brushless Oscillating Multi-Tool, Tool Only, DCS355N-XJ / Sold By GLOBALTECH 1998 FBA"
    assert extract_model_number(title) == "DCS355N-XJ"


def test_ignores_voltage_wattage_and_size_specs():
    title = "119° - Forge Steel Sale | E.g. 2 x 8m Tape Measure / Drive Socket 3/8\" Set 18 Pcs - £17.49 / Mixed Angle Screwdriver Set 112 Pcs -£19.99 - Free C&C"
    assert extract_model_number(title) is None


def test_ignores_pure_digits_and_prices():
    assert extract_model_number("Save £19.99 on this 4.7L Ninja Air Fryer, 50W, 112 Pcs") is None


def test_picks_longest_candidate_when_multiple():
    assert extract_model_number("Compatible with AB1234 and the newer XYZ98765 model") == "XYZ98765"


def test_returns_none_for_plain_title():
    assert extract_model_number("Nuii Cream & Anatolian Pistachio Ice Cream Sticks 3 x 90ml") is None
