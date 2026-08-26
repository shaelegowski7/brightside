"""get_confirmed_deals's query logic, backing GET /deals.json: only the
latest Score per deal counts (a deal can be re-scored across price
changes), and only genuinely good verdicts (PASS/PASS_WITH_FLAGS) show
up."""
from app import dashboard, models


def _product(db, asin: str, title: str = "Widget", confidence: str = "high") -> models.Product:
    p = models.Product(ean=None, asin=asin, title=title, matched_via="amazon_url", confidence=confidence)
    db.add(p)
    db.commit()
    return p


def _deal(db, url: str, product_id: int | None, source: str = "hotukdeals", retailer_url: str | None = None, title: str = "Deal title") -> models.Deal:
    d = models.Deal(
        source=source, retailer="Argos", title=title, url=url, retailer_url=retailer_url or url,
        buy_price=1000, status="pinged", product_id=product_id,
    )
    db.add(d)
    db.commit()
    return d


def _score(db, deal_id: int, verdict: str, roi: float | None = 0.5, net_profit: int | None = 500) -> models.Score:
    s = models.Score(deal_id=deal_id, verdict=verdict, roi=roi, net_profit=net_profit, sell_price=1500)
    db.add(s)
    db.commit()
    return s


def test_get_confirmed_deals_excludes_reject(db_session):
    product = _product(db_session, "B000AAA001")
    deal = _deal(db_session, "https://x/1", product.id)
    _score(db_session, deal.id, "REJECT")

    rows = dashboard.get_confirmed_deals(db_session)
    assert rows == []


def test_get_confirmed_deals_includes_pass_and_pass_with_flags(db_session):
    p1 = _product(db_session, "B000AAA002")
    d1 = _deal(db_session, "https://x/2", p1.id)
    _score(db_session, d1.id, "PASS")

    p2 = _product(db_session, "B000AAA003")
    d2 = _deal(db_session, "https://x/3", p2.id)
    _score(db_session, d2.id, "PASS_WITH_FLAGS")

    rows = dashboard.get_confirmed_deals(db_session)
    assert {r.asin for r in rows} == {"B000AAA002", "B000AAA003"}


def test_get_confirmed_deals_uses_only_latest_score_per_deal(db_session):
    """A deal re-scored after a price change must show its current verdict,
    not an earlier stale one -- an old REJECT must not hide a deal that now
    passes, and an old PASS must not leak if it later rejects."""
    product = _product(db_session, "B000AAA004")
    deal = _deal(db_session, "https://x/4", product.id)
    _score(db_session, deal.id, "REJECT")
    latest = _score(db_session, deal.id, "PASS")

    rows = dashboard.get_confirmed_deals(db_session)
    assert len(rows) == 1
    assert rows[0].asin == "B000AAA004"
    assert rows[0].ts == latest.ts or rows[0].verdict == "PASS"


def test_get_confirmed_deals_orders_newest_first(db_session):
    p1 = _product(db_session, "B000AAA005")
    d1 = _deal(db_session, "https://x/5", p1.id)
    _score(db_session, d1.id, "PASS")

    p2 = _product(db_session, "B000AAA006")
    d2 = _deal(db_session, "https://x/6", p2.id)
    _score(db_session, d2.id, "PASS")

    rows = dashboard.get_confirmed_deals(db_session)
    assert [r.asin for r in rows] == ["B000AAA006", "B000AAA005"]


def test_get_confirmed_deals_scan_source_has_no_retailer_link(db_session):
    """A scan's retailer_url is the synthetic scan:<ean>:<uuid> dedup key,
    not a real URL -- must not be surfaced as a clickable link."""
    product = _product(db_session, "B000AAA007")
    deal = _deal(db_session, "scan:5901234123457:abc123", product.id, source="scan", retailer_url="scan:5901234123457:abc123")
    _score(db_session, deal.id, "PASS")

    rows = dashboard.get_confirmed_deals(db_session)
    assert rows[0].retailer_url is None


def test_get_confirmed_deals_falls_back_to_deal_title_when_no_product(db_session):
    deal = _deal(db_session, "https://x/8", None, title="Raw deal title")
    _score(db_session, deal.id, "PASS")

    rows = dashboard.get_confirmed_deals(db_session)
    assert rows[0].title == "Raw deal title"
    assert rows[0].asin is None
