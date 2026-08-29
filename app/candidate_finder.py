"""Reverse candidate search -- the inverse of every scraper source here.

Those all ask "here's a supplier item, is it viable on Amazon?", which
means the answer is no for almost everything: across 1,723 NDA Toys items
(2026-08-28) not one reached a PASS, and of the 102 that got as far as
full scoring, *none* failed on profitability -- they failed structurally
(70 had no live Amazon buybox at all, 20 gated, 10 Amazon-on-listing, 2 no
sales velocity). Supplier catalogues are full of products Amazon buyers
aren't buying.

This module asks the opposite question: give me products that ALREADY
satisfy every structural gate in decision/engine.py -- a real buybox,
Amazon not on the listing, FBA competition under the cap, genuine sales
velocity, rank inside the category threshold -- and then compute what
we'd have to source each one at to clear min_roi and min_net_profit. The
output is a shopping list ("find these at or below £X"), which is the one
question a supplier catalogue can actually answer.

Deliberately produces no Deals and no Scores: these are targets to go
sourcing for, not deals that have been found. Nothing here feeds the
pipeline; it feeds a human.

WHY min_buybox_pence MATTERS MORE THAN IT LOOKS: FBA fees don't scale
down with price, so the discount needed to clear both thresholds gets
brutal at the bottom end. Measured against real fee data (2026-08-29):

    £12.30 buybox -> must source at £2.81  (77% off)
    £16.99 buybox -> must source at £7.38  (57% off)
    £21.99 buybox -> must source at £10.80 (51% off)
    £37.99 buybox -> must source at £21.68 (43% off)

Sub-£20 products are where the maths quietly stops working -- and that
band is most of what a generic-novelty wholesale catalogue contains,
which is the real reason those sources produce nothing. The config's
min_buybox_pence exists to stop spending tokens down there.
"""
import re
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from . import keepa_client, models, spapi_client
from .decision.engine import DecisionConfig
from .pricing.fees import FeeProvider, SizeDims

# Sorting the finder by best rank surfaces category megasellers, which in
# practice means renewed Apple hardware dominating the entire list
# (confirmed live 2026-08-29: 15 of the top 15 were renewed iPhones/iPads).
# Those are unusable here regardless of margin -- brand-gated, high capital
# per unit, and condition-specific listing rules -- so they're dropped on
# the title before spending an SP-API gating call on them.
_EXCLUDED_TITLE_RE = re.compile(r"\b(renewed|refurbished|pre-?owned)\b", re.I)

# getListingsRestrictions is rate-limited and spapi_client has no backoff
# of its own (a 429 there just logs and returns None). A daily run over
# ~50 candidates can afford to be polite.
_GATING_DELAY_S = 0.3


@dataclass
class Candidate:
    asin: str
    title: str | None
    buybox_price_pence: int
    target_buy_price_pence: int
    sales_rank: int | None
    fba_offer_count: int
    est_monthly_sales: float | None

    @property
    def discount_required_pct(self) -> float:
        """How far below the Amazon price we'd need to source, as a
        fraction -- the single most useful number for judging whether a
        candidate is realistic before contacting a supplier."""
        return 1 - (self.target_buy_price_pence / self.buybox_price_pence)


def target_buy_price_pence(
    sell_price_pence: int,
    total_fees_pence: int,
    storage_cost_pence: int,
    cfg: DecisionConfig,
) -> int:
    """Inverts decision/engine.py's scoring maths: the highest buy price
    that still clears BOTH min_roi and min_net_profit_pence. Kept as a
    pure function so it can be tested against the engine's own formula
    without any Keepa/DB involvement.

        net_profit = sell - fees - storage - inbound - buy
        roi        = net_profit / buy

    Solving each constraint for buy:
        roi >= min_roi          ->  buy <= headroom / (1 + min_roi)
        net_profit >= min_prof  ->  buy <= headroom - min_prof

    where headroom = sell - fees - storage - inbound. Returns the tighter
    of the two, floored at 0 (a negative result means the product can't
    clear the thresholds at any purchase price, not even free)."""
    headroom = sell_price_pence - total_fees_pence - storage_cost_pence - cfg.inbound_shipping_pence
    max_buy_for_roi = headroom / (1 + cfg.min_roi)
    max_buy_for_profit = headroom - cfg.min_net_profit_pence
    return max(0, int(min(max_buy_for_roi, max_buy_for_profit)))


