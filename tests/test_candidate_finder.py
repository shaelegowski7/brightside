"""Reverse candidate search: the target-buy-price inversion (pure maths,
checked against decision/engine.py's own forward formula), the Keepa
finder param mapping, and the dedupe that stops a daily run re-posting
the same shortlist."""
from app import candidate_finder, keepa_client, models
from app.candidate_finder import Candidate, target_buy_price_pence
from app.decision.engine import DecisionConfig, ScoreInput, score_deal
from app.pricing.fees import FeeInput


def _cfg(**overrides) -> DecisionConfig:
    base = dict(
        min_roi=0.30,
        min_net_profit_pence=300,
        max_fba_offers=6,
        rank_history_min_days=90,
        price_spike_pct=0.20,
        vat_registered=False,
        reject_oversize=True,
        category_rank_thresholds={"Toys & Games": 80000},
        default_rank_threshold=150000,
        category_blocklist=set(),
        inbound_shipping_pence=40,
    )
    base.update(overrides)
    return DecisionConfig(**base)


# --- target_buy_price_pence: pure, no Keepa/DB ---


def test_target_price_is_the_tighter_of_roi_and_profit_constraints():
    cfg = _cfg()
    # headroom = 2000 - 500 - 0 - 40 = 1460
    #   roi leg:    1460 / 1.30 = 1123
    #   profit leg: 1460 - 300  = 1160
    # -> roi is tighter here
    assert target_buy_price_pence(2000, 500, 0, cfg) == 1123


def test_target_price_profit_constraint_binds_on_cheap_items():
    cfg = _cfg()
    # headroom = 900 - 400 - 0 - 40 = 460
    #   roi leg:    460 / 1.30 = 353
    #   profit leg: 460 - 300  = 160
    # -> the flat £3 minimum bites much harder at low prices
    assert target_buy_price_pence(900, 400, 0, cfg) == 160


def test_target_price_floors_at_zero_when_unachievable():
    cfg = _cfg()
    # Fees alone exceed the sell price -- no purchase price clears this.
    assert target_buy_price_pence(500, 600, 0, cfg) == 0


def test_target_price_accounts_for_storage():
    cfg = _cfg()
    without = target_buy_price_pence(2000, 500, 0, cfg)
    with_storage = target_buy_price_pence(2000, 500, 200, cfg)
    assert with_storage < without


def test_target_price_result_actually_passes_the_real_engine():
    """The whole point of the inversion: buying at exactly the target must
    satisfy score_deal's own forward maths. Guards against the two
    formulas drifting apart."""
    cfg = _cfg()
    sell, fees_pence = 2500, 600
    target = target_buy_price_pence(sell, fees_pence, 0, cfg)

    result = score_deal(
        ScoreInput(
            buy_price_pence=target,
            match_confidence="high",
            category="Toys & Games",
            fba_offer_count=1,
            amazon_on_listing=False,
            fees=FeeInput(
                referral_fee_pence=int(fees_pence / 1.20 / 2),
                fba_fulfilment_fee_pence=int(fees_pence / 1.20 / 2),
                monthly_storage_fee_pence=0,
                estimated=False,
            ),
            sales_rank=1000,
            est_monthly_sales=50.0,
            buybox_price_pence=sell,
        ),
        cfg,
    )
    assert result.verdict.value in ("PASS", "PASS_WITH_FLAGS")
    assert result.roi >= cfg.min_roi
    assert result.net_profit_pence >= cfg.min_net_profit_pence


# --- finder params ---


def test_finder_params_mirror_the_engine_gates():
    cfg = _cfg()
    params = candidate_finder._build_finder_params(
        {"max_sales_rank": 120000, "min_buybox_pence": 2000}, cfg
    )
    assert params["buyBoxIsAmazon"] is False
    assert params["buyBoxEligibleOfferCountsNewFBA_lte"] == cfg.max_fba_offers
    assert params["monthlySold_gte"] == int(cfg.velocity_min_monthly_sales)
    assert params["current_SALES_lte"] == 120000
    assert params["current_BUY_BOX_SHIPPING_gte"] == 2000
    # Must require a rank at all -- rank=None products are listed-but-dormant
    # and die on the engine's velocity floor.
    assert params["current_SALES_gte"] == 1


def test_finder_params_set_perpage_to_max_results():
    """n_products alone doesn't lift Keepa's 50-per-page default -- without
    perPage a request for 150 silently returns 50."""
    params = candidate_finder._build_finder_params(
        {"max_sales_rank": 1000, "min_buybox_pence": 100, "max_results": 150}, _cfg()
    )
    assert params["perPage"] == 150


def test_finder_params_include_max_buybox_when_set():
    cfg = _cfg()
    capped = candidate_finder._build_finder_params(
        {"max_sales_rank": 1000, "min_buybox_pence": 2000, "max_buybox_pence": 12000}, cfg
    )
    assert capped["current_BUY_BOX_SHIPPING_lte"] == 12000

    uncapped = candidate_finder._build_finder_params(
        {"max_sales_rank": 1000, "min_buybox_pence": 2000}, cfg
    )
    assert "current_BUY_BOX_SHIPPING_lte" not in uncapped


