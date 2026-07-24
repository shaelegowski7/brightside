"""Boot smoke test — proves `app.main:app` imports and starts without
error, and /health responds with the right shape. Uses the sqlite test DB
from conftest.py, so it needs no real Postgres/Keepa/Discord."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


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
