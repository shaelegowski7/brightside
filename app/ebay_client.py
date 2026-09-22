"""eBay Sell API client -- OAuth user tokens + Inventory/Offer/Account.

This is the *selling* side of Brightside. Everything else in this repo
sources deals to buy; this lists what we own onto the Brightside Commerce
eBay shop. Dormant by default in exactly the same shape as spapi_client.py:
is_configured() is the single on/off switch, and every public function
returns None (or an all-failed result) rather than raising, so an eBay
outage can never take down the scanner.

AUTH MODEL -- eBay Sell APIs need a *user* token, not an application token.
Application tokens (client_credentials grant) are only good for public
Buy-side data; anything that touches a seller's own inventory needs the
authorization-code grant, which requires a one-time human consent step in
a browser. tools/ebay_consent.py drives that; it produces the long-lived
refresh token that goes in EBAY_REFRESH_TOKEN. See docs/EBAY_SETUP.md.

  access token   ~2 hours    -- cached in-process here, refreshed on demand
  refresh token  ~18 months  -- stored in env, must be re-consented at expiry
  auth code      5 minutes   -- one-shot, exchanged by tools/ebay_consent.py

VERIFIED 2026-08-31, not assumed. developer.ebay.com 403s every non-browser
client (plain curl included), so these were confirmed against two sources
that are not the blocked host: eBay's docs mirror at www.edp.ebay.com, and
the OpenAPI-generated client at zVPS/ebay-sell-inventory-php-client
(generated from eBay's published Inventory API OAS contract):

- Base URLs: https://api.ebay.com/sell/inventory/v1 (production),
  api.sandbox.ebay.com for sandbox. The token endpoint is on the *api*
  host (/identity/v1/oauth2/token); the consent page is on a different
  host entirely (auth.ebay.com / auth.sandbox.ebay.com).
- Bulk endpoints cap at **25 items per call** -- all three of
  bulk_create_or_replace_inventory_item, bulk_create_offer and
  bulk_publish_offer. 100 manuals is therefore 4 calls per stage.
- Bulk calls report per-item failures **in-band with an HTTP 200**: the
  response is {"responses": [{"statusCode": 200|4xx, "sku"/"offerId": ...,
  "errors": [...]}]}. A 200 from requests does NOT mean 25 items succeeded.
  This is the same trap SP-API's per-ASIN "Status": "ClientError" was, so
  _bulk_results() below always reads the per-item statusCode.
- Amount.value is a **string**, not a number ({"value": "8.99",
  "currency": "GBP"}). Sending a float is rejected.
- bulkCreateOrReplaceInventoryItem requires a **Content-Language** header
  on top of the usual auth/content-type. Omitting it is a 400.
- redirect_uri takes eBay's **RuName**, not an https:// URL. See
  EbaySettings.ru_name.

STILL UNVERIFIED against a live account (no keyset exists yet as of
2026-08-31 -- see docs/EBAY_SETUP.md): the real end-to-end responses. The
tests here are all mocked against the contract shapes above, which is the
same boundary spapi_client.py's tests had -- and note that SP-API's real
response turned out to nest one level deeper than its own published model
summary suggested. Treat the first live run as the real verification, and
diff an actual response before trusting a silent no-op.
"""
import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests

from .config import get_ebay_settings

_TIMEOUT_SECONDS = 30
_BULK_MAX = 25          # eBay's hard cap on all three bulk_* endpoints
_CONTENT_LANGUAGE = "en-GB"

# The scopes the listing flow actually needs, and no more: sell.inventory
# to create/publish, sell.account to read the business policy IDs. Keep
# this in step with the consent URL -- a refresh token only carries the
# scopes that were consented to, so adding a scope here without re-running
# tools/ebay_consent.py silently 403s at call time rather than at startup.
SCOPES = (
    # Base scope -- needed by the Taxonomy API (category lookup), which is
    # not a sell.* endpoint and 403s without it.
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
)

_token_cache: dict = {"access_token": None, "expires_at": None}


def _is_production() -> bool:
    return get_ebay_settings().env.strip().lower() in ("production", "prod")