def test_excluded_title_pattern_drops_renewed_and_refurbished():
    for bad in [
        "Apple iPhone 14, 128GB, Purple - (Renewed)",
        "Dell Laptop (Refurbished)",
        "Sony Camera - Pre-Owned",
        "Nintendo Switch preowned bundle",
    ]:
        assert candidate_finder._EXCLUDED_TITLE_RE.search(bad), bad

    for good in [
        "Bullyland Rocket Marvel Collectible Figure",
        "Lepro Bayonet Light Bulb, Warm White 2700K",
        "Renewable Energy Science Kit for Kids",   # 'renewable' must not match
    ]:
        assert candidate_finder._EXCLUDED_TITLE_RE.search(good) is None, good


def test_finder_params_omit_root_category_when_unset():
    cfg = _cfg()
    params = candidate_finder._build_finder_params(
        {"max_sales_rank": 1000, "min_buybox_pence": 100, "root_categories": []}, cfg
    )
    assert "rootCategory" not in params

    scoped = candidate_finder._build_finder_params(
        {"max_sales_rank": 1000, "min_buybox_pence": 100, "root_categories": [12345]}, cfg
    )
    assert scoped["rootCategory"] == [12345]


# --- dedupe ---


def _candidate(asin: str, target: int = 500) -> Candidate:
    return Candidate(
        asin=asin, title=f"Widget {asin}", buybox_price_pence=2000,
        target_buy_price_pence=target, sales_rank=1000, fba_offer_count=2,
        est_monthly_sales=25.0,
    )


def test_filter_unseen_returns_only_new_candidates(db_session):
    first = candidate_finder.filter_unseen(db_session, [_candidate("B001"), _candidate("B002")])
    assert {c.asin for c in first} == {"B001", "B002"}

    # Same two again plus one new -- only the new one should come back.
    second = candidate_finder.filter_unseen(
        db_session, [_candidate("B001"), _candidate("B002"), _candidate("B003")]
    )
    assert [c.asin for c in second] == ["B003"]


def test_filter_unseen_refreshes_pricing_on_known_candidates(db_session):
    candidate_finder.filter_unseen(db_session, [_candidate("B001", target=500)])
    candidate_finder.filter_unseen(db_session, [_candidate("B001", target=750)])

    row = db_session.get(models.CandidateAsin, "B001")
    assert row.target_buy_price == 750   # target moves with Amazon's buybox


def test_discount_required_pct():
    c = Candidate(
        asin="B001", title="x", buybox_price_pence=2000, target_buy_price_pence=500,
        sales_rank=1, fba_offer_count=1, est_monthly_sales=10.0,
    )
    assert c.discount_required_pct == 0.75


# --- end-to-end with Keepa stubbed ---


def test_find_candidates_skips_products_that_cannot_clear_thresholds(db_session, monkeypatch):
    """A product whose fees leave no headroom must not be reported at all,
    rather than reported with a nonsense £0 target."""
    monkeypatch.setattr(keepa_client, "find_asins", lambda db, params, n: ["B0GOOD", "B0BAD"])

    def fake_stage2(db, asins):
        return {
            "B0GOOD": keepa_client.Stage2Result(
                asin="B0GOOD", title="Good", category="Toys & Games", sales_rank=5000,
                buybox_price_pence=3000, amazon_on_listing=False, fba_offer_count=2,
                lowest_fba_offer_pence=None, est_monthly_sales=40.0, buybox_avg_90d_pence=3000,
                rank_history_days=400, hazmat=False, package_weight_kg=0.2,
                package_longest_cm=10.0, package_dims_sum_cm=20.0,
                fba_fulfilment_fee_pence=200, referral_fee_percentage=15.0,
                leaf_category_id=None, leaf_category_rank=None,
            ),
            "B0BAD": keepa_client.Stage2Result(
                asin="B0BAD", title="Bad", category="Toys & Games", sales_rank=5000,
                buybox_price_pence=400, amazon_on_listing=False, fba_offer_count=2,
                lowest_fba_offer_pence=None, est_monthly_sales=40.0, buybox_avg_90d_pence=400,
                rank_history_days=400, hazmat=False, package_weight_kg=0.2,
                package_longest_cm=10.0, package_dims_sum_cm=20.0,
                fba_fulfilment_fee_pence=500, referral_fee_percentage=15.0,
                leaf_category_id=None, leaf_category_rank=None,
            ),
        }

    monkeypatch.setattr(keepa_client, "stage2_full", fake_stage2)

    # Real config, not a hand-written stand-in -- the fees block has a
    # fiddly shape (size tiers, per-category referral %) that's easy to get
    # subtly wrong, and a drifting copy here would test nothing useful.
    from app.config import get_config
    from app.pricing import fees as fees_module

    app_cfg = dict(get_config())
    app_cfg["candidate_finder"] = {"max_sales_rank": 120000, "min_buybox_pence": 2000, "max_results": 50}
    fee_provider = fees_module.build_fee_provider(db_session, app_cfg)

    found = candidate_finder.find_candidates(db_session, app_cfg, _cfg(), fee_provider)
    assert [c.asin for c in found] == ["B0GOOD"]
    assert found[0].target_buy_price_pence > 0
