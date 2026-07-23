"""Pure decision engine — no DB, no network, no app.config import, so it can
be unit-tested completely offline (see tests/test_decision_engine.py).

Implements the formula from fba-deal-scanner-spec.md ("Decision engine"),
with one deliberate correction — see NOTE below:

    fee_vat_mult   = 1.0 if vat_registered else 1.20
    total_fees     = (referral_fee + fba_fulfilment_fee) * fee_vat_mult

    our_share      = est_monthly_sales / (fba_offer_count + 1)
    est_months_to_sell = clamp(1 / max(our_share, 0.1), 1, 6)

    net_profit = sell_price
               - total_fees
               - (monthly_storage_fee * est_months_to_sell)
               - inbound_shipping_per_unit
    roi = net_profit / buy_price

NOTE: the spec's literal formula never subtracts buy_price from net_profit,
which would make "net_profit" ignore cost-of-goods entirely (buy at £10,
sell at £25 -> reported ~£16 "profit" / ~160% "ROI"). That can't be right
given the hard filters gate on net_profit < £3 and roi < 30% — both only
make sense as true profit and true return-on-capital. Implemented here WITH
`- buy_price_pence` added; flagged to the user, easy to revert if wrong.

All money fields are pence (int). `roi` and `est_months_to_sell` are ratios,
kept as float.
"""
from dataclasses import dataclass, field
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


@dataclass
class ScoreInput:
    buy_price_pence: int
    match_confidence: str            # 'high' | 'low'
    category: str
    fba_offer_count: int
    amazon_on_listing: bool
    fees: FeeInput
    sales_rank: int | None = None
    est_monthly_sales: float | None = None
    buybox_price_pence: int | None = None
    lowest_fba_offer_pence: int | None = None
    buybox_avg_90d_pence: int | None = None
    rank_history_days: int | None = None
    hazmat: bool = False
    oversize: bool = False
    gated: bool | None = None        # None = not checked (no SP-API yet)
    category_rank_percentile: float | None = None   # sales_rank / leaf-category productCount; None if unavailable


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
    velocity_min_monthly_sales: float = 10.0
    velocity_top_percentile: float = 0.02

    @classmethod
    def from_app_config(cls, cfg: dict) -> "DecisionConfig":
        thresholds = cfg["thresholds"]
        rank_cfg = dict(cfg["category_rank_thresholds"])
        default_rank_threshold = rank_cfg.pop("default_rank_threshold")
        velocity_cfg = cfg.get("velocity") or {}
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
            inbound_shipping_pence=cfg["decision"]["inbound_shipping_pence"],
            velocity_min_monthly_sales=velocity_cfg.get("min_monthly_sales", 10.0),
            velocity_top_percentile=velocity_cfg.get("top_category_percentile", 0.02),
        )


def _reject(reason: str, sell_price_pence: int | None) -> ScoreResult:
    return ScoreResult(
        verdict=Verdict.REJECT,
        verdict_reason=reason,
        sell_price_pence=sell_price_pence,
        net_profit_pence=None,
        roi=None,
    )


