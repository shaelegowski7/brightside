from datetime import datetime, timedelta, timezone

import pytest

from app import models, monitoring


def _deal(source: str, status: str, url: str, first_seen: datetime) -> models.Deal:
    return models.Deal(
        source=source, title="t", url=url, buy_price=1000,
        status=status, first_seen=first_seen, last_seen=first_seen,
    )


def test_funnel_summary_groups_by_source_and_status(db_session):
    now = datetime.now(timezone.utc)
    db_session.add_all([
        _deal("hotukdeals", "pinged", "https://x/1", now),
        _deal("hotukdeals", "no_ean_match", "https://x/2", now),
        _deal("hotukdeals", "no_ean_match", "https://x/3", now),
        _deal("argos", "stage2_scored", "https://x/4", now),
    ])
    db_session.commit()

    summary = monitoring.funnel_summary(db_session, since=now - timedelta(hours=1))

    assert summary["hotukdeals"]["pinged"] == 1
    assert summary["hotukdeals"]["no_ean_match"] == 2
    assert summary["argos"]["stage2_scored"] == 1


def test_funnel_summary_excludes_deals_outside_window(db_session):
    now = datetime.now(timezone.utc)
    db_session.add(_deal("hotukdeals", "pinged", "https://x/old", now - timedelta(days=2)))
    db_session.commit()

    summary = monitoring.funnel_summary(db_session, since=now - timedelta(hours=1))

    assert summary == {}


def test_keepa_token_summary_sums_by_stage(db_session):
    now = datetime.now(timezone.utc)
    db_session.add_all([
        models.TokenLog(ts=now, stage="stage1_screen", item_count=10, tokens_consumed=15),
        models.TokenLog(ts=now, stage="stage1_screen", item_count=5, tokens_consumed=8),
        models.TokenLog(ts=now, stage="stage2_full", item_count=2, tokens_consumed=26),
        models.TokenLog(ts=now - timedelta(days=2), stage="stage1_screen", item_count=1, tokens_consumed=100),
    ])
    db_session.commit()

    result = monitoring.keepa_token_summary(db_session, since=now - timedelta(hours=1))

    assert result["by_stage"]["stage1_screen"] == {"tokens": 23, "calls": 2}
    assert result["by_stage"]["stage2_full"] == {"tokens": 26, "calls": 1}
    assert result["total_consumed"] == 49


def test_keepa_token_summary_excludes_null_consumed(db_session):
    now = datetime.now(timezone.utc)
    db_session.add(models.TokenLog(ts=now, stage="stage1_screen", item_count=1, tokens_consumed=None))
    db_session.commit()

    result = monitoring.keepa_token_summary(db_session, since=now - timedelta(hours=1))

    assert result["total_consumed"] == 0
    assert result["by_stage"] == {}


def test_build_summary_shape(db_session):
    summary = monitoring.build_summary(db_session, hours=12)
    assert summary["hours"] == 12
    assert "by_source" in summary
    assert "keepa_tokens" in summary
    assert summary["keepa_tokens"]["total_consumed"] == 0


def _scored_purchase(db_session, ts: datetime, roi: float, actual_buy_price: int) -> models.Purchase:
    product = models.Product(ean=None, asin=f"B{ts.timestamp()}", matched_via="amazon_url", confidence="high")
    db_session.add(product)
    db_session.commit()
    deal = models.Deal(source="hotukdeals", title="t", url=f"https://x/{ts.timestamp()}", buy_price=actual_buy_price, status="pinged", product_id=product.id)
    db_session.add(deal)
    db_session.commit()
    score = models.Score(deal_id=deal.id, verdict="PASS", roi=roi)
    db_session.add(score)
    db_session.commit()
    purchase = models.Purchase(score_id=score.id, qty=1, actual_buy_price=actual_buy_price, ts=ts)
    db_session.add(purchase)
    db_session.commit()
    db_session.refresh(purchase)
    return purchase


def test_purchases_outcomes_summary_computes_realised_and_predicted_roi(db_session):
    now = datetime.now(timezone.utc)
    purchase = _scored_purchase(db_session, now, roi=0.5, actual_buy_price=1000)
    db_session.add(models.Outcome(purchase_id=purchase.id, sold_price=2000, sold_date=now, actual_fees=300))
    db_session.commit()

    result = monitoring.purchases_outcomes_summary(db_session, since=now - timedelta(hours=1), until=now + timedelta(hours=1))

    assert result["outcomes_recorded"] == 1
    assert result["purchases_logged"] == 1
    # (2000 - 300 - 1000) / 1000 = 0.7
    assert result["avg_realised_roi"] == pytest.approx(0.7)
    assert result["avg_predicted_roi"] == pytest.approx(0.5)


def test_purchases_outcomes_summary_excludes_outside_window(db_session):
    now = datetime.now(timezone.utc)
    purchase = _scored_purchase(db_session, now - timedelta(days=10), roi=0.5, actual_buy_price=1000)
    db_session.add(models.Outcome(purchase_id=purchase.id, sold_price=2000, sold_date=now - timedelta(days=10), actual_fees=300))
    db_session.commit()

    result = monitoring.purchases_outcomes_summary(db_session, since=now - timedelta(hours=1), until=now + timedelta(hours=1))

    assert result["outcomes_recorded"] == 0
    assert result["avg_realised_roi"] is None
    assert result["avg_predicted_roi"] is None


def test_build_weekly_summary_counts_pings(db_session):
    now = datetime.now(timezone.utc)
    db_session.add(models.Ping(asin="B000TEST", deal_id=1, score_id=1, ts=now))
    db_session.commit()

    summary = monitoring.build_weekly_summary(db_session, hours=168)

    assert summary["pings"] == 1
    assert summary["outcomes_recorded"] == 0


