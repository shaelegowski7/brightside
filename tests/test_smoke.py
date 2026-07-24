"""Boot smoke test — proves `app.main:app` imports and starts without
error, and /health responds with the right shape. Uses the sqlite test DB
from conftest.py, so it needs no real Postgres/Keepa/Discord."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
_AUTH_HEADERS = {"X-Shared-Secret": "test-shared-secret"}


def test_root_responds():
    resp = client.get("/")
    assert resp.status_code == 200


def test_health_reports_ok():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["db"] == "ok"
    assert body["checks"]["scheduler"] == "ok"


def test_status_summary_responds_with_expected_shape():
    resp = client.get("/status/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["hours"] == 24
    assert "by_source" in body
    assert "keepa_tokens" in body


def test_status_summary_respects_hours_query_param():
    resp = client.get("/status/summary?hours=6")
    assert resp.status_code == 200
    assert resp.json()["hours"] == 6


def _seed_scored_product(db_session, asin: str) -> None:
    from app import models

    product = models.Product(ean=None, asin=asin, title="Widget", matched_via="amazon_url", confidence="high")
    db_session.add(product)
    db_session.commit()
    deal = models.Deal(source="hotukdeals", title="Widget deal", url=f"https://x/{asin}", buy_price=1000, status="pinged", product_id=product.id)
    db_session.add(deal)
    db_session.commit()
    score = models.Score(deal_id=deal.id, verdict="PASS", roi=0.5)
    db_session.add(score)
    db_session.commit()


def test_create_purchase_requires_shared_secret(db_session):
    _seed_scored_product(db_session, "B000SMOKE1")
    resp = client.post("/purchases", json={"asin": "B000SMOKE1", "qty": 1, "actual_buy_price": 1000})
    assert resp.status_code == 401


def test_create_purchase_succeeds_with_secret(db_session):
    _seed_scored_product(db_session, "B000SMOKE2")
    resp = client.post(
        "/purchases", json={"asin": "B000SMOKE2", "qty": 1, "actual_buy_price": 1000}, headers=_AUTH_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["qty"] == 1


def test_create_purchase_404_when_no_score(db_session):
    resp = client.post(
        "/purchases", json={"asin": "B000UNKNOWN", "qty": 1, "actual_buy_price": 1000}, headers=_AUTH_HEADERS,
    )
    assert resp.status_code == 404


def test_create_outcome_succeeds_with_secret(db_session):
    _seed_scored_product(db_session, "B000SMOKE3")
    purchase_resp = client.post(
        "/purchases", json={"asin": "B000SMOKE3", "qty": 1, "actual_buy_price": 1000}, headers=_AUTH_HEADERS,
    )
    purchase_id = purchase_resp.json()["id"]

    resp = client.post(
        "/outcomes",
        json={"purchase_id": purchase_id, "sold_price": 2000, "sold_date": "2026-07-24T00:00:00Z", "actual_fees": 300},
        headers=_AUTH_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["sold_price"] == 2000


def test_scan_requires_shared_secret():
    resp = client.post("/scan", json={"ean": "5901234123457", "buy_price": 1000})
    assert resp.status_code == 401


def test_scan_succeeds_with_secret(monkeypatch):
    from app import keepa_client

    monkeypatch.setattr(keepa_client, "stage1_screen", lambda db, codes, is_ean: {
        codes[0]: keepa_client.Stage1Result(
            asin="B000SCANSMOKE", title="Widget", category="Toys & Games",
            sales_rank=20000, est_sell_price_pence=2400, rank_history_days=200,
        )
    })
    monkeypatch.setattr(keepa_client, "stage2_full", lambda db, asins: {
        "B000SCANSMOKE": keepa_client.Stage2Result(
            asin="B000SCANSMOKE", title="Widget", category="Toys & Games",
            sales_rank=20000, buybox_price_pence=2500, amazon_on_listing=False,
            fba_offer_count=2, lowest_fba_offer_pence=None, est_monthly_sales=60,
            buybox_avg_90d_pence=2400, rank_history_days=200, hazmat=False,
            package_weight_kg=None, package_longest_cm=None, package_dims_sum_cm=None,
            fba_fulfilment_fee_pence=None, referral_fee_percentage=None,
            leaf_category_id=None, leaf_category_rank=None,
        )
    })
    monkeypatch.setattr("app.discord_notifier.send_ping", lambda url, embed: True)

    resp = client.post("/scan", json={"ean": "5901234123457", "buy_price": 1000}, headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["asin"] == "B000SCANSMOKE"
    assert body["verdict"] == "PASS_WITH_FLAGS"


def test_scan_cors_preflight_allows_configured_origin():
    resp = client.options(
        "/scan",
        headers={
            "Origin": "https://pwa.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-Shared-Secret",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "https://pwa.example.com"
