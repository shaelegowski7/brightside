"""Dormant SP-API client (Phase 2) -- getMyFeesEstimateForASIN (Product Fees
API v0) + getListingsRestrictions (Listings Restrictions API v2021-08-01).
No live SP-API credentials were available when this was written (see
app/pricing/fees.py's module docstring: SP-API was previously dropped
entirely because there's no Pro-seller developer account to register it
against) -- every public function here returns None on any failure or when
unconfigured, so this module has zero effect on current behavior until real
credentials exist. is_configured() is the single on/off switch.

AUTH MODEL -- verified against Amazon's own SP-API changelog, not assumed:
as of October 2023 SP-API dropped AWS SigV4/IAM signing entirely; every
operation (including these two -- neither returns buyer PII, so no
Restricted Data Token either) needs only an LWA bearer access token in the
`x-amz-access-token` header. If that ever turns out wrong for these specific
operations, calls will fail with an auth error from SP-API itself, caught
and logged here as a None return -- not a crash, not silently wrong data.

FIELD NAMES -- UNVERIFIED. The request/response JSON shapes below (
FeesEstimateRequest/FeesEstimateResult/FeeDetailList, restrictions[].reasons)
are my best-effort recollection of the published OpenAPI models, matching
the same "confirm on first live call" caveat keepa_client.py already uses
for its own pre-verification field mappings (see its module docstring).
Confirm against a real response before trusting the parsed values.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy.orm import Session

from . import models
from .config import get_settings

_LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
_SPAPI_BASE_URL = "https://sellingpartnerapi-eu.amazon.com"   # EU endpoint -- UK marketplace
_TIMEOUT_SECONDS = 15
_FEE_CACHE_TTL_HOURS = 24
_GATING_CACHE_TTL_DAYS = 7
_PRICE_BAND_PENCE = 100   # bucket to nearest £1 so near-identical prices reuse a cache hit

_token_cache: dict = {"access_token": None, "expires_at": None}


def is_configured() -> bool:
    s = get_settings()
    return bool(
        s.spapi_client_id and s.spapi_client_secret and s.spapi_refresh_token
        and s.spapi_seller_id and s.spapi_marketplace_id
    )


def _price_band(sell_price_pence: int) -> int:
    return round(sell_price_pence / _PRICE_BAND_PENCE) * _PRICE_BAND_PENCE


def _get_access_token() -> str | None:
    """In-memory cache with a 60s safety margin before the token's real
    expiry, so a call started just before expiry doesn't get a token that
    dies mid-request."""
    now = datetime.now(timezone.utc)
    if _token_cache["access_token"] and _token_cache["expires_at"] and now < _token_cache["expires_at"]:
        return _token_cache["access_token"]

    s = get_settings()
    try:
        resp = requests.post(
            _LWA_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": s.spapi_refresh_token,
                "client_id": s.spapi_client_id,
                "client_secret": s.spapi_client_secret,
            },
            timeout=_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data["access_token"]
        expires_in = data.get("expires_in", 3600)
    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"[SPAPI] LWA token refresh failed: {e}")
        return None

    _token_cache["access_token"] = token
    _token_cache["expires_at"] = now + timedelta(seconds=max(expires_in - 60, 60))
    return token


def _get(path: str, params: dict) -> dict | None:
    token = _get_access_token()
    if token is None:
        return None
    try:
        resp = requests.get(
            f"{_SPAPI_BASE_URL}{path}", params=params,
            headers={"x-amz-access-token": token}, timeout=_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[SPAPI] GET {path} failed: {e}")
        return None


def _post(path: str, body: dict) -> dict | None:
    token = _get_access_token()
    if token is None:
        return None
    try:
        resp = requests.post(
            f"{_SPAPI_BASE_URL}{path}", json=body,
            headers={"x-amz-access-token": token}, timeout=_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[SPAPI] POST {path} failed: {e}")
        return None


@dataclass
class FeesEstimateResult:
    referral_fee_pence: int
    fba_fulfilment_fee_pence: int


def _parse_fees_estimate(data: dict) -> FeesEstimateResult | None:
    try:
        details = data["FeesEstimateResult"]["FeesEstimate"]["FeeDetailList"]
        by_type = {d["FeeType"]: round(float(d["FeeAmount"]["Amount"]) * 100) for d in details}
        return FeesEstimateResult(
            referral_fee_pence=by_type.get("ReferralFee", 0),
            fba_fulfilment_fee_pence=by_type.get("FBAFees", 0),
        )
    except (KeyError, TypeError, ValueError) as e:
        print(f"[SPAPI] unexpected feesEstimate response shape: {e}")
        return None


def get_fees_estimate(db: Session, asin: str, sell_price_pence: int) -> FeesEstimateResult | None:
    """Spec: "Cache fee estimates per (ASIN, price-band) for 24h." Follows
    keepa_client.get_category_size's exact template: check fresh cache,
    else fetch+store, fail open to a stale cached value if the live fetch
    errors (better than nothing when SP-API is briefly unavailable)."""
    band = _price_band(sell_price_pence)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_FEE_CACHE_TTL_HOURS)
    cached = db.get(models.FeeEstimateCache, (asin, band))
    if cached is not None:
        fetched_at = cached.fetched_at if cached.fetched_at.tzinfo else cached.fetched_at.replace(tzinfo=timezone.utc)
        if fetched_at >= cutoff:
            return FeesEstimateResult(cached.referral_fee_pence, cached.fba_fulfilment_fee_pence)

    if not is_configured():
        return FeesEstimateResult(cached.referral_fee_pence, cached.fba_fulfilment_fee_pence) if cached else None

    s = get_settings()
    data = _post(
        f"/products/fees/v0/items/{asin}/feesEstimate",
        {
            "FeesEstimateRequest": {
                "MarketplaceId": s.spapi_marketplace_id,
                "IsAmazonFulfilled": True,
                "PriceToEstimateFees": {
                    "ListingPrice": {"CurrencyCode": "GBP", "Amount": band / 100},
                },
                "Identifier": f"{asin}-{band}",
            }
        },
    )
    result = _parse_fees_estimate(data) if data else None
    if result is None:
        return FeesEstimateResult(cached.referral_fee_pence, cached.fba_fulfilment_fee_pence) if cached else None

    if cached is None:
        cached = models.FeeEstimateCache(asin=asin, price_band_pence=band)
        db.add(cached)
    cached.referral_fee_pence = result.referral_fee_pence
    cached.fba_fulfilment_fee_pence = result.fba_fulfilment_fee_pence
    cached.fetched_at = models.utcnow()
    db.commit()
    return result


def _parse_gating(data: dict) -> bool | None:
    try:
        restrictions = data["restrictions"]
        return any(r.get("reasons") for r in restrictions)
    except (KeyError, TypeError) as e:
        print(f"[SPAPI] unexpected restrictions response shape: {e}")
        return None


def check_gating(db: Session, asin: str) -> bool | None:
    """Spec: "Cache gating results per ASIN for 7 days." Same fail-open
    template as get_fees_estimate."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=_GATING_CACHE_TTL_DAYS)
    cached = db.get(models.GatingCache, asin)
    if cached is not None:
        fetched_at = cached.fetched_at if cached.fetched_at.tzinfo else cached.fetched_at.replace(tzinfo=timezone.utc)
        if fetched_at >= cutoff:
            return cached.gated

    if not is_configured():
        return cached.gated if cached else None

    s = get_settings()
    data = _get(
        "/listings/2021-08-01/restrictions",
        {"asin": asin, "sellerId": s.spapi_seller_id, "marketplaceIds": s.spapi_marketplace_id, "conditionType": "new_new"},
    )
    gated = _parse_gating(data) if data else None
    if gated is None:
        return cached.gated if cached else None

    if cached is None:
        cached = models.GatingCache(asin=asin)
        db.add(cached)
    cached.gated = gated
    cached.fetched_at = models.utcnow()
    db.commit()
    return gated
