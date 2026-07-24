"""app/scan.py: the /scan backend (Phase 2) -- full flow with Keepa/Discord
mocked, same style as test_pipeline.py. Covers PASS, REJECT-before-scoring
(no EAN match), and the cooldown/posted_to_discord interaction for a repeat
scan (see scan.py's module docstring)."""
from app import keepa_client, models, scan
from app.decision.engine import DecisionConfig
from app.pricing.fees import FeeTableProvider


def _decision_cfg(**overrides) -> DecisionConfig:
    base = dict(
        min_roi=0.30, min_net_profit_pence=300, max_fba_offers=6,
        rank_history_min_days=90, price_spike_pct=0.20, vat_registered=False,
        reject_oversize=True,
        category_rank_thresholds={"Toys & Games": 80000}, default_rank_threshold=150000,
        category_blocklist=set(), inbound_shipping_pence=40,
    )
    base.update(overrides)
    return DecisionConfig(**base)


def _fee_provider() -> FeeTableProvider:
    return FeeTableProvider({
        "default_referral_pct": 0.15,
        "category_referral_pct": {"Toys & Games": 0.15},
        "fba_fee_by_size_tier_pence": {"small_standard": 230, "standard": 320, "large_standard": 450, "oversize": 900},
        "monthly_storage_fee_pence": {"standard": 27, "oversize": 60},
        "size_tier_thresholds_cm_kg": {
            "small_standard": {"max_weight_kg": 0.46, "max_longest_cm": 35, "max_dims_sum_cm": 60},
            "standard": {"max_weight_kg": 9.0, "max_longest_cm": 45, "max_dims_sum_cm": 90},
            "large_standard": {"max_weight_kg": 23.0, "max_longest_cm": 61, "max_dims_sum_cm": 210},
        },
    })


_APP_CFG = {
    "thresholds": {"cooldown_hours": 24, "cooldown_price_improve_pct": 0.10},
    "price_sanity": {"min_page_price_ratio": 0.05},
}


def _stage2(**overrides) -> "keepa_client.Stage2Result":
    base = dict(
        asin="B000SCAN", title="Widget", category="Toys & Games",
        sales_rank=20000, buybox_price_pence=2500, amazon_on_listing=False,
        fba_offer_count=2, lowest_fba_offer_pence=None, est_monthly_sales=60,
        buybox_avg_90d_pence=2400, rank_history_days=200, hazmat=False,
        package_weight_kg=None, package_longest_cm=None, package_dims_sum_cm=None,
        fba_fulfilment_fee_pence=None, referral_fee_percentage=None,
        leaf_category_id=None, leaf_category_rank=None,
    )
    base.update(overrides)
    return keepa_client.Stage2Result(**base)


def _mock_pass(monkeypatch, asin="B000SCAN"):
    monkeypatch.setattr(keepa_client, "stage1_screen", lambda db, codes, is_ean: {
        codes[0]: keepa_client.Stage1Result(
            asin=asin, title="Widget", category="Toys & Games",
            sales_rank=20000, est_sell_price_pence=2400, rank_history_days=200,
        )
    })
    monkeypatch.setattr(keepa_client, "stage2_full", lambda db, asins: {asin: _stage2(asin=asin)})
    import app.discord_notifier as dn
    sent = []
    monkeypatch.setattr(dn, "send_ping", lambda webhook_url, embed: sent.append(embed) or True)
    return sent


def test_run_scan_pass_returns_full_verdict(db_session, monkeypatch):
    sent = _mock_pass(monkeypatch)

    result = scan.run_scan(db_session, "5901234123457", 1000, _decision_cfg(), _fee_provider(), _APP_CFG)

    assert result["verdict"] == "PASS_WITH_FLAGS"
    assert result["asin"] == "B000SCAN"
    assert result["net_profit_pence"] == 599
    assert result["roi"] == 0.599
    assert result["posted_to_discord"] is True
    assert result["keepa_url"] == "https://keepa.com/#!product/2-B000SCAN"
    assert len(sent) == 1


def test_run_scan_no_ean_match_rejects_before_scoring(db_session, monkeypatch):
    monkeypatch.setattr(keepa_client, "stage1_screen", lambda db, codes, is_ean: {})   # Keepa has no match for this EAN

    result = scan.run_scan(db_session, "0000000000000", 1000, _decision_cfg(), _fee_provider(), _APP_CFG)

    assert result["verdict"] == "REJECT"
    assert result["reasons"] == ["Could not match this barcode to an Amazon product"]
    assert result["asin"] is None
    assert result["posted_to_discord"] is False


def test_run_scan_second_scan_within_cooldown_not_posted_to_discord(db_session, monkeypatch):
    sent = _mock_pass(monkeypatch)

    first = scan.run_scan(db_session, "5901234123457", 1000, _decision_cfg(), _fee_provider(), _APP_CFG)
    second = scan.run_scan(db_session, "5901234123457", 1000, _decision_cfg(), _fee_provider(), _APP_CFG)

    assert first["posted_to_discord"] is True
    # still a full verdict on the second scan -- just not re-posted
    assert second["verdict"] == "PASS_WITH_FLAGS"
    assert second["net_profit_pence"] == 599
    assert second["posted_to_discord"] is False
    assert len(sent) == 1   # only the first scan actually posted


def test_run_scan_creates_a_fresh_deal_row_per_scan(db_session, monkeypatch):
    """Each scan gets a unique synthetic URL -- _upsert_deal's same-price/
    terminal-status dedup logic must never suppress a fresh scan."""
    _mock_pass(monkeypatch)

    scan.run_scan(db_session, "5901234123457", 1000, _decision_cfg(), _fee_provider(), _APP_CFG)
    scan.run_scan(db_session, "5901234123457", 1000, _decision_cfg(), _fee_provider(), _APP_CFG)

    deals = db_session.query(models.Deal).filter(models.Deal.source == "scan").all()
    assert len(deals) == 2
    assert deals[0].url != deals[1].url
