"""Pure decision engine — no DB, no network, no app.config import, so it can
be unit-tested completely offline (see tests/test_decision_engine.py).

Implements the formula from fba-deal-scanner-spec.md ("Decision engine"),
with one deliberate correction — see NOTE below — and the 2026-10-01
criteria update (cost lines marked *):

    sell_price     = min(current buy box, 90-day avg buy box), else lowest FBA offer
    amazon_fees    = (referral_fee + fba_fulfilment_fee) * (1.15 if fees estimated*)
    total_fees     = amazon_fees * (1 + digital_services_fee*) * fee_vat_mult
    our_share      = est_monthly_sales / (fba_offer_count + 1)
    est_months_to_sell = clamp(order_units* / our_share, 1, 6)

    net_profit = sell_price
               - total_fees
               - storage (month by month, Q4 rate in Oct-Dec*)
               - returns_allowance* (sell_price * category %)
               - prep_cost* - inbound_shipping_per_unit
               - buy_price
    roi = net_profit / buy_price

NOTE: the spec's literal formula never subtracts buy_price from net_profit,
which would make "net_profit" ignore cost-of-goods entirely (buy at £10,
sell at £25 -> reported ~£16 "profit" / ~160% "ROI"). That can't be right
given the hard filters gate on net_profit < £3 and roi < 30% — both only
make sense as true profit and true return-on-capital. Implemented here WITH
`- buy_price_pence` added; flagged to the user, easy to revert if wrong.

Hard rules run cheapest first and the first failure is the reject reason:
sell price -> hazmat/oversize/gating -> Amazon -> private label -> seller
count -> rank -> velocity -> profit.

All money fields are pence (int). `roi` and `est_months_to_sell` are ratios,
kept as float.
"""
from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class Verdict(str, Enum):
    PASS = "PASS"
    PASS_WITH_FLAGS = "PASS_WITH_FLAGS"
    REJECT = "REJECT"


@dataclass
class FeeInput:
    referral_fee_pence: int
    fba_fulfilment_fee_pence: int
    monthly_storage_fee_pence: int
    estimated: bool   # True when sourced from the config fee-table fallback (no SP-API yet)
    q4_monthly_storage_fee_pence: int | None = None   # None = same rate all year


@dataclass
class ScoreInput:
    buy_price_pence: int
    match_confidence: str            # 'high' | 'low'
    category: str
    fba_offer_count: int
    amazon_on_listing: bool
    fees: FeeInput
    sales_rank: int | None = None
    sales_rank_avg90: int | None = None   # preferred over sales_rank for the rank cap
    est_monthly_sales: float | None = None   # Keepa monthlySold badge only
    buybox_price_pence: int | None = None
    lowest_fba_offer_pence: int | None = None
    buybox_avg_90d_pence: int | None = None
    buybox_avg_30d_pence: int | None = None
    rank_history_days: int | None = None
    hazmat: bool = False
    oversize: bool = False
    gating_status: str | None = None   # "ungated" | "approval_required" | "not_eligible" | None = not checked
    category_rank_percentile: float | None = None   # 90-day avg leaf rank / leaf productCount; None if unavailable
    leaf_category_size: int | None = None
    distinct_sellers_ever: int | None = None   # lifetime, from Keepa; None = unknown
    max_new_offers_ever: int | None = None
    amazon_instock_pct_90: float | None = None   # 0-1
    fba_offer_count_30d_ago: int | None = None
    sellers_active_90d: int | None = None
    brand_matches_main_seller: bool | None = None
    is_variation: bool = False
    start_month: int = field(default_factory=lambda: date.today().month)


@dataclass
class ScoreResult:
    verdict: Verdict
    verdict_reason: str | None
    sell_price_pence: int | None
    net_profit_pence: int | None
    roi: float | None
    flags: list[str] = field(default_factory=list)
    fees_breakdown: dict = field(default_factory=dict)
    est_months_to_sell: float | None = None
    velocity_basis: str | None = None   # "monthly_sold" | "leaf_rank_top_pct" | "category_rank_cap"


