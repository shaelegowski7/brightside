from datetime import datetime, timedelta, timezone

import pytest

from app import models, purchases


def _scored_deal(db_session, asin: str, url: str, roi: float = 0.5) -> models.Score:
    product = db_session.query(models.Product).filter(models.Product.asin == asin).first()
    if product is None:
        product = models.Product(ean=None, asin=asin, title="Widget", matched_via="amazon_url", confidence="high")
        db_session.add(product)
        db_session.commit()

    deal = models.Deal(source="hotukdeals", title="Widget deal", url=url, buy_price=1000, status="pinged", product_id=product.id)
    db_session.add(deal)
    db_session.commit()

    score = models.Score(deal_id=deal.id, verdict="PASS", roi=roi, sell_price=2000, net_profit=500)
    db_session.add(score)
    db_session.commit()
    db_session.refresh(score)
    return score


def test_resolve_latest_score_for_asin_picks_most_recent(db_session):
    _scored_deal(db_session, "B000TEST01", "https://x/1")
    latest = _scored_deal(db_session, "B000TEST01", "https://x/2")

    result = purchases.resolve_latest_score_for_asin(db_session, "B000TEST01")

    assert result.id == latest.id


def test_resolve_latest_score_for_asin_none_when_unknown(db_session):
    assert purchases.resolve_latest_score_for_asin(db_session, "B000UNKNOWN") is None


def test_log_purchase_raises_when_no_score_found(db_session):
    with pytest.raises(purchases.NoScoreFoundError):
        purchases.log_purchase(db_session, "B000UNKNOWN", qty=1, actual_buy_price=1000, notes=None)


def test_log_purchase_succeeds(db_session):
    score = _scored_deal(db_session, "B000TEST02", "https://x/3")

    purchase = purchases.log_purchase(db_session, "B000TEST02", qty=2, actual_buy_price=950, notes="clearance rack")

    assert purchase.score_id == score.id
    assert purchase.qty == 2
    assert purchase.actual_buy_price == 950


def test_log_outcome_raises_when_purchase_not_found(db_session):
    with pytest.raises(purchases.PurchaseNotFoundError):
        purchases.log_outcome(db_session, purchase_id=9999, sold_price=2000, sold_date=datetime.now(timezone.utc), actual_fees=None, notes=None)


def test_log_outcome_raises_when_already_exists(db_session):
    score = _scored_deal(db_session, "B000TEST03", "https://x/4")
    purchase = purchases.log_purchase(db_session, "B000TEST03", qty=1, actual_buy_price=1000, notes=None)
    purchases.log_outcome(db_session, purchase.id, sold_price=2000, sold_date=datetime.now(timezone.utc), actual_fees=300, notes=None)

    with pytest.raises(purchases.OutcomeAlreadyExistsError):
        purchases.log_outcome(db_session, purchase.id, sold_price=1900, sold_date=datetime.now(timezone.utc), actual_fees=300, notes=None)


def test_log_outcome_succeeds(db_session):
    score = _scored_deal(db_session, "B000TEST04", "https://x/5")
    purchase = purchases.log_purchase(db_session, "B000TEST04", qty=1, actual_buy_price=1000, notes=None)
    sold_date = datetime.now(timezone.utc) - timedelta(days=1)

    outcome = purchases.log_outcome(db_session, purchase.id, sold_price=2100, sold_date=sold_date, actual_fees=350, notes="sold fast")

    assert outcome.purchase_id == purchase.id
    assert outcome.sold_price == 2100
    assert outcome.actual_fees == 350