def _build_finder_params(finder_cfg: dict, cfg: DecisionConfig) -> dict:
    """Maps our config + decision thresholds onto Keepa's ProductParams.
    Each filter here mirrors a specific hard gate in engine.py's
    score_deal, so anything Keepa returns has already cleared it:

      current_SALES_gte=1          -- must HAVE a rank at all. Products with
                                      rank=None are listed-but-dormant and
                                      die on engine.py's velocity floor
                                      (confirmed: every Bullyland item
                                      checked on 2026-08-29 looked like this).
      current_SALES_lte            -- category_rank_thresholds
      buyBoxIsAmazon=False         -- the amazon_on_listing hard-reject
      buyBoxEligibleOfferCountsNewFBA_lte -- thresholds.max_fba_offers
      monthlySold_gte              -- velocity.min_monthly_sales
      current_BUY_BOX_SHIPPING_gte -- min_buybox_pence, see module docstring
      productType=[0]              -- physical goods only
    """
    params = {
        "current_SALES_gte": 1,
        "current_SALES_lte": finder_cfg["max_sales_rank"],
        "buyBoxIsAmazon": False,
        "buyBoxEligibleOfferCountsNewFBA_lte": cfg.max_fba_offers,
        "monthlySold_gte": int(cfg.velocity_min_monthly_sales),
        "current_BUY_BOX_SHIPPING_gte": finder_cfg["min_buybox_pence"],
        "productType": [0],
        "sort": [["current_SALES", "asc"]],
    }
    max_buybox = finder_cfg.get("max_buybox_pence")
    if max_buybox:
        params["current_BUY_BOX_SHIPPING_lte"] = max_buybox
    root_categories = finder_cfg.get("root_categories") or []
    if root_categories:
        params["rootCategory"] = root_categories
    # n_products alone does NOT lift Keepa's 50-per-page default: asking for
    # 150 without this silently returns exactly 50 (confirmed live
    # 2026-08-29). perPage is the real control.
    params["perPage"] = finder_cfg.get("max_results", 50)
    return params


def find_candidates(
    db: Session, app_cfg: dict, cfg: DecisionConfig, fee_provider: FeeProvider
) -> list[Candidate]:
    """Runs the finder query, then a real stage2 lookup on the results so
    fees/storage are computed from each product's actual dimensions and
    competition rather than the finder's coarser filter data. Skips
    anything whose target buy price lands at 0 (can't clear the
    thresholds at any price)."""
    finder_cfg = app_cfg.get("candidate_finder") or {}
    params = _build_finder_params(finder_cfg, cfg)
    asins = keepa_client.find_asins(db, params, finder_cfg.get("max_results", 50))
    if not asins:
        return []

    stage2_by_asin = keepa_client.stage2_full(db, asins)
    check_gating = spapi_client.is_configured()
    candidates = []
    for asin in asins:
        stage2 = stage2_by_asin.get(asin)
        if stage2 is None or not stage2.buybox_price_pence:
            continue
        if stage2.title and _EXCLUDED_TITLE_RE.search(stage2.title):
            continue

        dims = None
        if stage2.package_weight_kg and stage2.package_longest_cm and stage2.package_dims_sum_cm:
            dims = SizeDims(stage2.package_weight_kg, stage2.package_longest_cm, stage2.package_dims_sum_cm)
        if fee_provider.classify_size_tier(dims) == "oversize" and cfg.reject_oversize:
            continue

        fees = fee_provider.get_fees(
            stage2.category or "", stage2.buybox_price_pence, dims,
            stage2.fba_fulfilment_fee_pence, stage2.referral_fee_percentage, asin=asin,
        )
        fee_vat_mult = 1.0 if cfg.vat_registered else 1.20
        total_fees = round((fees.referral_fee_pence + fees.fba_fulfilment_fee_pence) * fee_vat_mult)

        # Same months-to-sell model as engine.py, so the storage cost
        # baked into the target price matches what scoring would charge.
        est_monthly_sales = stage2.est_monthly_sales or 0.0
        our_share = est_monthly_sales / (stage2.fba_offer_count + 1)
        est_months_to_sell = min(max(1.0 / max(our_share, 0.1), 1.0), 6.0)
        storage_cost = round(fees.monthly_storage_fee_pence * est_months_to_sell)

        target = target_buy_price_pence(stage2.buybox_price_pence, total_fees, storage_cost, cfg)
        if target <= 0:
            continue

        # Last, because it's the only check that costs a network call.
        # Only a definite True excludes: check_gating returns None when
        # SP-API is unreachable or the response is unparseable, and
        # treating "unknown" as "gated" would silently empty the list on
        # any SP-API wobble. Same convention as engine.py, which only
        # hard-rejects on gated is True.
        if check_gating:
            time.sleep(_GATING_DELAY_S)
            if spapi_client.check_gating(db, asin) is True:
                continue

        candidates.append(Candidate(
            asin=asin,
            title=stage2.title,
            buybox_price_pence=stage2.buybox_price_pence,
            target_buy_price_pence=target,
            sales_rank=stage2.sales_rank,
            fba_offer_count=stage2.fba_offer_count,
            est_monthly_sales=stage2.est_monthly_sales,
        ))
    return candidates


def filter_unseen(db: Session, candidates: list[Candidate]) -> list[Candidate]:
    """Drops candidates already recorded, so a daily run only reports
    genuinely new finds instead of re-posting the same shopping list.
    Refreshes last_seen/pricing on the ones already known (the target
    price moves with Amazon's buybox) without re-reporting them."""
    unseen = []
    for c in candidates:
        row = db.get(models.CandidateAsin, c.asin)
        if row is None:
            db.add(models.CandidateAsin(
                asin=c.asin,
                title=c.title,
                buybox_price=c.buybox_price_pence,
                target_buy_price=c.target_buy_price_pence,
                sales_rank=c.sales_rank,
                fba_offer_count=c.fba_offer_count,
                est_monthly_sales=c.est_monthly_sales,
            ))
            unseen.append(c)
        else:
            row.title = c.title
            row.buybox_price = c.buybox_price_pence
            row.target_buy_price = c.target_buy_price_pence
            row.sales_rank = c.sales_rank
            row.fba_offer_count = c.fba_offer_count
            row.est_monthly_sales = c.est_monthly_sales
            row.last_seen = models.utcnow()
    db.commit()
    return unseen
