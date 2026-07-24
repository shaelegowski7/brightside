"""End-to-end pipeline wiring, with resolver/Keepa/Discord calls mocked out
(no network) but everything else — dedupe, matching, decision engine, DB
writes, cooldown — running for real against the sqlite test DB."""
import pytest

from app import keepa_client, models, pipeline, resolver
from app.decision.engine import DecisionConfig
from app.pricing.fees import FeeTableProvider
from app.sources.base import RawDeal


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
        asin="B000TEST", title="Widget", category="Toys & Games",
        sales_rank=20000, buybox_price_pence=2500, amazon_on_listing=False,
        fba_offer_count=2, lowest_fba_offer_pence=None, est_monthly_sales=60,
        buybox_avg_90d_pence=2400, rank_history_days=200, hazmat=False,
        package_weight_kg=None, package_longest_cm=None, package_dims_sum_cm=None,
        fba_fulfilment_fee_pence=None, referral_fee_percentage=None,
        leaf_category_id=None, leaf_category_rank=None,
    )
    base.update(overrides)
    return keepa_client.Stage2Result(**base)


def test_amazon_url_match_full_pass_pings_and_records(db_session, monkeypatch):
    raw = RawDeal(
        source="hotukdeals", retailer="Amazon", title="Widget Deal",
        url="https://www.hotukdeals.com/deals/widget-deal-4938754",
        buy_price_pence=1000, image_url=None,
    )
    monkeypatch.setattr(resolver, "resolve", lambda url, key="": resolver.ResolvedDeal(
        final_url="https://www.amazon.co.uk/dp/B000WIDGT1?tag=x", html="<html></html>",
        status_code=200, blocked=False,
    ))
    monkeypatch.setattr(keepa_client, "stage1_screen", lambda db, codes, is_ean: {
        "B000WIDGT1": keepa_client.Stage1Result(
            asin="B000WIDGT1", title="Widget", category="Toys & Games",
            sales_rank=20000, est_sell_price_pence=2400, rank_history_days=200,
        )
    })
    monkeypatch.setattr(keepa_client, "stage2_full", lambda db, asins: {
        "B000WIDGT1": _stage2(asin="B000WIDGT1"),
    })

    sent_embeds = []
    import app.discord_notifier as dn
    monkeypatch.setattr(dn, "send_ping", lambda webhook_url, embed: sent_embeds.append(embed) or True)

    pipeline.process_deal(db_session, raw, _decision_cfg(), _fee_provider(), _APP_CFG)

    deal = db_session.query(models.Deal).filter(models.Deal.url == raw.url).first()
    assert deal.status == "pinged"
    assert deal.product_id is not None

    product = db_session.get(models.Product, deal.product_id)
    assert product.asin == "B000WIDGT1"
    assert product.matched_via == "amazon_url"

    score = db_session.query(models.Score).filter(models.Score.deal_id == deal.id).first()
    # PASS_WITH_FLAGS not PASS: FeeTableProvider always returns estimated=True
    # (Phase 1 has no SP-API), which is itself a soft flag — realistic Phase 1
    # behaviour never yields a pure PASS.
    assert score.verdict == "PASS_WITH_FLAGS"
    assert score.flags_json == ["estimated_fees"]
    assert score.net_profit == 599   # matches the decision-engine "clear pass" fixture exactly
    assert score.roi == 0.599

    ping = db_session.query(models.Ping).filter(models.Ping.asin == "B000WIDGT1").first()
    assert ping is not None
    assert ping.deal_id == deal.id
    assert len(sent_embeds) == 1

    # SP-API unconfigured (Phase 2, dormant, no real creds in this env) ->
    # gated must stay None, same as before that wiring existed.
    assert score.gated is None


