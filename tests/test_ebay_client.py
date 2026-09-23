"""ebay_client -- token caching, sandbox/production routing, the
bulk-chunking cap, and the in-band per-item failure parsing that eBay's
HTTP 200 hides. All mocked: no eBay keyset exists yet (see the module's
own docstring), so these prove the contract we coded to, not the contract
eBay actually serves. The first live run is the real verification."""
from datetime import datetime, timedelta, timezone

import pytest
import requests

from app import ebay_client
from app.config import EbaySettings


def _settings(**overrides) -> EbaySettings:
    base = dict(
        env="sandbox", client_id="cid", client_secret="csecret",
        ru_name="Shae-Bright-brigh-abcdef", refresh_token="v^1.1#refresh",
        marketplace_id="EBAY_GB", merchant_location_key="",
        fulfillment_policy_id="", payment_policy_id="", return_policy_id="",
    )
    base.update(overrides)
    return EbaySettings(**base)


@pytest.fixture(autouse=True)
def _reset_token_cache():
    ebay_client._reset_token_cache()
    yield
    ebay_client._reset_token_cache()


@pytest.fixture(autouse=True)
def _default_settings(monkeypatch):
    monkeypatch.setattr(ebay_client, "get_ebay_settings", _settings)


class _FakeResponse:
    def __init__(self, json_data, status=200, content=b"{}"):
        self._json = json_data
        self.status_code = status
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json


# --- configuration gates ---------------------------------------------------

def test_is_configured_false_without_credentials(monkeypatch):
    monkeypatch.setattr(ebay_client, "get_ebay_settings", lambda: _settings(refresh_token=""))
    assert ebay_client.is_configured() is False


def test_is_configured_true_with_credentials():
    assert ebay_client.is_configured() is True


def test_publishing_prerequisites_lists_every_unset_field():
    # Credentials alone are not enough to publish -- the four listing
    # prerequisites are reported by name so a half-set account gives a
    # precise error rather than an opaque eBay 400.
    assert ebay_client.publishing_prerequisites_missing() == [
        "EBAY_FULFILLMENT_POLICY_ID", "EBAY_MERCHANT_LOCATION_KEY",
        "EBAY_PAYMENT_POLICY_ID", "EBAY_RETURN_POLICY_ID",
    ]


def test_publishing_prerequisites_empty_when_all_set(monkeypatch):
    monkeypatch.setattr(ebay_client, "get_ebay_settings", lambda: _settings(
        merchant_location_key="HOME", fulfillment_policy_id="1",
        payment_policy_id="2", return_policy_id="3"))
    assert ebay_client.publishing_prerequisites_missing() == []


# --- environment routing ---------------------------------------------------

def test_sandbox_is_the_default_environment():
    assert ebay_client.api_base() == "https://api.sandbox.ebay.com"
    assert ebay_client.auth_base() == "https://auth.sandbox.ebay.com"


def test_production_env_switches_both_hosts(monkeypatch):
    monkeypatch.setattr(ebay_client, "get_ebay_settings", lambda: _settings(env="production"))
    assert ebay_client.api_base() == "https://api.ebay.com"
    # The consent page lives on a different host from the API -- a common
    # mix-up, so it is asserted separately.
    assert ebay_client.auth_base() == "https://auth.ebay.com"


def test_consent_url_sends_runame_as_redirect_uri():
    url = ebay_client.consent_url()
    assert url.startswith("https://auth.sandbox.ebay.com/oauth2/authorize?")
    # redirect_uri must be the RuName, not an https:// URL.
    assert "redirect_uri=Shae-Bright-brigh-abcdef" in url
    assert "response_type=code" in url
    assert "sell.inventory" in url


# --- token handling --------------------------------------------------------

def test_access_token_is_cached_across_calls(monkeypatch):
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append(data)
        return _FakeResponse({"access_token": "tok", "expires_in": 7200})

    monkeypatch.setattr(requests, "post", fake_post)
    assert ebay_client._get_access_token() == "tok"
    assert ebay_client._get_access_token() == "tok"
    assert len(calls) == 1
    assert calls[0]["grant_type"] == "refresh_token"


