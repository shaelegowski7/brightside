"""spapi_client (Phase 2, dormant) -- token caching, None-on-failure/
unconfigured, and cache hit/miss/TTL behavior, all mocked. No real SP-API
credentials exist (see the module's own docstring), so these tests are the
only verification available -- see the plan's own "genuinely unverifiable
without live credentials" boundary for what this can't prove."""
from datetime import datetime, timedelta, timezone

import pytest

from app import models, spapi_client
from app.config import Settings


def _settings(configured: bool) -> Settings:
    suffix = "" if not configured else "x"
    return Settings(
        database_url="sqlite:///:memory:", keepa_api_key="k", discord_webhook_url="https://x",
        pwa_shared_secret="s", environment="test", scraperapi_key="", pwa_origin="",
        spapi_client_id=f"client-id{suffix}" if configured else "",
        spapi_client_secret=f"client-secret{suffix}" if configured else "",
        spapi_refresh_token=f"refresh-token{suffix}" if configured else "",
        spapi_seller_id=f"seller-id{suffix}" if configured else "",
        spapi_marketplace_id="A1F83G8C2ARO7P" if configured else "",
    )


@pytest.fixture(autouse=True)
def _reset_token_cache():
    spapi_client._token_cache["access_token"] = None
    spapi_client._token_cache["expires_at"] = None
    yield
    spapi_client._token_cache["access_token"] = None
    spapi_client._token_cache["expires_at"] = None


class _FakeResponse:
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json


def _fees_response() -> _FakeResponse:
    return _FakeResponse({
        "FeesEstimateResult": {"FeesEstimate": {"FeeDetailList": [
            {"FeeType": "ReferralFee", "FeeAmount": {"Amount": 3.00}},
            {"FeeType": "FBAFees", "FeeAmount": {"Amount": 2.25}},
        ]}}
    })


def _token_or_fees(url, *a, **kw) -> _FakeResponse:
    if "auth" in url:
        return _FakeResponse({"access_token": "tok", "expires_in": 3600})
    return _fees_response()


def test_is_configured_true_when_all_fields_set(monkeypatch):
    monkeypatch.setattr(spapi_client, "get_settings", lambda: _settings(configured=True))
    assert spapi_client.is_configured() is True


def test_is_configured_false_when_any_field_missing(monkeypatch):
    monkeypatch.setattr(spapi_client, "get_settings", lambda: _settings(configured=False))
    assert spapi_client.is_configured() is False


def test_get_access_token_caches_within_expiry(monkeypatch):
    monkeypatch.setattr(spapi_client, "get_settings", lambda: _settings(configured=True))
    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        return _FakeResponse({"access_token": "tok-1", "expires_in": 3600})

    monkeypatch.setattr(spapi_client.requests, "post", fake_post)

    token1 = spapi_client._get_access_token()
    token2 = spapi_client._get_access_token()

    assert token1 == token2 == "tok-1"
    assert len(calls) == 1


