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
    # None when open (or unchecked). "APPROVAL_REQUIRED" means listable once
    # a qualifying trade invoice is submitted -- worth sourcing for, but not
    # worth sourcing for FIRST, so it is carried through to the report
    # rather than silently dropped. NOT_ELIGIBLE never reaches here.
    gating: str | None = None

    def months_to_clear(self, units: int) -> float | None:
        """How long an order of `units` takes to sell through, assuming the
        buy box splits evenly between us and the existing FBA sellers.

        The number that actually decides a wholesale order: velocity alone
        is misleading because it ignores who you're splitting it with. Same
        share model as engine.py's est_months_to_sell, but uncapped -- the
        engine clamps to 6 months to bound its storage-cost estimate, which
        is exactly the information you want to see here.
        """
        if not self.est_monthly_sales:
            return None
        return units * (self.fba_offer_count + 1) / self.est_monthly_sales

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
      buyBoxIsAmazon=False         -- a cheap first pass at the
                                      amazon_on_listing hard-reject, NOT
                                      equivalent to it: buyBoxIsAmazon is
                                      only the instantaneous buy-box winner
                                      and understates real Amazon competition
                                      the same way keepa_client.py's module
                                      docstring already flags for stage1/2 --
                                      Amazon can hold a live offer without
                                      currently owning the box. Real
                                      enforcement is below in find_candidates,
                                      against stage2's amazon_on_listing
                                      (confirmed live 2026-08-31: 19 of the
                                      top 30 lowest-discount candidates from
                                      the 2026-08-29 run had buyBoxIsAmazon
                                      False at finder time but amazon_on_listing
                                      True on refresh -- score_deal would have
                                      hard-rejected every one of them).
      buyBoxEligibleOfferCountsNewFBA_lte -- thresholds.max_fba_offers
      buyBoxEligibleOfferCountsNewFBA_gte -- candidate_finder.min_fba_offers,
                                      NOT a decision-engine gate -- see
                                      config.yaml's comment on min_fba_offers.
                                      A sole-source (1-offer) ASIN is exactly
                                      the case with no provable supply chain
                                      to join, so the finder is deliberately
                                      stricter here than engine.py's own
                                      max_fba_offers ceiling.
      monthlySold_gte              -- velocity.min_monthly_sales
      current_BUY_BOX_SHIPPING_gte -- min_buybox_pence, see module docstring
      productType=[0]              -- physical goods only

    Filters are free. Keepa charges per product returned, not per criterion,
    so every gate pushed into the query is one we don't spend tokens
    discovering afterwards. Four more, added 2026-09-06:

      buyBoxStatsAmazon90_lte -- percent of the last 90 days Amazon held the
                              buy box. buyBoxIsAmazon above is a snapshot and
                              misses Amazon sharing a listing; this asks the
                              same question historically, and is what would
                              have caught the 19-of-30 false positives up
                              front rather than on a refresh pass. It does
                              NOT replace the stage2 amazon_on_listing check
                              in find_candidates, which stays.
      packageQuantity_lte  -- units in the manufacturer's package. A
                              multipack ASIN needs N supplier units per sale,
                              so a target price computed per unit is wrong by
                              a factor of N -- the error that put phantom
                              300-1500% ROIs on the Pharmazon and Novanex
                              reports (see wholesale_scan._bundle_units).
                              Restricting to singles keeps the shopping list
                              one-to-one with what a supplier actually sells.
      returnRate_lte       -- high-return SKUs eat margin invisibly: the
                              returned unit is often unsellable and the FBA
                              fee is already spent.
      buyBoxStatsSellerCount365_gte
                           -- distinct sellers who held the buy box over the
                              last year, used as a SOURCEABILITY proxy. The
                              finder's real failure mode is not gating, it is
                              returning structurally perfect products nobody
                              can buy: the 2026-09-06 run came back mostly
                              Chinese private label (TESSAN, COOLJOYA,
                              Dinosoo, Enzeno...) with no UK trade route at
                              all. min_fba_offers only counts sellers
                              competing *today*, which a brand owner plus two
                              of its own accounts satisfies. A product many
                              different sellers have won the box on over a
                              year is, by definition, one multiple resellers
                              can buy -- i.e. it has a distribution channel
                              to join. That is the question a sourcing
                              shortlist actually needs answered.
      page                 -- pagination. Without it the finder re-reads the
                              same best-ranked window every run -- 2
                              CandidateAsin rows exist after a fortnight of
                              daily runs, which is a query problem, not a
                              market one. The sort is stable, so page N is
                              the same window run to run and filter_unseen
                              stays meaningful.

    Every one of these is optional: leave the config key unset and the
    filter is omitted entirely rather than sent with a default, since a
    wrong bound silently shrinks the result set with no error.
    """
    params = {
        "current_SALES_gte": 1,
        "current_SALES_lte": finder_cfg["max_sales_rank"],
        "buyBoxIsAmazon": False,
        "buyBoxEligibleOfferCountsNewFBA_lte": cfg.max_fba_offers,
        "buyBoxEligibleOfferCountsNewFBA_gte": finder_cfg.get("min_fba_offers", 1),
        # Finder-specific floor, deliberately higher than the engine's
        # velocity gate: engine.py asks "does this sell at all", the finder
        # asks "can we clear a wholesale order of it". Buying 100 units of
        # something selling 50/mo against 2 competitors is 6 months of
        # capital and storage; at 200/mo the worst case on the 2026-09-06
        # list is 2.5 months. Falls back to the engine floor if unset.
        #
        # Note Keepa BUCKETS monthlySold and its lowest bucket is 50 -- the
        # previous value of 10 was never really asking for 10, since nothing
        # below 50 exists in the data. 200 is the first threshold that
        # actually filters anything (36 of 52 survive).
        "monthlySold_gte": int(finder_cfg.get("min_monthly_sold")
                               or cfg.velocity_min_monthly_sales),
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

    # All optional -- an unset key means "don't send this filter", not
    # "send a default". A bound we invented would quietly shrink the result
    # set with no error to notice.
    amazon_bb_pct = finder_cfg.get("max_amazon_buybox_pct_90d")
    if amazon_bb_pct is not None:
        params["buyBoxStatsAmazon90_lte"] = amazon_bb_pct
    max_pack_qty = finder_cfg.get("max_package_quantity")
    if max_pack_qty is not None:
        params["packageQuantity_lte"] = max_pack_qty
    # returnRate is an enum band list (list[int]), NOT a range -- there is no
    # returnRate_lte, and passing one is rejected outright by the client's
    # own validation. Passed straight through so the meaning of the bands
    # lives in config next to the value, rather than being guessed here.
    return_rate_bands = finder_cfg.get("return_rate_bands") or []
    if return_rate_bands:
        params["returnRate"] = list(return_rate_bands)
    min_sellers_365 = finder_cfg.get("min_distinct_buybox_sellers_365")
    if min_sellers_365 is not None:
        params["buyBoxStatsSellerCount365_gte"] = min_sellers_365
    page = finder_cfg.get("page")
    if page:
        params["page"] = page
    # n_products alone does NOT lift Keepa's 50-per-page default: asking for
    # 150 without this silently returns exactly 50 (confirmed live
    # 2026-08-29). perPage is the real control.
    #
    # Floored at 50 because Keepa REJECTS anything smaller outright --
    # perPage=15 comes back REQUEST_REJECTED while every other parameter in
    # the same query is accepted (isolated live, 2026-08-29). Without this
    # floor, lowering max_results below 50 to save tokens would silently
    # break the whole job rather than shrinking it.
    params["perPage"] = max(50, finder_cfg.get("max_results", 50))
    return params


def discover_asins(db: Session, app_cfg: dict, cfg: DecisionConfig, page: int | None = None) -> list[str]:
    """Just the Keepa Product Finder call -- which ASINs match the filters.

    Split out from scoring because the two cost wildly different amounts:
    measured live 2026-09-06, a finder page of 50 ASINs costs ~11 tokens
    while stage2 on those same 50 costs ~300, i.e. discovery is ~27x
    cheaper per product. That makes it worth enumerating the whole matching
    set exhaustively (399 ASINs over 8 pages, 88 tokens total) before
    spending anything on scoring, instead of discovering and scoring 50 at
    a time and never learning how big the pool actually is.
    """
    finder_cfg = dict(app_cfg.get("candidate_finder") or {})
    if page is not None:
        finder_cfg["page"] = page
    params = _build_finder_params(finder_cfg, cfg)
    return keepa_client.find_asins(db, params, finder_cfg.get("max_results", 50))


def score_asins(
    db: Session, asins: list[str], app_cfg: dict, cfg: DecisionConfig, fee_provider: FeeProvider
) -> list[Candidate]:
    """Real stage2 lookup on `asins`, so fees/storage come from each
    product's actual dimensions and competition rather than the finder's
    coarser filter data. Skips anything whose target buy price lands at 0
    (can't clear the thresholds at any price)."""
    finder_cfg = app_cfg.get("candidate_finder") or {}
    if not asins:
        return []

    stage2_by_asin = keepa_client.stage2_full(db, asins)
    check_gating = spapi_client.is_configured()
    candidates = []
    for asin in asins:
        stage2 = stage2_by_asin.get(asin)
        if stage2 is None or not stage2.buybox_price_pence:
            continue
        if stage2.amazon_on_listing:
            continue
        # Same lesson as amazon_on_listing above: the finder query's
        # buyBoxEligibleOfferCountsNewFBA_gte is a snapshot at query time,
        # not authoritative -- confirmed live 2026-08-31, competition can
        # drop between the finder call and this stage2 refresh (4 of 9
        # candidates that cleared min_fba_offers=2 at the finder came back
        # fba_offer_count==1 here). Re-check against the fresh data.
        if stage2.fba_offer_count < finder_cfg.get("min_fba_offers", 1):
            continue
        # And the price band, for the same reason: the buy box can fall
        # below min_buybox_pence between the finder call and this refresh.
        # Two of 177 candidates in the 2026-09-06 full scan came back under
        # the £20 floor, one at £14.49 needing 68% off -- precisely the
        # sub-£20 band the module docstring exists to keep out, arriving
        # through the back door because the floor was only ever enforced at
        # discovery. The ceiling is re-checked too: capital per unit is the
        # reason it exists, and that argument doesn't care when the price moved.
        min_bb = finder_cfg.get("min_buybox_pence")
        max_bb = finder_cfg.get("max_buybox_pence")
        if min_bb and stage2.buybox_price_pence < min_bb:
            continue
        if max_bb and stage2.buybox_price_pence > max_bb:
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
        #
        # Only NOT_ELIGIBLE excludes. This used to drop everything gated,
        # which threw away most of the list: 39 of 50 in the 2026-08-29 run
        # were recorded gated, and that measurement came from a bool parser
        # that could not tell APPROVAL_REQUIRED from NOT_ELIGIBLE. Checked
        # properly on 2026-09-06, all 22 wholesale candidates split 15 open
        # / 7 APPROVAL_REQUIRED / 0 NOT_ELIGIBLE -- nothing was actually
        # shut. APPROVAL_REQUIRED wants a qualifying trade invoice, which is
        # the one document wholesale sourcing produces as a side effect, so
        # for a *sourcing shortlist* it is a paperwork step, not a
        # disqualification. It is kept and marked, and the caller decides.
        #
        # None still means unknown, never gated: SP-API being unreachable
        # must not silently empty the list. Same convention as engine.py.
        gating_note = None
        if check_gating:
            time.sleep(_GATING_DELAY_S)
            gate = spapi_client.check_gating_detail(db, asin)
            if gate is not None and gate.gated:
                if gate.reason_code and "NOT_ELIGIBLE" in gate.reason_code:
                    continue
                gating_note = gate.reason_code or "GATED"

        candidates.append(Candidate(
            asin=asin,
            title=stage2.title,
            buybox_price_pence=stage2.buybox_price_pence,
            target_buy_price_pence=target,
            sales_rank=stage2.sales_rank,
            fba_offer_count=stage2.fba_offer_count,
            est_monthly_sales=stage2.est_monthly_sales,
            gating=gating_note,
        ))
    return candidates


def find_candidates(
    db: Session, app_cfg: dict, cfg: DecisionConfig, fee_provider: FeeProvider
) -> list[Candidate]:
    """Discover one page and score it -- the original single-page entry
    point, kept for the daily scheduler job. To sweep the whole matching
    set instead, call discover_asins() across pages first and hand the
    collected ASINs to score_asins(): see discover_asins' docstring for why
    separating them matters at these token prices."""
    return score_asins(db, discover_asins(db, app_cfg, cfg), app_cfg, cfg, fee_provider)


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
