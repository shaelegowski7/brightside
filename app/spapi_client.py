"""SP-API client (Phase 2) -- getMyFeesEstimateForASIN (Product Fees API v0)
+ getListingsRestrictions (Listings Restrictions API v2021-08-01). Live since
2026-08-28 (Solution Provider application approved -- Private developer,
Pricing + Product Listing roles only, see SECURITY.md). is_configured() is
still the single on/off switch, and every public function here still returns
None on any failure so a real outage degrades to the config fee-estimate
table / unchecked gating rather than crashing the pipeline.

AUTH MODEL -- verified against Amazon's own SP-API changelog, not assumed:
as of October 2023 SP-API dropped AWS SigV4/IAM signing entirely; every
operation (including these two -- neither returns buyer PII, so no
Restricted Data Token either) needs only an LWA bearer access token in the
`x-amz-access-token` header. If that ever turns out wrong for these specific
operations, calls will fail with an auth error from SP-API itself, caught
and logged here as a None return -- not a crash, not silently wrong data.

FIELD NAMES -- confirmed against real live responses (2026-08-28/29), not
just the OpenAPI model summary:
- getListingsRestrictions: matches what was assumed -- a top-level
  `restrictions[].reasons[].reasonCode`/`links[].resource` object, no
  extra wrapper.
- getMyFeesEstimateForASIN: did NOT match what was assumed. The real
  response wraps the whole result one level deeper than expected --
  `payload.FeesEstimateResult.FeesEstimate.FeeDetailList`, not
  `FeesEstimateResult.FeesEstimate.FeeDetailList`. Everything below that
  (FeeType values, FeeAmount.Amount as a plain float in major currency
  units) was already correct. This missing `payload` level meant every
  real fee lookup since SP-API went live was silently failing to parse
  and falling back to the config estimate table -- fixed once caught by
  manually re-running a real fees call and diffing the actual JSON.
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
        # Confirmed live 2026-08-29: the whole result is wrapped one level
        # deeper than the OpenAPI model summary suggests -- every field
        # inside is otherwise exactly as guessed (FeeType values, Amount as
        # a plain float in major currency units), just missing this "payload"
        # level, which is why every real call was silently falling back to
        # the config fee-estimate table since SP-API went live.
        result = data["payload"]["FeesEstimateResult"]
    except (KeyError, TypeError):
        print(f"[SPAPI] unexpected feesEstimate response shape: {list(data)[:5]}")
        return None

    # Amazon reports per-ASIN failures in-band with a 200: Status is
    # "ClientError"/"ServerError" and FeesEstimate is simply absent (seen
    # live on ~30% of a 50-ASIN sweep, 2026-08-29 -- typically ASINs whose
    # dimensions Amazon can't resolve). That's an expected outcome, not a
    # shape mismatch, so it returns None quietly and the caller falls back
    # to the config fee table rather than logging on every one.
    status = result.get("Status")
    if status != "Success" or "FeesEstimate" not in result:
        return None

    try:
        details = result["FeesEstimate"]["FeeDetailList"]
        by_type = {d["FeeType"]: round(float(d["FeeAmount"]["Amount"]) * 100) for d in details}
    except (KeyError, TypeError, ValueError) as e:
        print(f"[SPAPI] unexpected feesEstimate response shape: {e}")
        return None

    return FeesEstimateResult(
        referral_fee_pence=by_type.get("ReferralFee", 0),
        fba_fulfilment_fee_pence=by_type.get("FBAFees", 0),
    )


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


@dataclass
class GatingResult:
    """`gated` is not a decision on its own -- see reason_code.

    APPROVAL_REQUIRED means there is an application path (Amazon returns the
    Seller Central deep link in approval_url); a qualifying trade invoice
    normally clears it, which is exactly what wholesale sourcing produces.
    NOT_ELIGIBLE means no path exists and the SKU is dead. Collapsing both
    to `gated=True` throws away the difference between "paperwork" and
    "never", so callers making buying decisions should read reason_code.
    """

    gated: bool
    reason_code: str | None      # comma-separated + sorted if an ASIN carries several
    approval_url: str | None


def _parse_gating(data: dict) -> GatingResult | None:
    try:
        restrictions = data["restrictions"]
        reasons = [rn for r in restrictions for rn in (r.get("reasons") or [])]
    except (KeyError, TypeError) as e:
        print(f"[SPAPI] unexpected restrictions response shape: {e}")
        return None

    if not reasons:
        return GatingResult(gated=False, reason_code=None, approval_url=None)

    codes = sorted({rn.get("reasonCode") for rn in reasons if rn.get("reasonCode")})
    url = next(
        (link.get("resource") for rn in reasons for link in (rn.get("links") or [])
         if link.get("resource")),
        None,
    )
    return GatingResult(gated=True, reason_code=",".join(codes) or None, approval_url=url)


def check_gating_detail(db: Session, asin: str) -> GatingResult | None:
    """Spec: "Cache gating results per ASIN for 7 days." Same fail-open
    template as get_fees_estimate.

    A single live read is authoritative but a *failed* one must never be
    written -- a wrong value would then be served from cache for a full
    week, and 7 days is long enough to plan and place a real order against
    it. So a None from the API leaves any existing row untouched.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=_GATING_CACHE_TTL_DAYS)
    cached = db.get(models.GatingCache, asin)

    def _from_cache() -> GatingResult | None:
        if cached is None:
            return None
        return GatingResult(cached.gated, cached.reason_code, cached.approval_url)

    if cached is not None:
        fetched_at = cached.fetched_at if cached.fetched_at.tzinfo else cached.fetched_at.replace(tzinfo=timezone.utc)
        if fetched_at >= cutoff:
            return _from_cache()

    if not is_configured():
        return _from_cache()

    s = get_settings()
    data = _get(
        "/listings/2021-08-01/restrictions",
        {"asin": asin, "sellerId": s.spapi_seller_id, "marketplaceIds": s.spapi_marketplace_id, "conditionType": "new_new"},
    )
    result = _parse_gating(data) if data else None
    if result is None:
        return _from_cache()

    if cached is None:
        cached = models.GatingCache(asin=asin)
        db.add(cached)
    cached.gated = result.gated
    cached.reason_code = result.reason_code
    cached.approval_url = result.approval_url
    cached.fetched_at = models.utcnow()
    db.commit()
    return result


def check_gating(db: Session, asin: str) -> bool | None:
    """Bool-only view of check_gating_detail, for callers that only filter
    on gated/not (pipeline.py's scoring path). Prefer check_gating_detail
    anywhere the APPROVAL_REQUIRED vs NOT_ELIGIBLE distinction matters."""
    result = check_gating_detail(db, asin)
    return result.gated if result else None
