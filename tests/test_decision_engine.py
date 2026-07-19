"""Decision-engine maths, verified offline against hardcoded fixtures — no
DB, no Keepa, no Discord. Run before wiring any live API (see
fba-deal-scanner-spec.md "Decision engine" for the formula this checks)."""
import pytest

from app.decision.engine import DecisionConfig, FeeInput, ScoreInput, Verdict, score_deal


def default_config(**overrides) -> DecisionConfig:
    base = dict(
        min_roi=0.30,
        min_net_profit_pence=300,
        max_fba_offers=6,
        rank_history_min_days=90,
        price_spike_pct=0.20,
        vat_registered=False,
        reject_oversize=True,
        category_rank_thresholds={
            "Toys & Games": 80000,
            "Electronics": 60000,
            "Home & Kitchen": 100000,
        },
        default_rank_threshold=150000,
        category_blocklist=set(),
        inbound_shipping_pence=40,
    )
    base.update(overrides)
    return DecisionConfig(**base)


def test_clear_pass_no_flags():
    """Healthy margin, confirmed buy box, high-confidence match, no spike,
    plenty of rank history, real (non-estimated) fees -> clean PASS."""
    inp = ScoreInput(
        buy_price_pence=1000,
        match_confidence="high",
        category="Toys & Games",
        fba_offer_count=2,
        amazon_on_listing=False,
        sales_rank=20000,
        est_monthly_sales=60,
        buybox_price_pence=2500,
        buybox_avg_90d_pence=2400,
        rank_history_days=200,
        fees=FeeInput(
            referral_fee_pence=375,
            fba_fulfilment_fee_pence=320,
            monthly_storage_fee_pence=27,
            estimated=False,
        ),
    )
    result = score_deal(inp, default_config())

    assert result.verdict == Verdict.PASS
    assert result.flags == []
    assert result.sell_price_pence == 2500
    # total_fees = (375+320)*1.20 = 834; storage = 27*1 (clamped to 1 month);
    # net_profit = 2500 - 834 - 27 - 40 - 1000(buy_price) = 599
    assert result.net_profit_pence == 599
    assert result.roi == pytest.approx(0.599)
    assert result.est_months_to_sell == 1.0


def test_amazon_on_listing_rejects_before_financials():
    """Amazon holding the listing is an auto-reject regardless of margin —
    net_profit/roi must not even be computed."""
    inp = ScoreInput(
        buy_price_pence=1500,
        match_confidence="high",
        category="Electronics",
        fba_offer_count=1,
        amazon_on_listing=True,
        sales_rank=5000,
        est_monthly_sales=100,
        buybox_price_pence=3000,
        buybox_avg_90d_pence=2900,
        rank_history_days=365,
        fees=FeeInput(
            referral_fee_pence=240,
            fba_fulfilment_fee_pence=320,
            monthly_storage_fee_pence=27,
            estimated=False,
        ),
    )
    result = score_deal(inp, default_config())

    assert result.verdict == Verdict.REJECT
    assert result.verdict_reason == "amazon_on_listing"
    assert result.sell_price_pence == 3000
    assert result.net_profit_pence is None
    assert result.roi is None


def test_soft_flags_produce_amber_pass():
    """Suppressed buy box, low-confidence match, estimated fees, and thin
    rank history all annotate the ping without failing it, since the
    underlying margin still clears both thresholds."""
    inp = ScoreInput(
        buy_price_pence=800,
        match_confidence="low",
        category="Home & Kitchen",
        fba_offer_count=3,
        amazon_on_listing=False,
        sales_rank=50000,
        est_monthly_sales=30,
        buybox_price_pence=None,
        lowest_fba_offer_pence=2000,
        buybox_avg_90d_pence=1900,
        rank_history_days=45,
        fees=FeeInput(
            referral_fee_pence=300,
            fba_fulfilment_fee_pence=320,
            monthly_storage_fee_pence=27,
            estimated=True,
        ),
    )
    result = score_deal(inp, default_config())

    assert result.verdict == Verdict.PASS_WITH_FLAGS
    assert set(result.flags) == {"no_buybox", "low_confidence", "estimated_fees", "short_rank_history"}
    assert result.sell_price_pence == 2000
    # total_fees = (300+320)*1.20 = 744; storage = 27*1;
    # net_profit = 2000 - 744 - 27 - 40 - 800(buy_price) = 389
    assert result.net_profit_pence == 389
    assert result.roi == pytest.approx(0.48625)


def test_no_live_offers_hard_rejects_with_no_sell_price():
    inp = ScoreInput(
        buy_price_pence=1000,
        match_confidence="high",
        category="Electronics",
        fba_offer_count=0,
        amazon_on_listing=False,
        buybox_price_pence=None,
        lowest_fba_offer_pence=None,
        fees=FeeInput(
            referral_fee_pence=0,
            fba_fulfilment_fee_pence=0,
            monthly_storage_fee_pence=0,
            estimated=True,
        ),
    )
    result = score_deal(inp, default_config())

    assert result.verdict == Verdict.REJECT
    assert result.verdict_reason == "no_sell_price"
    assert result.sell_price_pence is None


def test_roi_below_threshold_rejects_after_financials():
    """Thin margin: passes every hard filter but roi/net_profit both miss
    threshold -> REJECT with the numbers still populated for review."""
    inp = ScoreInput(
        buy_price_pence=5000,
        match_confidence="high",
        category="Electronics",
        fba_offer_count=2,
        amazon_on_listing=False,
        sales_rank=10000,
        est_monthly_sales=50,
        buybox_price_pence=5600,
        fees=FeeInput(
            referral_fee_pence=448,
            fba_fulfilment_fee_pence=320,
            monthly_storage_fee_pence=27,
            estimated=True,
        ),
    )
    result = score_deal(inp, default_config())

    assert result.verdict == Verdict.REJECT
    assert "roi" in result.verdict_reason
    assert result.net_profit_pence is not None
    assert result.roi < 0.30
