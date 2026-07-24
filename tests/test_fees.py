"""FeeTableProvider: config-table fallback vs Keepa-sourced fulfilment fee
and referral %. See pricing/fees.py's module docstring for why Keepa's
fbaFees/referralFeePercentage replace the SP-API getMyFeesEstimate plan
(cost/eligibility bar too high for this project) for both fee components.
`estimated` is only False when BOTH components are Keepa-sourced -- either
one still falling back to the config table means the result is a guess."""
from app import spapi_client
from app.pricing.fees import FeeTableProvider, SizeDims, SpApiFeeProvider, build_fee_provider


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
    # Still estimated overall -- no keepa_referral_pct was supplied, so that
    # component fell back to the config table.
    assert fees.estimated is True


def test_referral_fee_still_table_sourced_without_keepa_referral_pct():
    # No keepa_referral_pct supplied -> stays category-percentage-based,
    # regardless of whether the fulfilment fee came from Keepa or the table.
    fees = _provider().get_fees("Electronics", 2000, dims=None, keepa_fulfilment_fee_pence=280)
    assert fees.referral_fee_pence == round(2000 * 0.08)


def test_uses_keepa_referral_pct_when_provided():
    # 13.0 == 13% (percentage points, not a 0-1 fraction) -- overrides the
    # config table's 8% for Electronics.
    fees = _provider().get_fees("Electronics", 2000, dims=None, keepa_referral_pct=13.0)
    assert fees.referral_fee_pence == round(2000 * 0.13)


def test_estimated_false_only_when_both_fee_components_are_keepa_sourced():
    fees = _provider().get_fees(
        "Electronics", 2000, dims=None, keepa_fulfilment_fee_pence=280, keepa_referral_pct=13.0,
    )
    assert fees.estimated is False


def test_keepa_fee_does_not_override_size_tier_classification():
    # dims still drive the size tier (and storage fee/oversize classification)
    # even when the fulfilment fee itself comes from Keepa.
    dims = SizeDims(weight_kg=30.0, longest_cm=70.0, dims_sum_cm=250.0)   # oversize
    fees = _provider().get_fees("Electronics", 2000, dims=dims, keepa_fulfilment_fee_pence=1500)
    assert fees.fba_fulfilment_fee_pence == 1500
    assert fees.monthly_storage_fee_pence == 60   # oversize storage rate


class TestSpApiFeeProvider:
    """SpApiFeeProvider (Phase 2, dormant) -- layers SP-API on top of an
    unchanged FeeTableProvider fallback. See spapi_client.py's module
    docstring: dormant until real credentials exist, zero effect on today's
    behavior otherwise."""

    def test_no_asin_always_falls_through(self, db_session, monkeypatch):
        calls = []
        monkeypatch.setattr(spapi_client, "get_fees_estimate", lambda *a, **kw: calls.append(1) or None)
        provider = SpApiFeeProvider(db_session, _provider())

        fees = provider.get_fees("Electronics", 2000, dims=None, asin=None)

        assert calls == []
        assert fees.estimated is True   # fell through to the table-sourced fallback

    def test_falls_through_when_spapi_returns_none(self, db_session, monkeypatch):
        monkeypatch.setattr(spapi_client, "get_fees_estimate", lambda db, asin, price: None)
        provider = SpApiFeeProvider(db_session, _provider())

        fees = provider.get_fees("Electronics", 2000, dims=None, keepa_referral_pct=13.0, asin="B000TEST")

        assert fees.referral_fee_pence == round(2000 * 0.13)   # unchanged fallback behavior

    def test_uses_spapi_values_when_available(self, db_session, monkeypatch):
        monkeypatch.setattr(
            spapi_client, "get_fees_estimate",
            lambda db, asin, price: spapi_client.FeesEstimateResult(referral_fee_pence=150, fba_fulfilment_fee_pence=275),
        )
        provider = SpApiFeeProvider(db_session, _provider())

        fees = provider.get_fees("Electronics", 2000, dims=None, asin="B000TEST")

        assert fees.referral_fee_pence == 150
        assert fees.fba_fulfilment_fee_pence == 275
        assert fees.estimated is False
        # storage fee still comes from the fallback -- SP-API's fee estimate has no storage component
        assert fees.monthly_storage_fee_pence == 27

    def test_classify_size_tier_delegates_to_fallback(self, db_session):
        fallback = _provider()
        provider = SpApiFeeProvider(db_session, fallback)
        dims = SizeDims(weight_kg=30.0, longest_cm=70.0, dims_sum_cm=250.0)

        assert provider.classify_size_tier(dims) == fallback.classify_size_tier(dims) == "oversize"


class TestBuildFeeProvider:
    """The single place scheduler.py and /scan both get their fee provider
    from -- can never drift out of sync (see pricing/fees.py's docstring)."""

    def _app_cfg(self) -> dict:
        return {"fees": {
            "default_referral_pct": 0.15,
            "category_referral_pct": {"Electronics": 0.08},
            "fba_fee_by_size_tier_pence": {"small_standard": 230, "standard": 320, "large_standard": 450, "oversize": 900},
            "monthly_storage_fee_pence": {"standard": 27, "oversize": 60},
            "size_tier_thresholds_cm_kg": {
                "small_standard": {"max_weight_kg": 0.46, "max_longest_cm": 35, "max_dims_sum_cm": 60},
                "standard": {"max_weight_kg": 9.0, "max_longest_cm": 45, "max_dims_sum_cm": 90},
                "large_standard": {"max_weight_kg": 23.0, "max_longest_cm": 61, "max_dims_sum_cm": 210},
            },
        }}

    def test_returns_plain_table_provider_when_unconfigured(self, db_session, monkeypatch):
        monkeypatch.setattr(spapi_client, "is_configured", lambda: False)
        provider = build_fee_provider(db_session, self._app_cfg())
        assert isinstance(provider, FeeTableProvider)

    def test_returns_spapi_wrapped_provider_when_configured(self, db_session, monkeypatch):
        monkeypatch.setattr(spapi_client, "is_configured", lambda: True)
        provider = build_fee_provider(db_session, self._app_cfg())
        assert isinstance(provider, SpApiFeeProvider)
