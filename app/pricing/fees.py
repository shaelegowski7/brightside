"""Fee sourcing seam. Phase 1 only has FeeTableProvider (config-driven
estimate — every result comes back with `estimated=True`). Phase 2 adds an
SPAPIFeeProvider backed by SP-API's getMyFeesEstimate, implementing the same
FeeProvider interface, for exact fees — not implemented here."""
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.decision.engine import FeeInput

_SIZE_TIER_ORDER = ("small_standard", "standard", "large_standard")


@dataclass
class SizeDims:
    weight_kg: float
    longest_cm: float
    dims_sum_cm: float   # length + width + height


class FeeProvider(ABC):
    @abstractmethod
    def get_fees(self, category: str, sell_price_pence: int, dims: SizeDims | None) -> FeeInput:
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

    def get_fees(self, category: str, sell_price_pence: int, dims: SizeDims | None) -> FeeInput:
        referral_pct = self._category_referral_pct.get(category, self._default_referral_pct)
        tier = self.classify_size_tier(dims)
        storage_key = "oversize" if tier == "oversize" else "standard"
        return FeeInput(
            referral_fee_pence=round(sell_price_pence * referral_pct),
            fba_fulfilment_fee_pence=self._fba_fee_by_tier[tier],
            monthly_storage_fee_pence=self._storage_fee_by_tier[storage_key],
            estimated=True,
        )