def test_gated_true_rejects_end_to_end(db_session, monkeypatch):
    """Phase 2: when spapi_client is configured and reports gated=True, the
    deal must REJECT via decision/engine.py's existing hard filter (`if
    inp.gated is True: return _reject("gated", ...)`) -- proves the pipeline
    wiring reaches all the way through, not just that a field gets set."""
    raw = RawDeal(
        source="hotukdeals", retailer="Amazon", title="Widget Deal",
        url="https://www.hotukdeals.com/deals/widget-deal-gated",
        buy_price_pence=1000, image_url=None,
    )
    monkeypatch.setattr(resolver, "resolve", lambda url, key="": resolver.ResolvedDeal(
        final_url="https://www.amazon.co.uk/dp/B000GATED1?tag=x", html="<html></html>",
        status_code=200, blocked=False,
    ))
    monkeypatch.setattr(keepa_client, "stage1_screen", lambda db, codes, is_ean: {
        "B000GATED1": keepa_client.Stage1Result(
            asin="B000GATED1", title="Widget", category="Toys & Games",
            sales_rank=20000, est_sell_price_pence=2400, rank_history_days=200,
        )
    })
    monkeypatch.setattr(keepa_client, "stage2_full", lambda db, asins: {
        "B000GATED1": _stage2(asin="B000GATED1"),
    })
    import app.spapi_client as spapi_client
    monkeypatch.setattr(spapi_client, "is_configured", lambda: True)
    monkeypatch.setattr(spapi_client, "check_gating", lambda db, asin: True)

    sent_embeds = []
    import app.discord_notifier as dn
    monkeypatch.setattr(dn, "send_ping", lambda webhook_url, embed: sent_embeds.append(embed) or True)

    pipeline.process_deal(db_session, raw, _decision_cfg(), _fee_provider(), _APP_CFG)

    deal = db_session.query(models.Deal).filter(models.Deal.url == raw.url).first()
    assert deal.status == "stage2_scored"   # reached scoring; the score itself is a REJECT
    score = db_session.query(models.Score).filter(models.Score.deal_id == deal.id).first()
    assert score.verdict == "REJECT"
    assert score.verdict_reason == "gated"
    assert score.gated is True
    assert len(sent_embeds) == 0


def test_scan_source_skips_title_validation(db_session, monkeypatch):
    """A scan's deal.title is just an EAN placeholder (see app/scan.py) --
    it must never be compared against the real Keepa title, unlike an
    ordinary HUKD/Argos scrape (see test_title_mismatch_rejects_wrong_
    product just above, which still enforces the check)."""
    ean = "5901234123457"
    raw = RawDeal(
        source="scan", retailer=None, title=f"Scanned item (EAN {ean})",
        url=f"scan:{ean}:abc123", buy_price_pence=1000, image_url=None,
        html=f'<script type="application/ld+json">{{"@type":"Product","gtin13":"{ean}"}}</script>',
    )
    monkeypatch.setattr(keepa_client, "stage1_screen", lambda db, codes, is_ean: {
        codes[0]: keepa_client.Stage1Result(
            asin="B000SCAN01", title="Completely Unrelated Product Name",
            category="Toys & Games", sales_rank=20000, est_sell_price_pence=2400, rank_history_days=200,
        )
    })
    monkeypatch.setattr(keepa_client, "stage2_full", lambda db, asins: {
        "B000SCAN01": _stage2(asin="B000SCAN01"),
    })
    sent_embeds = []
    import app.discord_notifier as dn
    monkeypatch.setattr(dn, "send_ping", lambda webhook_url, embed: sent_embeds.append(embed) or True)

    pipeline.process_deal(db_session, raw, _decision_cfg(), _fee_provider(), _APP_CFG)

    deal = db_session.query(models.Deal).filter(models.Deal.url == raw.url).first()
    assert deal.status != "title_mismatch"
    assert deal.status == "pinged"
    # the embed title uses the real Keepa title, not the EAN placeholder
    assert sent_embeds[0]["title"] == "Completely Unrelated Product Name"


def test_no_match_found_drops_silently_without_keepa_call(db_session, monkeypatch):
    """Fix Build Guide phase 2: no EAN/ASIN/title-search match -> dropped
    (status recorded, no Discord post), not posted as "unverified"."""
    raw = RawDeal(
        source="hotukdeals", retailer="Joybuy", title="Mystery Deal",
        url="https://www.hotukdeals.com/deals/mystery-deal-1111111",
        buy_price_pence=500, image_url=None,
    )
    monkeypatch.setattr(resolver, "resolve", lambda url, key="": resolver.ResolvedDeal(
        final_url="https://www.joybuy.co.uk/dp/x", html="<html><body>no structured data</body></html>",
        status_code=200, blocked=False,
    ))

    def _boom(*args, **kwargs):
        raise AssertionError("stage1_screen must not be called when there's no EAN/ASIN")
    monkeypatch.setattr(keepa_client, "stage1_screen", _boom)

    sent_embeds = []
    import app.discord_notifier as dn
    monkeypatch.setattr(dn, "send_ping", lambda webhook_url, embed: sent_embeds.append(embed) or True)

    pipeline.process_deal(db_session, raw, _decision_cfg(), _fee_provider(), _APP_CFG)

    deal = db_session.query(models.Deal).filter(models.Deal.url == raw.url).first()
    assert deal.status == "no_ean_match"
    assert deal.product_id is None
    assert len(sent_embeds) == 0