@dataclass
class DecisionConfig:
    min_roi: float
    min_net_profit_pence: int
    max_fba_offers: int
    rank_history_min_days: int
    price_spike_pct: float
    vat_registered: bool
    reject_oversize: bool
    category_rank_thresholds: dict
    default_rank_threshold: int
    category_blocklist: set
    inbound_shipping_pence: int
    # Everything below defaults to a no-op so an explicitly-constructed
    # config (tests, older callers) scores exactly as before; the real
    # values come from config.yaml via from_app_config. order_units=1
    # reproduces the old 1 / our_share months-to-sell model.
    min_sell_price_pence: int = 0
    velocity_min_monthly_sales: float = 10.0
    velocity_top_percentile: float = 0.02
    small_subcategory_min_items: int = 5000
    small_subcategory_use_rank_cap: bool = False
    max_amazon_instock_pct_90: float = 1.0
    min_monthly_sales_per_seller: float = 0.0
    order_units: int = 1
    prep_cost_pence: int = 0
    digital_services_fee_pct: float = 0.0
    estimated_fee_buffer_pct: float = 0.0
    default_returns_allowance_pct: float = 0.0
    returns_allowance_pct: dict = field(default_factory=dict)
    q4_months: frozenset = frozenset()
    fba_offer_growth_warn_pct: float = 0.5
    price_downtrend_warn_pct: float = 0.10
    expiry_dated_categories: frozenset = frozenset()

    @classmethod
    def from_app_config(cls, cfg: dict) -> "DecisionConfig":
        thresholds = cfg["thresholds"]
        rank_cfg = dict(cfg["category_rank_thresholds"])
        default_rank_threshold = rank_cfg.pop("default_rank_threshold")
        velocity_cfg = cfg.get("velocity") or {}
        decision_cfg = cfg["decision"]
        returns_cfg = dict(decision_cfg.get("returns_allowance_pct") or {})
        warn_cfg = cfg.get("warnings") or {}
        return cls(
            min_roi=thresholds["min_roi"],
            min_net_profit_pence=thresholds["min_net_profit_pence"],
            max_fba_offers=thresholds["max_fba_offers"],
            rank_history_min_days=thresholds["rank_history_min_days"],
            price_spike_pct=thresholds["price_spike_pct"],
            vat_registered=cfg["vat_registered"],
            reject_oversize=cfg["reject_oversize"],
            category_rank_thresholds=rank_cfg,
            default_rank_threshold=default_rank_threshold,
            category_blocklist=set(cfg.get("category_blocklist") or []),
            inbound_shipping_pence=decision_cfg["inbound_shipping_pence"],
            min_sell_price_pence=thresholds.get("min_sell_price_pence", 0),
            velocity_min_monthly_sales=velocity_cfg.get("min_monthly_sales", 10.0),
            velocity_top_percentile=velocity_cfg.get("top_category_percentile", 0.02),
            small_subcategory_min_items=velocity_cfg.get("small_subcategory_min_items", 5000),
            small_subcategory_use_rank_cap=velocity_cfg.get("small_subcategory_use_rank_cap", False),
            max_amazon_instock_pct_90=thresholds.get("max_amazon_instock_pct_90", 1.0),
            min_monthly_sales_per_seller=thresholds.get("min_monthly_sales_per_seller", 0.0),
            order_units=decision_cfg.get("order_units", 1),
            prep_cost_pence=decision_cfg.get("prep_cost_pence", 0),
            digital_services_fee_pct=decision_cfg.get("digital_services_fee_pct", 0.0),
            estimated_fee_buffer_pct=decision_cfg.get("estimated_fee_buffer_pct", 0.0),
            default_returns_allowance_pct=returns_cfg.pop("default", 0.0),
            returns_allowance_pct=returns_cfg,
            q4_months=frozenset(decision_cfg.get("q4_storage_months") or []),
            fba_offer_growth_warn_pct=warn_cfg.get("fba_offer_growth_pct", 0.5),
            price_downtrend_warn_pct=warn_cfg.get("price_downtrend_pct", 0.10),
            expiry_dated_categories=frozenset(warn_cfg.get("expiry_dated_categories") or []),
        )


def _reject(reason: str, sell_price_pence: int | None) -> ScoreResult:
    return ScoreResult(
        verdict=Verdict.REJECT,
        verdict_reason=reason,
        sell_price_pence=sell_price_pence,
        net_profit_pence=None,
        roi=None,
    )


