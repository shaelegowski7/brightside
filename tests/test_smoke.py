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