def test_title_mismatch_rejects_wrong_product(db_session, monkeypatch):
    """A title-search top result sharing zero word overlap with the deal
    title (the Fix Build Guide's DOOM/webcam example) must be dropped before
    ever reaching the expensive stage2 Keepa call."""
    raw = RawDeal(
        source="hotukdeals", retailer="Screwfix", title="Forge Steel Drill AF300UK Deal",
        url="https://www.hotukdeals.com/deals/forge-steel-drill-6666666",
        buy_price_pence=1200, image_url=None,
    )
    monkeypatch.setattr(resolver, "resolve", lambda url, key="": resolver.ResolvedDeal(
        final_url="https://www.screwfix.com/p/forge-steel-drill", html="<html><body>no structured data</body></html>",
        status_code=200, blocked=False,
    ))
    monkeypatch.setattr(keepa_client, "search_by_term", lambda db, term: keepa_client.Stage1Result(
        asin="B000WRONG1", title="DOOM Eternal PS4 Game", category="Video Games",
        sales_rank=5000, est_sell_price_pence=3000, rank_history_days=200,
    ))

    def _boom(*args, **kwargs):
        raise AssertionError("stage2_full must not be called on a title mismatch")
    monkeypatch.setattr(keepa_client, "stage2_full", _boom)

    sent_embeds = []
    import app.discord_notifier as dn
    monkeypatch.setattr(dn, "send_ping", lambda webhook_url, embed: sent_embeds.append(embed) or True)

    pipeline.process_deal(db_session, raw, _decision_cfg(), _fee_provider(), _APP_CFG)

    deal = db_session.query(models.Deal).filter(models.Deal.url == raw.url).first()
    assert deal.status == "title_mismatch"
    assert len(sent_embeds) == 0


def test_price_sanity_rejects_implausible_buy_price(db_session, monkeypatch):
    """A buy price far below the retailer page's own listed price is almost
    certainly a scrape/regex mis-parse (the guide's "£1.00 Longines" case) --
    reject before any Keepa spend."""
    raw = RawDeal(
        source="hotukdeals", retailer="Watches Etc", title="Longines Watch Deal",
        url="https://www.hotukdeals.com/deals/longines-watch-3333333",
        buy_price_pence=100, image_url=None,
    )
    page_html = (
        '<html><body><script type="application/ld+json">'
        '{"@type": "Product", "offers": {"@type": "Offer", "price": "899.00"}}'
        "</script></body></html>"
    )
    monkeypatch.setattr(resolver, "resolve", lambda url, key="": resolver.ResolvedDeal(
        final_url="https://www.watchesetc.example/longines", html=page_html,
        status_code=200, blocked=False,
    ))

    def _boom(*args, **kwargs):
        raise AssertionError("stage1_screen must not be called on a price-sanity reject")
    monkeypatch.setattr(keepa_client, "stage1_screen", _boom)

    pipeline.process_deal(db_session, raw, _decision_cfg(), _fee_provider(), _APP_CFG)

    deal = db_session.query(models.Deal).filter(models.Deal.url == raw.url).first()
    assert deal.status == "price_sanity_reject"