def resolve_sell_price(buybox_pence: int | None, buybox_avg_90d_pence: int | None,
                       lowest_fba_offer_pence: int | None) -> tuple[int | None, bool]:
    """(sell price, fell back to lowest FBA offer). The lower of the current
    and 90-day average buy box, so a temporary spike can't carry a deal.
    Callers price fees off this too, so referral matches what's scored."""
    if buybox_pence is not None:
        return (min(buybox_pence, buybox_avg_90d_pence) if buybox_avg_90d_pence else buybox_pence), False
    if lowest_fba_offer_pence is not None:
        return lowest_fba_offer_pence, True
    return None, False


def _rank_for_cap(inp: ScoreInput) -> int | None:
    return inp.sales_rank_avg90 if inp.sales_rank_avg90 is not None else inp.sales_rank


def velocity(inp: ScoreInput, cfg: DecisionConfig) -> tuple[bool, str | None]:
    """(passes, which test passed). monthlySold >= floor, else a top-N%
    90-day average leaf-category rank. A small leaf category makes top 2%
    too easy (rank 60 of 3,000), so optionally fall back to the category
    rank cap there instead."""
    if (inp.est_monthly_sales or 0) >= cfg.velocity_min_monthly_sales:
        return True, "monthly_sold"
    if (cfg.small_subcategory_use_rank_cap and inp.leaf_category_size is not None
            and inp.leaf_category_size < cfg.small_subcategory_min_items):
        rank = _rank_for_cap(inp)
        cap = cfg.category_rank_thresholds.get(inp.category, cfg.default_rank_threshold)
        return (rank is not None and rank <= cap), "category_rank_cap"
    if inp.category_rank_percentile is not None and inp.category_rank_percentile <= cfg.velocity_top_percentile:
        return True, "leaf_rank_top_pct"
    return False, None


def _effective_monthly_sales(inp: ScoreInput, cfg: DecisionConfig) -> float | None:
    """The badge, or for a rank-test pass with no badge the velocity floor --
    it cleared the gate, so it sells at least that."""
    if inp.est_monthly_sales:
        return inp.est_monthly_sales
    passed, _ = velocity(inp, cfg)
    return cfg.velocity_min_monthly_sales if passed else None


def months_to_sell(est_monthly_sales: float | None, fba_offer_count: int, cfg: DecisionConfig) -> float:
    """Months to clear one order of cfg.order_units at our share of sales,
    clamped to 1-6. Unknown sales means the 6-month worst case."""
    our_share = (est_monthly_sales or 0.0) / (fba_offer_count + 1)
    return min(max(cfg.order_units / max(our_share, 0.1), 1.0), 6.0)


def _storage_cost(fees: FeeInput, months: float, start_month: int, cfg: DecisionConfig) -> int:
    """Month by month from start_month, at the Q4 rate in cfg.q4_months; the
    last month is pro-rated."""
    total, remaining, month = 0.0, months, start_month
    while remaining > 0:
        part = min(1.0, remaining)
        q4 = month in cfg.q4_months and fees.q4_monthly_storage_fee_pence is not None
        total += (fees.q4_monthly_storage_fee_pence if q4 else fees.monthly_storage_fee_pence) * part
        remaining -= part
        month = month % 12 + 1
    return round(total)