def api_base() -> str:
    return "https://api.ebay.com" if _is_production() else "https://api.sandbox.ebay.com"


def auth_base() -> str:
    return "https://auth.ebay.com" if _is_production() else "https://auth.sandbox.ebay.com"


def is_configured() -> bool:
    """Credentials only -- enough to *call* eBay. Publishing an offer needs
    more than this (a location key and three policy IDs); that is checked
    separately by publishing_prerequisites_missing() so a half-configured
    account gives a precise error instead of an opaque eBay 400."""
    s = get_ebay_settings()
    return bool(s.client_id and s.client_secret and s.refresh_token)


def publishing_prerequisites_missing() -> list[str]:
    """Names of the settings that must be filled before an offer can be
    published. Empty list means ready. See docs/EBAY_SETUP.md step 6."""
    s = get_ebay_settings()
    required = {
        "EBAY_MERCHANT_LOCATION_KEY": s.merchant_location_key,
        "EBAY_FULFILLMENT_POLICY_ID": s.fulfillment_policy_id,
        "EBAY_PAYMENT_POLICY_ID": s.payment_policy_id,
        "EBAY_RETURN_POLICY_ID": s.return_policy_id,
    }
    return sorted(name for name, value in required.items() if not value)


def _basic_auth_header() -> str:
    s = get_ebay_settings()
    raw = f"{s.client_id}:{s.client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


# --------------------------------------------------------------------------
# OAuth
# --------------------------------------------------------------------------

def consent_url(scopes: tuple[str, ...] = SCOPES, force_login: bool = True) -> str:
    """The browser URL a human opens once to grant this app access to their
    eBay seller account. redirect_uri is the RuName, not a URL.

    force_login sends `prompt=login`, which eBay documents as forcing a
    fresh sign-in rather than silently reusing whatever eBay session the
    browser already holds. That default is deliberate and load-bearing:
    the refresh token binds to *whoever clicks Agree*, who need not be the
    developer-account holder. With a personal eBay account already signed
    in, the consent page would otherwise mint a token for that account and
    every listing would land on the wrong shop -- with no error anywhere,
    because from eBay's side nothing went wrong."""
    s = get_ebay_settings()
    params = {
        "client_id": s.client_id,
        "redirect_uri": s.ru_name,
        "response_type": "code",
        "scope": " ".join(scopes),
    }
    if force_login:
        params["prompt"] = "login"
    return f"{auth_base()}/oauth2/authorize?{urlencode(params)}"


def _token_request(payload: dict) -> dict | None:
    try:
        resp = requests.post(
            f"{api_base()}/identity/v1/oauth2/token",
            data=payload,
            headers={
                "Authorization": _basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        # eBay puts the actual reason in the body, not the status line --
        # surface it, since "invalid_grant" (dead refresh token) vs
        # "invalid_client" (wrong keyset) is the whole diagnosis.
        body = getattr(getattr(e, "response", None), "text", "")
        print(f"[EBAY] token request failed: {e} {body[:300]}")
        return None


def exchange_code_for_tokens(code: str) -> dict | None:
    """One-time: swap the 5-minute authorization code from the consent
    redirect for an access token + an ~18-month refresh token. Driven by
    tools/ebay_consent.py, never by the running app."""
    s = get_ebay_settings()
    return _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": s.ru_name,
    })


def _get_access_token() -> str | None:
    """In-memory cache with a 60s safety margin before real expiry, so a
    call started just before expiry doesn't get a token that dies
    mid-request. Same template as spapi_client._get_access_token."""
    now = datetime.now(timezone.utc)
    if _token_cache["access_token"] and _token_cache["expires_at"] and now < _token_cache["expires_at"]:
        return _token_cache["access_token"]

    s = get_ebay_settings()
    data = _token_request({
        "grant_type": "refresh_token",
        "refresh_token": s.refresh_token,
        "scope": " ".join(SCOPES),
    })
    if not data:
        return None
    token = data.get("access_token")
    if not token:
        print(f"[EBAY] unexpected token response shape: {list(data)[:5]}")
        return None

    _token_cache["access_token"] = token
    expires_in = data.get("expires_in", 7200)
    _token_cache["expires_at"] = now + timedelta(seconds=max(expires_in - 60, 60))
    return token