def test_velocity_gate_rejects_low_sales_high_roi_deal(db_session, monkeypatch):
    """Batman-Batmobile case (Fix Build Guide phase 3): huge ROI but only a
    handful of monthly sales and a loose leaf-category rank percentile ->
    reject, never reaches Discord."""
    raw = RawDeal(
        source="hotukdeals", retailer="Amazon", title="Batman Batmobile Toy",
        url="https://www.hotukdeals.com/deals/batmobile-7777777",
        buy_price_pence=500, image_url=None,
    )
    monkeypatch.setattr(resolver, "resolve", lambda url, key="": resolver.ResolvedDeal(
        final_url="https://www.amazon.co.uk/dp/B000BATMOB?tag=x", html="<html></html>",
        status_code=200, blocked=False,
    ))
    monkeypatch.setattr(keepa_client, "stage1_screen", lambda db, codes, is_ean: {
        "B000BATMOB": keepa_client.Stage1Result(
            asin="B000BATMOB", title="Batman Batmobile Toy", category="Toys & Games",
            sales_rank=20000, est_sell_price_pence=2500, rank_history_days=200,
        )
    })
    monkeypatch.setattr(keepa_client, "stage2_full", lambda db, asins: {
        "B000BATMOB": _stage2(
            asin="B000BATMOB", title="Batman Batmobile Toy", buybox_price_pence=2500,
            fba_offer_count=1, est_monthly_sales=4,
            leaf_category_id=123456, leaf_category_rank=20000,
        ),
    })
    # 20000 / 500000 = 4% -> looser than the 2% top-percentile bar, so
    # neither velocity leg clears.
    monkeypatch.setattr(keepa_client, "get_category_size", lambda db, cat_id: 500000)

    sent_embeds = []
    import app.discord_notifier as dn
    monkeypatch.setattr(dn, "send_ping", lambda webhook_url, embed: sent_embeds.append(embed) or True)

    pipeline.process_deal(db_session, raw, _decision_cfg(), _fee_provider(), _APP_CFG)

    deal = db_session.query(models.Deal).filter(models.Deal.url == raw.url).first()
    score = db_session.query(models.Score).filter(models.Score.deal_id == deal.id).first()
    assert score.verdict == "REJECT"
    assert "velocity_floor" in score.verdict_reason
    assert len(sent_embeds) == 0


def test_title_search_fallback_matches_via_model_number(db_session, monkeypatch):
    """No amazon_url, no JSON-LD EAN, but a model-number-shaped code in the
    title -> spec priority #2 kicks in: extract the code, search Keepa,
    match at confidence='low'."""
    raw = RawDeal(
        source="hotukdeals", retailer="Screwfix", title="Forge Steel Drill AF300UK Deal",
        url="https://www.hotukdeals.com/deals/forge-steel-drill-5555555",
        buy_price_pence=1200, image_url=None,
    )
    monkeypatch.setattr(resolver, "resolve", lambda url, key="": resolver.ResolvedDeal(
        final_url="https://www.screwfix.com/p/forge-steel-drill", html="<html><body>no structured data</body></html>",
        status_code=200, blocked=False,
    ))

    search_calls = []

    def fake_search_by_term(db, term):
        search_calls.append(term)
        return keepa_client.Stage1Result(
            asin="B000SEARCH1", title="Forge Steel Drill", category="Tools & Home Improvement",
            sales_rank=5000, est_sell_price_pence=3000, rank_history_days=200,
        )
    monkeypatch.setattr(keepa_client, "search_by_term", fake_search_by_term)
    monkeypatch.setattr(keepa_client, "stage2_full", lambda db, asins: {
        "B000SEARCH1": _stage2(
            asin="B000SEARCH1", title="Forge Steel Drill", category="Tools & Home Improvement",
            sales_rank=5000, buybox_price_pence=3000, fba_offer_count=1, est_monthly_sales=40,
            buybox_avg_90d_pence=2900,
        ),
    })

    import app.discord_notifier as dn
    monkeypatch.setattr(dn, "send_ping", lambda webhook_url, embed: True)

    pipeline.process_deal(db_session, raw, _decision_cfg(), _fee_provider(), _APP_CFG)

    assert search_calls == ["AF300UK"]
    deal = db_session.query(models.Deal).filter(models.Deal.url == raw.url).first()
    product = db_session.get(models.Product, deal.product_id)
    assert product.asin == "B000SEARCH1"
    assert product.matched_via == "title_search"
    assert product.confidence == "low"
    # Real Keepa-sourced title, not the deal's own title -- see pipeline.py's
    # _resolve_product title-passthrough fix. Needed for title validation to
    # be a real check rather than the deal title trivially matching itself.
    assert product.title == "Forge Steel Drill"
    score = db_session.query(models.Score).filter(models.Score.deal_id == deal.id).first()
    assert "low_confidence" in score.flags_json