def cost_breakdown(inp: ScoreInput, sell_price_pence: int, cfg: DecisionConfig) -> dict:
    """Every per-unit cost except the buy price. Shared with the target-
    buy-price callers so the price they quote is the one scoring accepts."""
    fee_vat_mult = 1.0 if cfg.vat_registered else 1.20
    buffer = (1 + cfg.estimated_fee_buffer_pct) if inp.fees.estimated else 1.0
    amazon_fees = (inp.fees.referral_fee_pence + inp.fees.fba_fulfilment_fee_pence) * buffer
    digital_services_fee = amazon_fees * cfg.digital_services_fee_pct
    total_fees = round((amazon_fees + digital_services_fee) * fee_vat_mult)

    est_months = months_to_sell(_effective_monthly_sales(inp, cfg), inp.fba_offer_count, cfg)
    storage_cost = _storage_cost(inp.fees, est_months, inp.start_month, cfg)
    returns_pct = cfg.returns_allowance_pct.get(inp.category, cfg.default_returns_allowance_pct)
    returns_allowance = round(sell_price_pence * returns_pct)

    non_buy = total_fees + storage_cost + returns_allowance + cfg.prep_cost_pence + cfg.inbound_shipping_pence
    return {
        "referral_fee_pence": inp.fees.referral_fee_pence,
        "fba_fulfilment_fee_pence": inp.fees.fba_fulfilment_fee_pence,
        "estimated_fee_buffer": buffer,
        "digital_services_fee_pence": round(digital_services_fee),
        "fee_vat_mult": fee_vat_mult,
        "total_fees_pence": total_fees,
        "monthly_storage_fee_pence": inp.fees.monthly_storage_fee_pence,
        "q4_monthly_storage_fee_pence": inp.fees.q4_monthly_storage_fee_pence,
        "storage_cost_pence": storage_cost,
        "est_months_to_sell": est_months,
        "returns_allowance_pence": returns_allowance,
        "prep_cost_pence": cfg.prep_cost_pence,
        "inbound_shipping_pence": cfg.inbound_shipping_pence,
        "non_buy_costs_pence": non_buy,
        "estimated": inp.fees.estimated,
    }