def score_deal(inp: ScoreInput, cfg: DecisionConfig) -> ScoreResult:
    flags: list[str] = []

    # --- sell price resolution ---
    if inp.buybox_price_pence is not None:
        sell_price = inp.buybox_price_pence
    elif inp.lowest_fba_offer_pence is not None:
        sell_price = inp.lowest_fba_offer_pence
        flags.append("no_buybox")
    else:
        return _reject("no_sell_price", None)

    # --- hard filters (checked before spending effort on the money maths) ---
    if inp.category in cfg.category_blocklist:
        return _reject("category_blocklisted", sell_price)
    if inp.amazon_on_listing:
        return _reject("amazon_on_listing", sell_price)
    if inp.fba_offer_count > cfg.max_fba_offers:
        return _reject(f"fba_offer_count {inp.fba_offer_count} > max {cfg.max_fba_offers}", sell_price)
    rank_threshold = cfg.category_rank_thresholds.get(inp.category, cfg.default_rank_threshold)
    if inp.sales_rank is not None and inp.sales_rank > rank_threshold:
        return _reject(
            f"sales_rank {inp.sales_rank} worse than {inp.category!r} threshold {rank_threshold}",
            sell_price,
        )
    if inp.gated is True:
        return _reject("gated", sell_price)
    if inp.hazmat:
        return _reject("hazmat", sell_price)
    if inp.oversize and cfg.reject_oversize:
        return _reject("oversize", sell_price)

    # --- velocity gate (Fix Build Guide phase 3): reject ROI-green dead
    # stock -- must clear EITHER a minimum monthly-sales volume OR a tight
    # rank percentile within its actual (leaf) category. Percentile is
    # deliberately NOT computed against the coarse root category here --
    # see keepa_client._leaf_category's docstring for why that would be
    # almost meaningless (root categories can run into the tens of millions
    # of products). category_rank_percentile is None whenever leaf-category
    # data wasn't available, in which case only the sales-volume leg counts. ---
    sales_ok = (inp.est_monthly_sales or 0) >= cfg.velocity_min_monthly_sales
    rank_ok = inp.category_rank_percentile is not None and inp.category_rank_percentile <= cfg.velocity_top_percentile
    if not (sales_ok or rank_ok):
        return _reject(
            f"velocity_floor: est_monthly_sales={inp.est_monthly_sales} "
            f"category_rank_percentile={inp.category_rank_percentile}",
            sell_price,
        )

    # --- financials ---
    fee_vat_mult = 1.0 if cfg.vat_registered else 1.20
    total_fees = round((inp.fees.referral_fee_pence + inp.fees.fba_fulfilment_fee_pence) * fee_vat_mult)

    est_monthly_sales = inp.est_monthly_sales or 0.0
    our_share = est_monthly_sales / (inp.fba_offer_count + 1)
    est_months_to_sell = min(max(1.0 / max(our_share, 0.1), 1.0), 6.0)

    storage_cost = round(inp.fees.monthly_storage_fee_pence * est_months_to_sell)
    # buy_price_pence subtracted here — see module docstring NOTE.
    net_profit = sell_price - total_fees - storage_cost - cfg.inbound_shipping_pence - inp.buy_price_pence
    roi = net_profit / inp.buy_price_pence

    fees_breakdown = {
        "referral_fee_pence": inp.fees.referral_fee_pence,
        "fba_fulfilment_fee_pence": inp.fees.fba_fulfilment_fee_pence,
        "fee_vat_mult": fee_vat_mult,
        "total_fees_pence": total_fees,
        "monthly_storage_fee_pence": inp.fees.monthly_storage_fee_pence,
        "storage_cost_pence": storage_cost,
        "inbound_shipping_pence": cfg.inbound_shipping_pence,
        "estimated": inp.fees.estimated,
    }

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
            fees_breakdown=fees_breakdown,
            est_months_to_sell=est_months_to_sell,
        )

    # --- soft flags (deal passes, but annotate) ---
    if inp.match_confidence == "low":
        flags.append("low_confidence")
    if inp.fees.estimated:
        flags.append("estimated_fees")
    if inp.buybox_avg_90d_pence and sell_price > inp.buybox_avg_90d_pence * (1 + cfg.price_spike_pct):
        flags.append("price_spike_risk")
    if inp.rank_history_days is not None and inp.rank_history_days < cfg.rank_history_min_days:
        flags.append("short_rank_history")

    verdict = Verdict.PASS_WITH_FLAGS if flags else Verdict.PASS
    return ScoreResult(
        verdict=verdict,
        verdict_reason=None,
        sell_price_pence=sell_price,
        net_profit_pence=net_profit,
        roi=roi,
        flags=flags,
        fees_breakdown=fees_breakdown,
        est_months_to_sell=est_months_to_sell,
    )