def test_title_search_result_is_cached_across_deals(db_session, monkeypatch):
    """Two different HUKD posts mentioning the same code must only hit
    Keepa's product-finder once -- the second reuses the cache."""
    def _raw(url):
        return RawDeal(
            source="hotukdeals", retailer="Screwfix", title="Forge Steel Drill AF300UK Deal",
            url=url, buy_price_pence=1200, image_url=None,
        )
    monkeypatch.setattr(resolver, "resolve", lambda url, key="": resolver.ResolvedDeal(
        final_url=f"https://www.screwfix.com/p/{url[-7:]}", html="<html><body>no structured data</body></html>",
        status_code=200, blocked=False,
    ))

    search_calls = []

    def fake_search_by_term(db, term):
        search_calls.append(term)
        return keepa_client.Stage1Result(
            asin="B000SEARCH2", title="Forge Steel Drill", category="Tools & Home Improvement",
            sales_rank=200000, est_sell_price_pence=None, rank_history_days=None,
        )
    monkeypatch.setattr(keepa_client, "search_by_term", fake_search_by_term)
    # Second deal's cache-hit path has no stage1 data attached (see
    # pipeline._try_title_search), so process_deal falls back to a plain
    # stage1_screen lookup -- stub it rather than hitting real Keepa.
    monkeypatch.setattr(keepa_client, "stage1_screen", lambda db, codes, is_ean: {})

    pipeline.process_deal(db_session, _raw("https://www.hotukdeals.com/deals/drill-a-1111111"), _decision_cfg(), _fee_provider(), _APP_CFG)
    pipeline.process_deal(db_session, _raw("https://www.hotukdeals.com/deals/drill-b-2222222"), _decision_cfg(), _fee_provider(), _APP_CFG)

    assert search_calls == ["AF300UK"]   # second deal reused the cache, no second Keepa search


def test_keepa_fulfilment_fee_yields_clean_pass_not_estimated(db_session, monkeypatch):
    """When Keepa supplies real fbaFees.pickAndPackFee AND referralFeePercentage,
    pipeline must pass both through to fee_provider.get_fees() and the
    resulting score must drop the estimated_fees soft flag (only a
    config-table fallback on either component counts as "estimated" — see
    pricing/fees.py). referral_fee_percentage=15.0 deliberately matches the
    config table's "Toys & Games" 15% so the expected net_profit/roi below
    are unchanged from the config-table math -- only `estimated` flips."""
    raw = RawDeal(
        source="hotukdeals", retailer="Amazon", title="Widget Deal",
        url="https://www.hotukdeals.com/deals/widget-deal-9999999",
        buy_price_pence=1000, image_url=None,
    )
    monkeypatch.setattr(resolver, "resolve", lambda url, key="": resolver.ResolvedDeal(
        final_url="https://www.amazon.co.uk/dp/B000WIDGT2?tag=x", html="<html></html>",
        status_code=200, blocked=False,
    ))
    monkeypatch.setattr(keepa_client, "stage1_screen", lambda db, codes, is_ean: {
        "B000WIDGT2": keepa_client.Stage1Result(
            asin="B000WIDGT2", title="Widget", category="Toys & Games",
            sales_rank=20000, est_sell_price_pence=2400, rank_history_days=200,
        )
    })
    monkeypatch.setattr(keepa_client, "stage2_full", lambda db, asins: {
        "B000WIDGT2": _stage2(asin="B000WIDGT2", fba_fulfilment_fee_pence=280, referral_fee_percentage=15.0),
    })

    import app.discord_notifier as dn
    monkeypatch.setattr(dn, "send_ping", lambda webhook_url, embed: True)

    pipeline.process_deal(db_session, raw, _decision_cfg(), _fee_provider(), _APP_CFG)

    deal = db_session.query(models.Deal).filter(models.Deal.url == raw.url).first()
    score = db_session.query(models.Score).filter(models.Score.deal_id == deal.id).first()
    assert score.verdict == "PASS"
    assert score.flags_json == []
    assert score.fees_json["fba_fulfilment_fee_pence"] == 280
    assert score.fees_json["estimated"] is False
    # total_fees = (375+280)*1.20 = 786; storage = 27*1;
    # net_profit = 2500 - 786 - 27 - 40 - 1000(buy_price) = 647
    assert score.net_profit == 647
    assert score.roi == pytest.approx(0.647)