def test_get_access_token_refreshes_after_expiry(monkeypatch):
    monkeypatch.setattr(spapi_client, "get_settings", lambda: _settings(configured=True))
    responses = iter([
        _FakeResponse({"access_token": "tok-1", "expires_in": 3600}),
        _FakeResponse({"access_token": "tok-2", "expires_in": 3600}),
    ])
    monkeypatch.setattr(spapi_client.requests, "post", lambda *a, **kw: next(responses))

    token1 = spapi_client._get_access_token()
    spapi_client._token_cache["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    token2 = spapi_client._get_access_token()

    assert token1 == "tok-1"
    assert token2 == "tok-2"


def test_get_access_token_returns_none_on_http_failure(monkeypatch):
    monkeypatch.setattr(spapi_client, "get_settings", lambda: _settings(configured=True))

    def fake_post(*a, **kw):
        import requests
        raise requests.RequestException("boom")

    monkeypatch.setattr(spapi_client.requests, "post", fake_post)

    assert spapi_client._get_access_token() is None


def test_get_fees_estimate_returns_none_when_unconfigured_and_no_cache(db_session, monkeypatch):
    monkeypatch.setattr(spapi_client, "get_settings", lambda: _settings(configured=False))
    assert spapi_client.get_fees_estimate(db_session, "B000TEST", 2000) is None


def test_get_fees_estimate_fetches_and_caches(db_session, monkeypatch):
    monkeypatch.setattr(spapi_client, "get_settings", lambda: _settings(configured=True))
    monkeypatch.setattr(spapi_client.requests, "post", _token_or_fees)

    result = spapi_client.get_fees_estimate(db_session, "B000TEST", 2000)

    assert result.referral_fee_pence == 300
    assert result.fba_fulfilment_fee_pence == 225


def test_get_fees_estimate_uses_cache_without_second_fetch(db_session, monkeypatch):
    monkeypatch.setattr(spapi_client, "get_settings", lambda: _settings(configured=True))
    call_count = {"n": 0}

    def fake_post(url, **kw):
        if "auth" in url:
            return _FakeResponse({"access_token": "tok", "expires_in": 3600})
        call_count["n"] += 1
        return _fees_response()

    monkeypatch.setattr(spapi_client.requests, "post", fake_post)

    spapi_client.get_fees_estimate(db_session, "B000TEST", 2000)
    spapi_client.get_fees_estimate(db_session, "B000TEST", 2000)

    assert call_count["n"] == 1


def test_get_fees_estimate_refetches_after_ttl_expiry(db_session, monkeypatch):
    monkeypatch.setattr(spapi_client, "get_settings", lambda: _settings(configured=True))
    call_count = {"n": 0}

    def fake_post(url, **kw):
        if "auth" in url:
            return _FakeResponse({"access_token": "tok", "expires_in": 3600})
        call_count["n"] += 1
        return _fees_response()

    monkeypatch.setattr(spapi_client.requests, "post", fake_post)

    spapi_client.get_fees_estimate(db_session, "B000TEST", 2000)
    row = db_session.get(models.FeeEstimateCache, ("B000TEST", spapi_client._price_band(2000)))
    row.fetched_at = datetime.now(timezone.utc) - timedelta(hours=25)
    db_session.commit()

    spapi_client.get_fees_estimate(db_session, "B000TEST", 2000)

    assert call_count["n"] == 2


def test_check_gating_true_when_reasons_present(db_session, monkeypatch):
    monkeypatch.setattr(spapi_client, "get_settings", lambda: _settings(configured=True))
    monkeypatch.setattr(spapi_client.requests, "post", lambda url, **kw: _FakeResponse({"access_token": "tok", "expires_in": 3600}))
    monkeypatch.setattr(spapi_client.requests, "get", lambda *a, **kw: _FakeResponse({
        "restrictions": [{"reasons": [{"reasonCode": "APPROVAL_REQUIRED"}]}]
    }))

    assert spapi_client.check_gating(db_session, "B000TEST") is True


def test_check_gating_false_when_no_reasons(db_session, monkeypatch):
    monkeypatch.setattr(spapi_client, "get_settings", lambda: _settings(configured=True))
    monkeypatch.setattr(spapi_client.requests, "post", lambda url, **kw: _FakeResponse({"access_token": "tok", "expires_in": 3600}))
    monkeypatch.setattr(spapi_client.requests, "get", lambda *a, **kw: _FakeResponse({"restrictions": []}))

    assert spapi_client.check_gating(db_session, "B000TEST") is False


def test_check_gating_returns_none_when_unconfigured(db_session, monkeypatch):
    monkeypatch.setattr(spapi_client, "get_settings", lambda: _settings(configured=False))
    assert spapi_client.check_gating(db_session, "B000TEST") is None


def test_check_gating_uses_cache_without_second_fetch(db_session, monkeypatch):
    monkeypatch.setattr(spapi_client, "get_settings", lambda: _settings(configured=True))
    monkeypatch.setattr(spapi_client.requests, "post", lambda url, **kw: _FakeResponse({"access_token": "tok", "expires_in": 3600}))
    call_count = {"n": 0}

    def fake_get(*a, **kw):
        call_count["n"] += 1
        return _FakeResponse({"restrictions": []})

    monkeypatch.setattr(spapi_client.requests, "get", fake_get)

    spapi_client.check_gating(db_session, "B000TEST")
    spapi_client.check_gating(db_session, "B000TEST")

    assert call_count["n"] == 1