def test_purchases_outcomes_summary_computes_hit_rate(db_session):
    now = datetime.now(timezone.utc)
    hit = _scored_purchase(db_session, now, roi=0.5, actual_buy_price=1000)
    db_session.add(models.Outcome(purchase_id=hit.id, sold_price=2000, sold_date=now, actual_fees=300))
    miss = _scored_purchase(db_session, now + timedelta(seconds=1), roi=0.5, actual_buy_price=1000)
    db_session.add(models.Outcome(purchase_id=miss.id, sold_price=1000, sold_date=now, actual_fees=100))
    db_session.commit()

    result = monitoring.purchases_outcomes_summary(
        db_session, since=now - timedelta(hours=1), until=now + timedelta(hours=1), min_roi_threshold=0.3
    )

    # hit: (2000-300-1000)/1000 = 0.7 >= 0.3; miss: (1000-100-1000)/1000 = -0.1 < 0.3
    assert result["hit_rate"] == pytest.approx(0.5)


def test_purchases_outcomes_summary_hit_rate_none_without_threshold(db_session):
    now = datetime.now(timezone.utc)
    purchase = _scored_purchase(db_session, now, roi=0.5, actual_buy_price=1000)
    db_session.add(models.Outcome(purchase_id=purchase.id, sold_price=2000, sold_date=now, actual_fees=300))
    db_session.commit()

    result = monitoring.purchases_outcomes_summary(db_session, since=now - timedelta(hours=1), until=now + timedelta(hours=1))

    assert result["hit_rate"] is None


def test_purchases_outcomes_summary_hit_rate_none_without_outcomes(db_session):
    now = datetime.now(timezone.utc)
    result = monitoring.purchases_outcomes_summary(
        db_session, since=now - timedelta(hours=1), until=now + timedelta(hours=1), min_roi_threshold=0.3
    )
    assert result["hit_rate"] is None


def _good_score(db_session, ts: datetime, asin: str, verdict: str = "PASS") -> models.Score:
    product = models.Product(ean=None, asin=asin, matched_via="amazon_url", confidence="high")
    db_session.add(product)
    db_session.commit()
    deal = models.Deal(source="hotukdeals", title="t", url=f"https://x/{asin}", buy_price=1000, status="stage2_scored", product_id=product.id)
    db_session.add(deal)
    db_session.commit()
    score = models.Score(deal_id=deal.id, verdict=verdict, roi=0.5, ts=ts)
    db_session.add(score)
    db_session.commit()
    db_session.refresh(score)
    return score


def test_purchases_outcomes_summary_computes_purchase_conversion_rate(db_session):
    now = datetime.now(timezone.utc)
    bought = _good_score(db_session, now, "BCONV1")
    _good_score(db_session, now, "BCONV2")  # confirmed good, never purchased
    db_session.add(models.Purchase(score_id=bought.id, qty=1, actual_buy_price=1000, ts=now))
    db_session.commit()

    result = monitoring.purchases_outcomes_summary(db_session, since=now - timedelta(hours=1), until=now + timedelta(hours=1))

    assert result["good_scores"] == 2
    assert result["purchases_logged"] == 1
    assert result["purchase_conversion_rate"] == pytest.approx(0.5)


def test_purchases_outcomes_summary_excludes_reject_verdicts_from_good_scores(db_session):
    now = datetime.now(timezone.utc)
    _good_score(db_session, now, "BREJ1", verdict="REJECT")
    db_session.commit()

    result = monitoring.purchases_outcomes_summary(db_session, since=now - timedelta(hours=1), until=now + timedelta(hours=1))

    assert result["good_scores"] == 0
    assert result["purchase_conversion_rate"] is None


def test_ping_latency_summary_computes_avg_and_max(db_session):
    now = datetime.now(timezone.utc)
    fast_deal = models.Deal(
        source="hotukdeals", title="t", url="https://x/lat1", buy_price=1000,
        status="pinged", first_seen=now - timedelta(minutes=10), last_seen=now,
    )
    slow_deal = models.Deal(
        source="hotukdeals", title="t", url="https://x/lat2", buy_price=1000,
        status="pinged", first_seen=now - timedelta(minutes=30), last_seen=now,
    )
    db_session.add_all([fast_deal, slow_deal])
    db_session.commit()
    db_session.add_all([
        models.Ping(asin="BLAT1", deal_id=fast_deal.id, score_id=1, ts=now),
        models.Ping(asin="BLAT2", deal_id=slow_deal.id, score_id=1, ts=now),
    ])
    db_session.commit()

    result = monitoring.ping_latency_summary(db_session, since=now - timedelta(hours=1))

    assert result["pings_measured"] == 2
    assert result["avg_latency_minutes"] == pytest.approx(20, abs=0.1)
    assert result["max_latency_minutes"] == pytest.approx(30, abs=0.1)


def test_ping_latency_summary_excludes_pings_outside_window(db_session):
    now = datetime.now(timezone.utc)
    deal = models.Deal(
        source="hotukdeals", title="t", url="https://x/lat-old", buy_price=1000,
        status="pinged", first_seen=now - timedelta(days=3), last_seen=now,
    )
    db_session.add(deal)
    db_session.commit()
    db_session.add(models.Ping(asin="BLATOLD", deal_id=deal.id, score_id=1, ts=now - timedelta(days=2)))
    db_session.commit()

    result = monitoring.ping_latency_summary(db_session, since=now - timedelta(hours=1))

    assert result["pings_measured"] == 0
    assert result["avg_latency_minutes"] is None
    assert result["max_latency_minutes"] is None


def test_build_summary_includes_ping_latency(db_session):
    summary = monitoring.build_summary(db_session, hours=24)
    assert "ping_latency" in summary
    assert summary["ping_latency"]["pings_measured"] == 0