def test_expired_access_token_is_refetched(monkeypatch):
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append(data)
        return _FakeResponse({"access_token": f"tok{len(calls)}", "expires_in": 7200})

    monkeypatch.setattr(requests, "post", fake_post)
    ebay_client._get_access_token()
    ebay_client._token_cache["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert ebay_client._get_access_token() == "tok2"
    assert len(calls) == 2


def test_token_failure_returns_none_not_raises(monkeypatch):
    def fake_post(url, data=None, headers=None, timeout=None):
        return _FakeResponse({"error": "invalid_grant"}, status=400)

    monkeypatch.setattr(requests, "post", fake_post)
    assert ebay_client._get_access_token() is None


def test_request_returns_none_when_token_unavailable(monkeypatch):
    monkeypatch.setattr(ebay_client, "_get_access_token", lambda: None)
    assert ebay_client._request("GET", "/sell/inventory/v1/inventory_item/X") is None


# --- bulk result parsing ---------------------------------------------------

def test_bulk_results_treats_per_item_status_as_the_verdict():
    # The whole point: eBay returns HTTP 200 for a batch containing
    # failures. Only the per-item statusCode says what actually happened.
    data = {"responses": [
        {"statusCode": 200, "sku": "A"},
        {"statusCode": 400, "sku": "B",
         "errors": [{"errorId": 25002, "longMessage": "A user error has occurred."}]},
    ]}
    results = ebay_client._bulk_results(data, ["A", "B"])
    assert [r.ok for r in results] == [True, False]
    assert results[1].status_code == 400
    assert "25002" in results[1].errors[0]


def test_bulk_results_reports_one_failure_per_item_when_call_fails():
    # A dead HTTP call must not silently shrink the batch -- successes plus
    # failures always has to add up to what was submitted.
    results = ebay_client._bulk_results(None, ["A", "B", "C"])
    assert len(results) == 3
    assert all(not r.ok for r in results)
    assert [r.sku for r in results] == ["A", "B", "C"]


def test_bulk_results_handles_unexpected_shape():
    results = ebay_client._bulk_results({"unexpected": 1}, ["A"])
    assert len(results) == 1 and not results[0].ok


def test_bulk_results_captures_publish_ids():
    data = {"responses": [{"statusCode": 200, "offerId": "OFFER1", "listingId": "LISTING1"}]}
    result = ebay_client._bulk_results(data, ["OFFER1"])[0]
    assert result.ok and result.offer_id == "OFFER1" and result.listing_id == "LISTING1"


# --- chunking --------------------------------------------------------------

def test_bulk_calls_are_chunked_at_ebay_limit_of_25(monkeypatch):
    batches = []

    def fake_request(method, path, *, body=None, params=None):
        batches.append(len(body["requests"]))
        return {"responses": [{"statusCode": 200, "sku": r["sku"]} for r in body["requests"]]}

    monkeypatch.setattr(ebay_client, "_request", fake_request)
    items = [{"sku": f"S{i}", "condition": "NEW", "product": {}} for i in range(60)]
    results = ebay_client.bulk_create_or_replace_inventory_items(items)

    assert batches == [25, 25, 10]
    assert len(results) == 60
    assert all(r.ok for r in results)


def test_publish_sends_offer_ids_in_ebay_shape(monkeypatch):
    sent = {}

    def fake_request(method, path, *, body=None, params=None):
        sent["path"] = path
        sent["body"] = body
        return {"responses": [{"statusCode": 200, "offerId": "O1", "listingId": "L1"}]}

    monkeypatch.setattr(ebay_client, "_request", fake_request)
    results = ebay_client.bulk_publish_offers(["O1"])

    assert sent["path"] == "/sell/inventory/v1/bulk_publish_offer"
    assert sent["body"] == {"requests": [{"offerId": "O1"}]}
    assert results[0].listing_id == "L1"


# --- account / taxonomy ----------------------------------------------------

def test_business_policies_read_their_camelcase_array_keys(monkeypatch):
    responses = {
        "/sell/account/v1/fulfillment_policy": {"fulfillmentPolicies": [{"fulfillmentPolicyId": "F1"}]},
        "/sell/account/v1/payment_policy": {"paymentPolicies": [{"paymentPolicyId": "P1"}]},
        "/sell/account/v1/return_policy": {"returnPolicies": [{"returnPolicyId": "R1"}]},
    }
    monkeypatch.setattr(ebay_client, "_request",
                        lambda method, path, **kw: responses.get(path))
    policies = ebay_client.list_business_policies()
    assert policies["fulfillment"][0]["fulfillmentPolicyId"] == "F1"
    assert policies["payment"][0]["paymentPolicyId"] == "P1"
    assert policies["return"][0]["returnPolicyId"] == "R1"


def test_policies_degrade_to_empty_on_failure(monkeypatch):
    monkeypatch.setattr(ebay_client, "_request", lambda method, path, **kw: None)
    assert ebay_client.list_business_policies() == {
        "fulfillment": [], "payment": [], "return": []}


def test_suggest_categories_builds_a_readable_path(monkeypatch):
    def fake_request(method, path, **kw):
        if path.endswith("get_default_category_tree_id"):
            return {"categoryTreeId": "3"}
        return {"categorySuggestions": [{
            "category": {"categoryId": "34248", "categoryName": "Car Service & Repair Manuals"},
            "categoryTreeNodeAncestors": [
                {"categoryName": "Car Manuals & Literature"},
                {"categoryName": "Vehicle Parts & Accessories"},
            ],
        }]}

    monkeypatch.setattr(ebay_client, "_request", fake_request)
    suggestions = ebay_client.suggest_categories("Haynes manual")
    assert suggestions[0]["categoryId"] == "34248"
    # Ancestors come back deepest-first; the path reads root-first.
    assert suggestions[0]["path"] == (
        "Vehicle Parts & Accessories > Car Manuals & Literature > Car Service & Repair Manuals")


def test_suggest_categories_empty_without_tree_id(monkeypatch):
    monkeypatch.setattr(ebay_client, "_request", lambda method, path, **kw: None)
    assert ebay_client.suggest_categories("anything") == []


def test_consent_url_forces_a_fresh_login_by_default():
    # The refresh token binds to whoever clicks Agree, not to the developer
    # account. Without prompt=login an already-signed-in personal account
    # would be consented silently and every listing would go to it.
    assert "prompt=login" in ebay_client.consent_url()


def test_consent_url_force_login_can_be_turned_off():
    assert "prompt=login" not in ebay_client.consent_url(force_login=False)
