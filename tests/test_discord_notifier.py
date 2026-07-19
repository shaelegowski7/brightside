from app import models
from app.discord_notifier import build_matched_embed, build_unverified_embed, record_ping, should_ping
from app.decision.engine import ScoreResult, Verdict


def _make_scored_deal(db_session, buy_price_pence: int, url: str) -> tuple[models.Deal, models.Score]:
    deal = models.Deal(source="hotukdeals", title="t", url=url, buy_price=buy_price_pence, status="scored")
    db_session.add(deal)
    db_session.commit()
    score = models.Score(deal_id=deal.id, verdict="PASS")
    db_session.add(score)
    db_session.commit()
    return deal, score


def test_should_ping_true_when_no_prior_ping(db_session):
    assert should_ping(db_session, "B000TEST01", 1000, cooldown_hours=24, price_improve_pct=0.10) is True


def test_should_ping_false_within_cooldown_same_price(db_session):
    deal, score = _make_scored_deal(db_session, 1000, "https://x/1")
    record_ping(db_session, "B000TEST02", deal.id, score.id)

    assert should_ping(db_session, "B000TEST02", 1000, cooldown_hours=24, price_improve_pct=0.10) is False


def test_should_ping_true_when_price_improved_enough(db_session):
    deal, score = _make_scored_deal(db_session, 1000, "https://x/2")
    record_ping(db_session, "B000TEST03", deal.id, score.id)

    # 15% cheaper than the 1000p original -> clears the 10% improvement bar
    assert should_ping(db_session, "B000TEST03", 850, cooldown_hours=24, price_improve_pct=0.10) is True


def test_should_ping_false_when_price_improved_but_not_enough(db_session):
    deal, score = _make_scored_deal(db_session, 1000, "https://x/3")
    record_ping(db_session, "B000TEST04", deal.id, score.id)

    # only 5% cheaper -> below the 10% bar
    assert should_ping(db_session, "B000TEST04", 950, cooldown_hours=24, price_improve_pct=0.10) is False


def test_build_matched_embed_pass_is_green_with_no_flags_footer():
    result = ScoreResult(
        verdict=Verdict.PASS, verdict_reason=None, sell_price_pence=2500,
        net_profit_pence=599, roi=0.599, flags=[],
    )
    embed = build_matched_embed(
        title="Widget", retailer_url="https://retailer.example/x", image_url=None,
        retailer="Amazon", asin="B000TEST05", buy_price_pence=1000, result=result,
        est_monthly_sales=60, offer_count=2, amazon_on_listing=False, gated=None,
        match_confidence="high",
    )
    assert embed["color"] == 0x2ECC71
    assert embed["footer"]["text"] == "Amazon"
    assert any(f["name"] == "Links" and "keepa.com/#!product/2-B000TEST05" in f["value"] for f in embed["fields"])


def test_build_matched_embed_pass_with_flags_is_amber():
    result = ScoreResult(
        verdict=Verdict.PASS_WITH_FLAGS, verdict_reason=None, sell_price_pence=2000,
        net_profit_pence=389, roi=0.486, flags=["no_buybox", "low_confidence"],
    )
    embed = build_matched_embed(
        title="Widget", retailer_url="https://retailer.example/x", image_url=None,
        retailer="Very", asin="B000TEST06", buy_price_pence=800, result=result,
        est_monthly_sales=30, offer_count=3, amazon_on_listing=False, gated=None,
        match_confidence="low",
    )
    assert embed["color"] == 0xF1C40F
    assert "no_buybox" in embed["footer"]["text"]


def test_build_unverified_embed_flags_check_manually():
    embed = build_unverified_embed(
        title="Mystery Item", retailer_url="https://retailer.example/y",
        image_url=None, retailer="Joybuy", buy_price_pence=950,
    )
    assert "UNVERIFIED MATCH — check manually" in embed["title"]
    assert embed["color"] == 0xF1C40F