def score_deal(inp: ScoreInput, cfg: DecisionConfig) -> ScoreResult:
    flags: list[str] = []

    sell_price, used_lowest_fba = resolve_sell_price(
        inp.buybox_price_pence, inp.buybox_avg_90d_pence, inp.lowest_fba_offer_pence)
    if sell_price is None:
        return _reject("no_sell_price", None)
    if used_lowest_fba:
        flags.append("no_buybox")
    if sell_price < cfg.min_sell_price_pence:
        return _reject(f"sell price {sell_price}p below {cfg.min_sell_price_pence}p floor", sell_price)
    if inp.category in cfg.category_blocklist:
        return _reject("category_blocklisted", sell_price)

    # --- can't sell it at all ---
    if inp.hazmat:
        return _reject("hazmat", sell_price)
    if inp.oversize and cfg.reject_oversize:
        return _reject("oversize", sell_price)
    if inp.gating_status == "not_eligible":
        return _reject("gating_not_eligible", sell_price)

    # --- Amazon ---
    if inp.amazon_on_listing:
        return _reject("amazon_on_listing", sell_price)
    if inp.amazon_instock_pct_90 is not None and inp.amazon_instock_pct_90 > cfg.max_amazon_instock_pct_90:
        return _reject(
            f"amazon_in_stock {inp.amazon_instock_pct_90:.0%} of last 90 days > {cfg.max_amazon_instock_pct_90:.0%}",
            sell_price)

    # --- private label: only one seller ever on the listing (HGUIM, KKSTY:
    # 1 ever; real brands like Myprotein or Lesser & Pavey show 2-23). Both
    # legs must agree -- see keepa_client._seller_history for why seller IDs
    # alone undercount. Brand-sells-direct and one-active-seller are only
    # warnings: both also fit genuine wholesale brands. ---
    if (inp.distinct_sellers_ever is not None and inp.distinct_sellers_ever <= 1
            and inp.max_new_offers_ever is not None and inp.max_new_offers_ever <= 1):
        return _reject("single_seller_listing", sell_price)

    # --- seller count ---
    if inp.fba_offer_count > cfg.max_fba_offers:
        return _reject(f"fba_offer_count {inp.fba_offer_count} > max {cfg.max_fba_offers}", sell_price)
    monthly_sales = _effective_monthly_sales(inp, cfg)
    if monthly_sales is not None:
        per_seller = monthly_sales / (inp.fba_offer_count + 1)
        if per_seller < cfg.min_monthly_sales_per_seller:
            return _reject(
                f"sales_per_seller {per_seller:.1f}/mo < {cfg.min_monthly_sales_per_seller:g} "
                f"({monthly_sales:g}/mo over {inp.fba_offer_count + 1} sellers incl. us)", sell_price)

    # --- rank cap, on the 90-day average so one good day can't pass ---
    rank = _rank_for_cap(inp)
    rank_threshold = cfg.category_rank_thresholds.get(inp.category, cfg.default_rank_threshold)
    if rank is not None and rank > rank_threshold:
        return _reject(f"sales_rank {rank} worse than {inp.category!r} threshold {rank_threshold}", sell_price)

    # --- velocity: the monthlySold badge or a top-2% leaf rank. The
    # rank-drop proxy was removed 2026-10-01: it overestimated slow sellers
    # ~10x (coconut lamp, shamanic drum) and undercounted fast ones
    # (1,000-2,000/mo sellers showed 23-59 drops). 11 of 12 badge-less
    # products with 50+ drops cleared the top-2% leaf rank in a live check. ---
    velocity_ok, velocity_basis = velocity(inp, cfg)
    if not velocity_ok:
        return _reject(
            f"velocity_floor: est_monthly_sales={inp.est_monthly_sales} "
            f"(floor={cfg.velocity_min_monthly_sales}) "
            f"category_rank_percentile={inp.category_rank_percentile}",
            sell_price,
        )

    # --- profit ---
    costs = cost_breakdown(inp, sell_price, cfg)
    est_months_to_sell = costs["est_months_to_sell"]
    # buy_price_pence subtracted here — see module docstring NOTE.
    net_profit = sell_price - costs["non_buy_costs_pence"] - inp.buy_price_pence
    roi = net_profit / inp.buy_price_pence

    if roi < cfg.min_roi or net_profit < cfg.min_net_profit_pence:
        reasons = []
        if roi < cfg.min_roi:
            reasons.append(f"roi {roi:.1%} < {cfg.min_roi:.0%}")
        if net_profit < cfg.min_net_profit_pence:
            reasons.append(f"net_profit {net_profit}p < {cfg.min_net_profit_pence}p")
        return ScoreResult(
            verdict=Verdict.REJECT,
            verdict_reason="; ".join(reasons),
            sell_price_pence=sell_price,
            net_profit_pence=net_profit,
            roi=roi,
            flags=flags,
            fees_breakdown=costs,
            est_months_to_sell=est_months_to_sell,
            velocity_basis=velocity_basis,
        )

    # --- warnings (deal passes, but annotate) ---
    if inp.match_confidence == "low":
        flags.append("low_confidence")
    if inp.fees.estimated:
        flags.append("estimated_fees")
    if (inp.buybox_price_pence and inp.buybox_avg_90d_pence
            and inp.buybox_price_pence > inp.buybox_avg_90d_pence * (1 + cfg.price_spike_pct)):
        flags.append("price_spike_risk")
    if inp.rank_history_days is not None and inp.rank_history_days < cfg.rank_history_min_days:
        flags.append("short_rank_history")
    if inp.amazon_instock_pct_90:
        flags.append(f"amazon_in_stock_recently: {inp.amazon_instock_pct_90:.0%} of the last 90 days")
    if (inp.fba_offer_count_30d_ago is not None
            and inp.fba_offer_count > inp.fba_offer_count_30d_ago * (1 + cfg.fba_offer_growth_warn_pct)):
        flags.append(f"fba_sellers_rising: {inp.fba_offer_count_30d_ago} -> {inp.fba_offer_count} in 30 days")
    if (inp.buybox_avg_30d_pence and inp.buybox_avg_90d_pence
            and inp.buybox_avg_30d_pence < inp.buybox_avg_90d_pence * (1 - cfg.price_downtrend_warn_pct)):
        flags.append(f"price_trending_down: 30-day avg {inp.buybox_avg_30d_pence}p vs 90-day {inp.buybox_avg_90d_pence}p")
    if inp.is_variation:
        flags.append("variation_listing")
    if inp.category in cfg.expiry_dated_categories:
        flags.append("expiry_dated_category")
    if inp.brand_matches_main_seller:
        flags.append("brand_sells_direct: the brand holds the buy box -- likely brand-controlled")
    if inp.sellers_active_90d == 1:
        flags.append("one_active_seller_90d")
    if inp.gating_status == "approval_required":
        flags.append("gating_approval_required")

    verdict = Verdict.PASS_WITH_FLAGS if flags else Verdict.PASS
    return ScoreResult(
        verdict=verdict,
        verdict_reason=None,
        sell_price_pence=sell_price,
        net_profit_pence=net_profit,
        roi=roi,
        flags=flags,
        fees_breakdown=costs,
        est_months_to_sell=est_months_to_sell,
        velocity_basis=velocity_basis,
    )
