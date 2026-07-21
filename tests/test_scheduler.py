"""poll_hukd_feeds: merchant_blocklist filtering. Real production data
(2026-07-21) showed HUKD's trending feed surfacing flights/hotels/digital
storefronts (Skyscanner, Ryanair, PlayStation Store, etc.) that can never
match a physical Amazon product — those get filtered before even reaching
process_deal, distinct from deals that reach it and fail to match."""
from app import pipeline, scheduler
from app.sources.base import RawDeal


def _raw(retailer: str, url: str) -> RawDeal:
    return RawDeal(source="hotukdeals", retailer=retailer, title="Some Deal", url=url, buy_price_pence=1000)


def _app_cfg(merchant_blocklist: list[str]) -> dict:
    return {
        "hukd": {
            "feeds": [{"name": "trending", "url": "https://example.com/rss"}],
            "merchant_blocklist": merchant_blocklist,
        },
        "thresholds": {
            "min_roi": 0.30, "min_net_profit_pence": 300, "max_fba_offers": 6,
            "rank_history_min_days": 90, "price_spike_pct": 0.20,
            "cooldown_hours": 24, "cooldown_price_improve_pct": 0.10,
        },
        "vat_registered": False,
        "reject_oversize": True,
        "category_rank_thresholds": {"default_rank_threshold": 150000},
        "category_blocklist": [],
        "decision": {"inbound_shipping_pence": 40},
        "fees": {
            "default_referral_pct": 0.15, "category_referral_pct": {},
            "fba_fee_by_size_tier_pence": {"small_standard": 230, "standard": 320, "large_standard": 450, "oversize": 900},
            "monthly_storage_fee_pence": {"standard": 27, "oversize": 60},
            "size_tier_thresholds_cm_kg": {
                "small_standard": {"max_weight_kg": 0.46, "max_longest_cm": 35, "max_dims_sum_cm": 60},
                "standard": {"max_weight_kg": 9.0, "max_longest_cm": 45, "max_dims_sum_cm": 90},
                "large_standard": {"max_weight_kg": 23.0, "max_longest_cm": 61, "max_dims_sum_cm": 210},
            },
        },
    }


def test_blocklisted_merchant_never_reaches_process_deal(monkeypatch):
    raw_deals = [
        _raw("Skyscanner", "https://www.hotukdeals.com/deals/flight-1"),
        _raw("Screwfix", "https://www.hotukdeals.com/deals/drill-2"),
    ]
    monkeypatch.setattr(scheduler, "get_config", lambda: _app_cfg(["Skyscanner", "Ryanair"]))
    monkeypatch.setattr(scheduler.HotUKDealsAdapter, "poll", lambda self: raw_deals)

    processed = []
    monkeypatch.setattr(pipeline, "process_deal", lambda db, raw, *a, **kw: processed.append(raw.retailer))

    scheduler.poll_hukd_feeds()

    assert processed == ["Screwfix"]


def test_blocklist_match_is_case_insensitive(monkeypatch):
    raw_deals = [_raw("SKYSCANNER", "https://www.hotukdeals.com/deals/flight-3")]
    monkeypatch.setattr(scheduler, "get_config", lambda: _app_cfg(["Skyscanner"]))
    monkeypatch.setattr(scheduler.HotUKDealsAdapter, "poll", lambda self: raw_deals)

    processed = []
    monkeypatch.setattr(pipeline, "process_deal", lambda db, raw, *a, **kw: processed.append(raw.retailer))

    scheduler.poll_hukd_feeds()

    assert processed == []
