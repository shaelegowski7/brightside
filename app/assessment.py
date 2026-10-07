"""Score one Keepa result the way every ASIN-first tool needs: the engine's
verdict, its cost breakdown, and the highest buy price that still clears the
thresholds. Shared by tools/scan_asin_list.py and the sourcing lookup."""
import time
from dataclasses import dataclass, replace

from sqlalchemy.orm import Session

from . import candidate_finder, keepa_client, spapi_client
from .decision.engine import DecisionConfig, ScoreResult, cost_breakdown, resolve_sell_price, score_deal
from .pricing.fees import FeeProvider, SizeDims

GATING_DELAY_S = 0.3   # getListingsRestrictions is rate-limited and spapi_client has no backoff


@dataclass
class Assessment:
    result: ScoreResult
    costs: dict
    sell_price_pence: int
    target_buy_price_pence: int   # 0 = can't clear the thresholds at any price
    category_rank_percentile: float | None
    gating_status: str | None


def assess(db: Session, stage2: "keepa_client.Stage2Result", cfg: DecisionConfig, fee_provider: FeeProvider,
           buy_price_pence: int | None = None, check_gating: bool = False) -> Assessment:
    """With no buy price, scores at the target price so the verdict reflects
    every structural rule at the most you could pay."""
    dims = None
    if stage2.package_weight_kg and stage2.package_longest_cm and stage2.package_dims_sum_cm:
        dims = SizeDims(stage2.package_weight_kg, stage2.package_longest_cm, stage2.package_dims_sum_cm)
    sell_price, _ = resolve_sell_price(
        stage2.buybox_price_pence, stage2.buybox_avg_90d_pence, stage2.lowest_fba_offer_pence)
    sell_price = sell_price or 0
    fees = fee_provider.get_fees(
        stage2.category or "", sell_price, dims,
        stage2.fba_fulfilment_fee_pence, stage2.referral_fee_percentage, asin=stage2.asin,
    )
    oversize = fee_provider.classify_size_tier(dims) == "oversize"
    category_rank_percentile, leaf_size = keepa_client.leaf_rank_stats(db, stage2)

    gating_status = None
    if check_gating:
        time.sleep(GATING_DELAY_S)
        gating_status = spapi_client.gating_status(spapi_client.check_gating_detail(db, stage2.asin))

    inp = keepa_client.score_input_from_stage2(
        stage2, buy_price_pence=buy_price_pence or 1, fees=fees, oversize=oversize,
        category_rank_percentile=category_rank_percentile, leaf_category_size=leaf_size,
        gating_status=gating_status,
    )
    costs = cost_breakdown(inp, sell_price, cfg)
    target = candidate_finder.target_buy_price_pence(sell_price, costs["non_buy_costs_pence"], cfg)
    if buy_price_pence is None:
        inp = replace(inp, buy_price_pence=max(target, 1))
    return Assessment(score_deal(inp, cfg), costs, sell_price, target, category_rank_percentile, gating_status)