def test_pokemon_center_restock_reprocesses_at_same_price(db_session, monkeypatch):
    """Pokemon Center's RRP doesn't move between restocks -- stock_state
    (upstream of process_deal) is what gates freshness, so a same-price
    resurface here must NOT be suppressed the way an ordinary same-price
    HUKD/Argos resurface would be (see pipeline.py's _ALWAYS_RETRIABLE_SOURCES).
    This site has no EAN/ASIN mechanism at all and no other notification
    path (see pipeline.py's _UNMATCHABLE_BY_DESIGN_SOURCES), so unlike an
    ordinary HUKD/Argos no-match, it still pings -- that behaviour predates
    and is unrelated to the Fix Build Guide's "no match -> don't post" rule
    for arbitrage sources."""
    raw = RawDeal(
        source="pokemon_center", retailer="Pokemon Center", title="Elite Trainer Box",
        url="https://www.pokemoncenter.com/en-gb/product/1/etb",
        buy_price_pence=5699, image_url=None, html="<html><body>no structured data</body></html>",
    )

    sent_embeds = []
    import app.discord_notifier as dn
    monkeypatch.setattr(dn, "send_ping", lambda webhook_url, embed: sent_embeds.append(embed) or True)

    # First drop: pings unverified (no EAN/ASIN on this site).
    pipeline.process_deal(db_session, raw, _decision_cfg(), _fee_provider(), _APP_CFG)
    deal = db_session.query(models.Deal).filter(models.Deal.url == raw.url).first()
    assert deal.status == "unverified_pinged"
    assert len(sent_embeds) == 1

    # Restocked again later at the identical RRP -- must still re-ping, not
    # get silently skipped as "unchanged".
    pipeline.process_deal(db_session, raw, _decision_cfg(), _fee_provider(), _APP_CFG)
    assert len(sent_embeds) == 2


def test_scraper_deal_with_html_skips_resolver(db_session, monkeypatch):
    """A RawDeal with html already set (retailer scraper, e.g. Argos) must
    not call resolver.resolve() at all — url is already the final retailer
    page, unlike HUKD deals which need the redirect wrapper followed."""
    raw = RawDeal(
        source="argos", retailer="Argos", title="Clearance Widget",
        url="https://www.argos.co.uk/product/7654321",
        buy_price_pence=1500, image_url=None,
        html="<html><body>no structured data</body></html>",
    )

    def _boom(url):
        raise AssertionError("resolver.resolve must not be called for scraper deals")
    monkeypatch.setattr(resolver, "resolve", _boom)

    sent_embeds = []
    import app.discord_notifier as dn
    monkeypatch.setattr(dn, "send_ping", lambda webhook_url, embed: sent_embeds.append(embed) or True)

    pipeline.process_deal(db_session, raw, _decision_cfg(), _fee_provider(), _APP_CFG)

    deal = db_session.query(models.Deal).filter(models.Deal.url == raw.url).first()
    assert deal.retailer_url == raw.url
    assert deal.status == "no_ean_match"   # no JSON-LD in the fixture html -> no match, dropped silently
    assert len(sent_embeds) == 0


def test_same_price_resurface_is_skipped(db_session, monkeypatch):
    raw = RawDeal(
        source="hotukdeals", retailer="Amazon", title="Repeat Deal",
        url="https://www.hotukdeals.com/deals/repeat-deal-2222222",
        buy_price_pence=1000, image_url=None,
    )
    call_count = {"n": 0}

    def _resolve(url, key=""):
        call_count["n"] += 1
        return resolver.ResolvedDeal(final_url="https://www.amazon.co.uk/dp/B000REPEAT?x", html="<html></html>", status_code=200, blocked=False)
    monkeypatch.setattr(resolver, "resolve", _resolve)
    monkeypatch.setattr(keepa_client, "stage1_screen", lambda db, codes, is_ean: {
        "B000REPEAT": keepa_client.Stage1Result(
            asin="B000REPEAT", title="Widget", category="Toys & Games",
            sales_rank=200000, est_sell_price_pence=None, rank_history_days=None,
        )
    })

    pipeline.process_deal(db_session, raw, _decision_cfg(), _fee_provider(), _APP_CFG)
    first_status = db_session.query(models.Deal).filter(models.Deal.url == raw.url).first().status
    assert first_status == "stage1_rejected"   # rank 200000 > 80000 threshold
    assert call_count["n"] == 1

    # Same URL, same price, resurfacing on the next poll -> must not reprocess.
    pipeline.process_deal(db_session, raw, _decision_cfg(), _fee_provider(), _APP_CFG)
    assert call_count["n"] == 1