def _reset_token_cache() -> None:
    _token_cache["access_token"] = None
    _token_cache["expires_at"] = None


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _request(method: str, path: str, *, body: dict | None = None,
             params: dict | None = None) -> dict | None:
    token = _get_access_token()
    if token is None:
        return None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        # Required by bulkCreateOrReplaceInventoryItem; harmless elsewhere.
        "Content-Language": _CONTENT_LANGUAGE,
        "Accept-Language": _CONTENT_LANGUAGE,
    }
    try:
        resp = requests.request(
            method, f"{api_base()}{path}",
            json=body, params=params, headers=headers, timeout=_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        # Several Inventory endpoints answer 204 No Content on success.
        if not resp.content:
            return {}
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        body_text = getattr(getattr(e, "response", None), "text", "")
        print(f"[EBAY] {method} {path} failed: {e} {body_text[:500]}")
        return None


# --------------------------------------------------------------------------
# Bulk result parsing
# --------------------------------------------------------------------------

@dataclass
class BulkItemResult:
    """One entry from a bulk response's per-item `responses` array."""
    ok: bool
    status_code: int
    sku: str | None = None
    offer_id: str | None = None
    listing_id: str | None = None
    errors: list[str] = field(default_factory=list)


def _error_messages(entry: dict) -> list[str]:
    out = []
    for key in ("errors", "warnings"):
        for err in entry.get(key) or []:
            if not isinstance(err, dict):
                continue
            msg = err.get("longMessage") or err.get("message") or ""
            code = err.get("errorId")
            label = key[:-1]
            out.append(f"[{label} {code}] {msg}".strip() if code else f"[{label}] {msg}".strip())
    return out


def _bulk_results(data: dict | None, fallback_ids: list[str]) -> list[BulkItemResult]:
    """Turns a bulk response into one result per submitted item.

    A None `data` means the whole HTTP call failed -- reported as a
    synthetic failure per submitted item rather than an empty list, so the
    caller's success + failure counts always add up to what it sent.

    Per-item statusCode is the real verdict: eBay returns HTTP 200 for a
    batch in which individual items failed."""
    if data is None:
        return [BulkItemResult(ok=False, status_code=0, sku=ident, errors=["request failed"])
                for ident in fallback_ids]

    responses = data.get("responses")
    if not isinstance(responses, list):
        print(f"[EBAY] unexpected bulk response shape: {list(data)[:5]}")
        return [BulkItemResult(ok=False, status_code=0, sku=ident,
                               errors=["unexpected response shape"])
                for ident in fallback_ids]

    results = []
    for entry in responses:
        status = entry.get("statusCode", 0)
        results.append(BulkItemResult(
            ok=200 <= status < 300,
            status_code=status,
            sku=entry.get("sku"),
            offer_id=entry.get("offerId"),
            listing_id=entry.get("listingId"),
            errors=_error_messages(entry),
        ))
    return results


def _chunks(items: list, size: int = _BULK_MAX):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# --------------------------------------------------------------------------
# Inventory API
# --------------------------------------------------------------------------

def bulk_create_or_replace_inventory_items(items: list[dict]) -> list[BulkItemResult]:
    """items: [{"sku": ..., "inventoryItem": {...}}]. Chunked at 25."""
    results: list[BulkItemResult] = []
    for chunk in _chunks(items):
        data = _request("POST", "/sell/inventory/v1/bulk_create_or_replace_inventory_item",
                        body={"requests": chunk})
        results.extend(_bulk_results(data, [i.get("sku", "") for i in chunk]))
    return results


def bulk_create_offers(offers: list[dict]) -> list[BulkItemResult]:
    """offers: full EbayOfferDetailsWithKeys dicts. Chunked at 25."""
    results: list[BulkItemResult] = []
    for chunk in _chunks(offers):
        data = _request("POST", "/sell/inventory/v1/bulk_create_offer",
                        body={"requests": chunk})
        results.extend(_bulk_results(data, [o.get("sku", "") for o in chunk]))
    return results


def bulk_publish_offers(offer_ids: list[str]) -> list[BulkItemResult]:
    """Turns unpublished offers into live listings. Chunked at 25."""
    results: list[BulkItemResult] = []
    for chunk in _chunks(offer_ids):
        data = _request("POST", "/sell/inventory/v1/bulk_publish_offer",
                        body={"requests": [{"offerId": oid} for oid in chunk]})
        # Publish responses key on offerId, not sku -- pass the ids through
        # as fallback identifiers so a total failure is still traceable.
        results.extend(_bulk_results(data, chunk))
    return results


def get_inventory_item(sku: str) -> dict | None:
    return _request("GET", f"/sell/inventory/v1/inventory_item/{sku}")


# --------------------------------------------------------------------------
# Account API -- business policies and inventory location
# --------------------------------------------------------------------------

_POLICY_ARRAY_KEYS = {
    "fulfillment_policy": "fulfillmentPolicies",
    "payment_policy": "paymentPolicies",
    "return_policy": "returnPolicies",
}


def _policy_options(kind: str) -> list[dict]:
    """kind: 'fulfillment_policy' | 'payment_policy' | 'return_policy'."""
    s = get_ebay_settings()
    data = _request("GET", f"/sell/account/v1/{kind}",
                    params={"marketplace_id": s.marketplace_id})
    if not data:
        return []
    return data.get(_POLICY_ARRAY_KEYS[kind]) or []


def list_business_policies() -> dict[str, list[dict]]:
    """Everything needed to fill in the three *_POLICY_ID settings, in one
    place, so docs/EBAY_SETUP.md step 6 is a single command."""
    return {
        "fulfillment": _policy_options("fulfillment_policy"),
        "payment": _policy_options("payment_policy"),
        "return": _policy_options("return_policy"),
    }


def list_inventory_locations() -> list[dict]:
    data = _request("GET", "/sell/inventory/v1/location", params={"limit": 100})
    if not data:
        return []
    return data.get("locations") or []


def create_inventory_location(key: str, postal_code: str, country: str = "GB",
                              name: str | None = None) -> bool:
    """A merchant location is mandatory before any offer can be published.
    For a home/office seller a postcode-only address is accepted."""
    ok = _request("POST", f"/sell/inventory/v1/location/{key}", body={
        "location": {"address": {"postalCode": postal_code, "country": country}},
        "name": name or key,
        "merchantLocationStatus": "ENABLED",
        "locationTypes": ["STORE"],
    })
    return ok is not None


# --------------------------------------------------------------------------
# Taxonomy API -- finding the categoryId an offer needs
# --------------------------------------------------------------------------

def default_category_tree_id() -> str | None:
    """Each marketplace has its own category tree; EBAY_GB's id is stable
    but is looked up rather than hardcoded, since a wrong tree id yields
    plausible-looking suggestions from the wrong marketplace."""
    s = get_ebay_settings()
    data = _request("GET", "/commerce/taxonomy/v1/get_default_category_tree_id",
                    params={"marketplace_id": s.marketplace_id})
    if not data:
        return None
    return data.get("categoryTreeId")


def suggest_categories(query: str) -> list[dict]:
    """eBay's own suggestion for where a product belongs. Returns
    [{"categoryId": ..., "categoryName": ..., "path": ...}] best-first.

    Worth using rather than guessing: 'Haynes manual' lands in a Vehicle
    Parts sub-category, not Books, and publishing into the wrong one is a
    silent policy problem rather than an API error."""
    tree_id = default_category_tree_id()
    if not tree_id:
        return []
    data = _request("GET", f"/commerce/taxonomy/v1/category_tree/{tree_id}/get_category_suggestions",
                    params={"q": query})
    if not data:
        return []
    out = []
    for suggestion in data.get("categorySuggestions") or []:
        category = suggestion.get("category") or {}
        ancestors = suggestion.get("categoryTreeNodeAncestors") or []
        path = " > ".join(
            a.get("categoryName", "") for a in reversed(ancestors) if a.get("categoryName")
        )
        out.append({
            "categoryId": category.get("categoryId"),
            "categoryName": category.get("categoryName"),
            "path": f"{path} > {category.get('categoryName', '')}".strip(" >"),
        })
    return out
