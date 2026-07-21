"""FeeTableProvider: config-table fallback vs Keepa-sourced fulfilment fee.
See pricing/fees.py's module docstring for why Keepa's fbaFees replaces the
SP-API getMyFeesEstimate plan (cost/eligibility bar too high for this
project) for the fulfilment-fee component specifically."""
from app.pricing.fees import FeeTableProvider, SizeDims


def _provider() -> FeeTableProvider:
    return FeeTableProvider({
        "default_referral_pct": 0.15,
        "category_referral_pct": {"Electronics": 0.08},
        "fba_fee_by_size_tier_pence": {"small_standard": 230, "standard": 320, "large_standard": 450, "oversize": 900},
        "monthly_storage_fee_pence": {"standard": 27, "oversize": 60},
        "size_tier_thresholds_cm_kg": {
            "small_standard": {"max_weight_kg": 0.46, "max_longest_cm": 35, "max_dims_sum_cm": 60},
            "standard": {"max_weight_kg": 9.0, "max_longest_cm": 45, "max_dims_sum_cm": 90},
            "large_standard": {"max_weight_kg": 23.0, "max_longest_cm": 61, "max_dims_sum_cm": 210},
        },
    })


def test_falls_back_to_size_tier_table_when_no_keepa_fee():
    fees = _provider().get_fees("Electronics", 2000, dims=None)
    assert fees.fba_fulfilment_fee_pence == 320   # "standard" tier, dims=None default
    assert fees.estimated is True


def test_uses_keepa_fulfilment_fee_when_provided():
    fees = _provider().get_fees("Electronics", 2000, dims=None, keepa_fulfilment_fee_pence=280)
    assert fees.fba_fulfilment_fee_pence == 280
    assert fees.estimated is False


def test_referral_fee_still_table_sourced_even_with_keepa_fulfilment_fee():
    # Keepa doesn't supply a referral fee — that stays category-percentage-based
    # regardless of whether the fulfilment fee came from Keepa or the table.
    fees = _provider().get_fees("Electronics", 2000, dims=None, keepa_fulfilment_fee_pence=280)
    assert fees.referral_fee_pence == round(2000 * 0.08)


def test_keepa_fee_does_not_override_size_tier_classification():
    # dims still drive the size tier (and storage fee/oversize classification)
    # even when the fulfilment fee itself comes from Keepa.
    dims = SizeDims(weight_kg=30.0, longest_cm=70.0, dims_sum_cm=250.0)   # oversize
    fees = _provider().get_fees("Electronics", 2000, dims=dims, keepa_fulfilment_fee_pence=1500)
    assert fees.fba_fulfilment_fee_pence == 1500
    assert fees.monthly_storage_fee_pence == 60   # oversize storage rate
