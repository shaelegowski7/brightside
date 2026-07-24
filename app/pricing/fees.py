"""Fee sourcing seam. FeeTableProvider is config-driven (referral % by
category, FBA fee by size tier) — the fallback whenever a real per-ASIN fee
component isn't available. SP-API's getMyFeesEstimate/gating were dropped as
a Phase 2 target (Pro-seller developer registration + ongoing cost, for a
seller account that doesn't have one); instead, get_fees() accepts two
optional per-ASIN Keepa values, each independently preferred over its
config-table guess when present (estimated=True only if either still had to
fall back): keepa_fulfilment_fee_pence (fbaFees.pickAndPackFee, computed from
the catalog's real weight/dims against Amazon's published rate card) and
keepa_referral_pct (referralFeePercentage — also sidesteps the config
table's category-name string matching, which already drifts: see
config.yaml's separate "Home & Kitchen"/"Kitchen & Home" entries). Both
confirmed live 2026-07-23 — see keepa_client.py. Gating remains unchecked;
buyers verify manually in Seller Central before purchasing."""
from abc import ABC, abstractmethod
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.decision.engine import FeeInput

_SIZE_TIER_ORDER = ("small_standard", "standard", "large_standard")


@dataclass
class SizeDims:
    weight_kg: float
    longest_cm: float
    dims_sum_cm: float   # length + width + height


class FeeProvider(ABC):
    @abstractmethod
    def get_fees(
        self, category: str, sell_price_pence: int, dims: SizeDims | None,
        keepa_fulfilment_fee_pence: int | None = None,
        keepa_referral_pct: float | None = None,
        asin: str | None = None,
    ) -> FeeInput:
        ...

    @abstractmethod
    def classify_size_tier(self, dims: SizeDims | None) -> str:
        ...


class FeeTableProvider(FeeProvider):
    """Category referral % + FBA fee by size tier, from config.yaml's `fees`
    section. Always returns FeeInput(estimated=True)."""

    def __init__(self, fees_cfg: dict):
        self._default_referral_pct = fees_cfg["default_referral_pct"]
        self._category_referral_pct = fees_cfg["category_referral_pct"]
        self._fba_fee_by_tier = fees_cfg["fba_fee_by_size_tier_pence"]
        self._storage_fee_by_tier = fees_cfg["monthly_storage_fee_pence"]
        self._size_thresholds = fees_cfg["size_tier_thresholds_cm_kg"]

    def classify_size_tier(self, dims: SizeDims | None) -> str:
        if dims is None:
            # Unknown dimensions: assume standard rather than risk a false
            # "oversize" hard-reject on a perfectly good deal.
            return "standard"
        for tier in _SIZE_TIER_ORDER:
            t = self._size_thresholds[tier]
            if (
                dims.weight_kg <= t["max_weight_kg"]
                and dims.longest_cm <= t["max_longest_cm"]
                and dims.dims_sum_cm <= t["max_dims_sum_cm"]
            ):
                return tier
        return "oversize"

    def get_fees(
        self, category: str, sell_price_pence: int, dims: SizeDims | None,
        keepa_fulfilment_fee_pence: int | None = None,
        keepa_referral_pct: float | None = None,
        asin: str | None = None,   # accepted, ignored -- this provider has no per-ASIN data source
    ) -> FeeInput:
        tier = self.classify_size_tier(dims)
        storage_key = "oversize" if tier == "oversize" else "standard"

        if keepa_referral_pct is not None:
            # Keepa reports this in percentage points (13.0 == 13%), not a
            # 0-1 fraction -- confirmed live 2026-07-23. Same "prefer the
            # real per-ASIN Keepa value over the config-table category-name
            # guess" pattern already used for the FBA fulfilment fee below;
            # also sidesteps the config table's fragile category-name
            # matching (e.g. "Home & Kitchen" vs "Kitchen & Home" drift).
            referral_pct = keepa_referral_pct / 100
            referral_estimated = False
        else:
            referral_pct = self._category_referral_pct.get(category, self._default_referral_pct)
            referral_estimated = True

        if keepa_fulfilment_fee_pence is not None:
            fulfilment_fee_pence = keepa_fulfilment_fee_pence
            fulfilment_estimated = False
        else:
            fulfilment_fee_pence = self._fba_fee_by_tier[tier]
            fulfilment_estimated = True

        return FeeInput(
            referral_fee_pence=round(sell_price_pence * referral_pct),
            fba_fulfilment_fee_pence=fulfilment_fee_pence,
            monthly_storage_fee_pence=self._storage_fee_by_tier[storage_key],
            estimated=referral_estimated or fulfilment_estimated,
        )


class SpApiFeeProvider(FeeProvider):
    """Layers SP-API (Phase 2, dormant) on top of an unchanged fallback --
    itself already layered on Keepa-preferred values, see FeeTableProvider
    above. SP-API is tried first when an asin is given and spapi_client.
    is_configured(); falls through unchanged to today's fallback behavior
    otherwise, so nothing regresses while SP-API stays unconfigured (every
    real deployment today -- see app/spapi_client.py's module docstring).
    monthly_storage_fee_pence always comes from the fallback: SP-API's fee
    estimate doesn't include a storage-fee component, that's a separate
    SP-API surface not in scope here."""

    def __init__(self, db: Session, fallback: FeeProvider):
        self._db = db
        self._fallback = fallback

    def classify_size_tier(self, dims: SizeDims | None) -> str:
        return self._fallback.classify_size_tier(dims)

    def get_fees(
        self, category: str, sell_price_pence: int, dims: SizeDims | None,
        keepa_fulfilment_fee_pence: int | None = None,
        keepa_referral_pct: float | None = None,
        asin: str | None = None,
    ) -> FeeInput:
        from app import spapi_client   # deferred: avoids importing spapi_client at module load for every fee lookup

        fallback_fees = self._fallback.get_fees(category, sell_price_pence, dims, keepa_fulfilment_fee_pence, keepa_referral_pct, asin)
        if not asin:
            return fallback_fees

        result = spapi_client.get_fees_estimate(self._db, asin, sell_price_pence)
        if result is None:
            return fallback_fees

        return FeeInput(
            referral_fee_pence=result.referral_fee_pence,
            fba_fulfilment_fee_pence=result.fba_fulfilment_fee_pence,
            monthly_storage_fee_pence=fallback_fees.monthly_storage_fee_pence,
            estimated=False,
        )


def build_fee_provider(db: Session, app_cfg: dict) -> FeeProvider:
    """Single place the "is SP-API on?" branch lives -- used by both the
    scheduler's poll jobs and /scan, so they can never drift out of sync."""
    from app import spapi_client

    fallback = FeeTableProvider(app_cfg["fees"])
    if spapi_client.is_configured():
        return SpApiFeeProvider(db, fallback)
    return fallback
