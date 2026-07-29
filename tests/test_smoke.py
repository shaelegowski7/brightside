"""Boot smoke test — proves `app.main:app` imports and starts without
error, and /health responds with the right shape. Uses the sqlite test DB
from conftest.py, so it needs no real Postgres/Keepa/Discord."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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


def test_deals_dashboard_shows_login_form_when_unauthenticated():
    resp = client.get("/deals")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "login-form" in resp.text


def test_deals_dashboard_rejects_stale_cookie():
    resp = client.get("/deals", cookies={"deals_secret": "wrong-secret"})
    assert resp.status_code == 200
    assert "login-form" in resp.text


def test_deals_login_sets_cookie_and_unlocks_dashboard():
    login_resp = client.post("/deals/login", json={"secret": "test-shared-secret"})
    assert login_resp.status_code == 200
    assert login_resp.cookies.get("deals_secret") == "test-shared-secret"

    dashboard_resp = client.get("/deals", cookies={"deals_secret": "test-shared-secret"})
    assert dashboard_resp.status_code == 200
    assert "login-form" not in dashboard_resp.text
    assert "No confirmed deals yet" in dashboard_resp.text or "<table" in dashboard_resp.text


def test_deals_login_rejects_wrong_secret():
    resp = client.post("/deals/login", json={"secret": "nope"})
    assert resp.status_code == 401
    assert "deals_secret" not in resp.cookies


def test_deals_json_requires_shared_secret():
    resp = client.get("/deals.json")
    assert resp.status_code == 401


def test_deals_json_responds_with_secret_header():
    resp = client.get("/deals.json", headers=_AUTH_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == []


def test_crawl_requires_shared_secret():
    resp = client.post("/crawl")
    assert resp.status_code == 401


def test_crawl_status_requires_shared_secret():
    resp = client.get("/crawl/status")
    assert resp.status_code == 401


def test_crawl_triggers_and_status_reflects_it(monkeypatch):
    from app import crawl_runner
    monkeypatch.setattr(crawl_runner, "start_crawl", lambda: (True, {
        "running": True, "started_at": "2026-07-29T00:00:00Z", "finished_at": None, "sources": [],
    }))
    resp = client.post("/crawl", headers=_AUTH_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["started"] is True
    assert resp.json()["running"] is True

    monkeypatch.setattr(crawl_runner, "get_status", lambda: {
        "running": False, "started_at": "2026-07-29T00:00:00Z", "finished_at": "2026-07-29T00:05:00Z", "sources": [],
    })
    resp2 = client.get("/crawl/status", headers=_AUTH_HEADERS)
    assert resp2.status_code == 200
    assert resp2.json()["running"] is False


def test_crawl_ws_rejects_wrong_secret():
    with client.websocket_connect("/crawl/ws") as ws:
        ws.send_text("wrong-secret")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_crawl_ws_sends_current_status_after_correct_secret(monkeypatch):
    from app import crawl_runner
    fake_status = {"running": False, "started_at": None, "finished_at": None, "sources": []}
    monkeypatch.setattr(crawl_runner, "get_status", lambda: fake_status)

    with client.websocket_connect("/crawl/ws") as ws:
        ws.send_text("test-shared-secret")
        data = ws.receive_json()
    assert data == fake_status


def test_crawl_ws_rejects_when_no_message_sent_in_time(monkeypatch):
    # A client that connects but never sends the secret must not hang the
    # server (or start streaming status) forever -- shrink the real 10s
    # timeout so the test doesn't have to wait for it.
    import app.main as main_module
    monkeypatch.setattr(main_module, "_CRAWL_WS_AUTH_TIMEOUT_S", 0.05)

    with client.websocket_connect("/crawl/ws") as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


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
