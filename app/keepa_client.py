"""Two-stage Keepa lookup wrapper (spec: "Keepa usage — two-stage lookup").

Stage 1 is a cheap stats-only screen (~1-2 tokens): kill obviously-bad deals
using cached price/rank history before spending real tokens. Stage 2
(offers=20, ~13 tokens) is reserved for stage-1 survivors and is the
authoritative source for the decision engine's buy-box/offer-count inputs.

Field mapping is grounded in the installed `keepa` package's typed models
(keepa.models.backend.Stats/Product, and the Product.CsvType index ordering
read directly from the library source — see CsvType index constants below)
since no live API key was available at build time. Two fields carry
residual uncertainty and are flagged inline: categoryTree[0] as "the"
category, and stats.buyBoxIsAmazon as the Amazon-on-listing signal. Verify
both against a real product on first live run.

fbaFees.pickAndPackFee confirmed live 2026-07-21 against a real matched
product (B00HER8E5A, DEWALT multi-tool): {"lastUpdate": ..., "pickAndPackFee":
330} — pence, same convention as every other Keepa money field here. This is
Keepa's own fee computed from the catalog's actual package weight/dims
against Amazon's published FBA rate card — meaningfully more trustworthy
than the config-table flat size-tier guess in pricing/fees.py, and it's part
of the base product payload (not gated behind `offers`), so it's effectively
free. Used to avoid the SP-API getMyFeesEstimate cost/eligibility bar
(Pro-seller developer registration + ongoing fee) for this one component;
referral fee and gating still aren't SP-API-backed — see pricing/fees.py.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

import keepa
from sqlalchemy.orm import Session

from . import models
from .config import get_settings
from .decision.engine import DecisionConfig
from .pricing.fees import FeeProvider

KEEPA_DOMAIN = "GB"   # Keepa's domain code for the UK marketplace — NOT "UK"

# Product.CsvType indices into stats.current / stats.avg90 (etc), read from
# the installed keepa package's CsvType enum (declaration order == index).
_IDX_NEW = 1
_IDX_SALES_RANK = 3
_IDX_NEW_FBA = 10
_IDX_BUY_BOX_SHIPPING = 18
_IDX_COUNT_NEW_FBA = 34

_client: "keepa.Keepa | None" = None


def _get_client() -> "keepa.Keepa":
    global _client
    if _client is None:
        _client = keepa.Keepa(get_settings().keepa_api_key)
    return _client


def _log_tokens(db: Session, stage: str, item_count: int, tokens_before: int, tokens_after: int) -> None:
    consumed = tokens_before - tokens_after
    note = None
    if consumed < 0:
        # wait=True can block and let tokens regenerate mid-call, so a
        # negative diff means we can't isolate this call's true cost.
        note = "refilled_during_wait, cost approximate"
    db.add(models.TokenLog(
        stage=stage,
        item_count=item_count,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        tokens_consumed=consumed,
        note=note,
    ))
    db.commit()
    print(f"[KEEPA] {stage}: {item_count} item(s), tokens {tokens_before}->{tokens_after} (cost~{consumed})")


def _csv_value(arr: list | None, idx: int) -> float | None:
    if not arr or len(arr) <= idx:
        return None
    v = arr[idx]
    return None if v is None or v < 0 else v   # -1/-2 in Keepa CSV data means "no value"


def _category_name(product: dict) -> str | None:
    # Assumption: categoryTree is root-first, so index 0 is the broad
    # top-level category matching config.yaml's coarse category names.
    tree = product.get("categoryTree") or []
    return tree[0]["name"] if tree else None


def _fba_fulfilment_fee_pence(product: dict) -> int | None:
    fba_fees = product.get("fbaFees") or {}
    fee = fba_fees.get("pickAndPackFee")
    return fee if fee is not None and fee >= 0 else None


def _rank_history_days(product: dict) -> int | None:
    tracking_since = product.get("trackingSince")
    if not isinstance(tracking_since, datetime):
        return None
    since = tracking_since if tracking_since.tzinfo else tracking_since.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - since).days, 0)


@dataclass
class Stage1Result:
    asin: str
    title: str | None
    category: str | None
    sales_rank: int | None
    est_sell_price_pence: int | None   # optimistic proxy — NOT a confirmed buy box
    rank_history_days: int | None


@dataclass
class Stage2Result:
    asin: str
    title: str | None
    category: str | None
    sales_rank: int | None
    buybox_price_pence: int | None
    amazon_on_listing: bool
    fba_offer_count: int
    lowest_fba_offer_pence: int | None
    est_monthly_sales: float | None
    buybox_avg_90d_pence: int | None
    rank_history_days: int | None
    hazmat: bool
    package_weight_kg: float | None
    package_longest_cm: float | None
    package_dims_sum_cm: float | None
    fba_fulfilment_fee_pence: int | None   # Keepa's own fee for this ASIN's real dims/weight; None if unavailable


def stage1_screen(db: Session, codes: list[str], is_ean: bool) -> dict[str, Stage1Result]:
    """`codes` are EANs when is_ean else ASINs. Batch up to 100 per call."""
    client = _get_client()
    tokens_before = client.tokens_left
    products = client.query(
        codes,
        domain=KEEPA_DOMAIN,
        stats=90,
        offers=None,
        product_code_is_asin=not is_ean,
        wait=True,
    )
    _log_tokens(db, "stage1_screen", len(codes), tokens_before, client.tokens_left)

    results: dict[str, Stage1Result] = {}
    for product in products:
        asin = product.get("asin")
        if not asin:
            continue
        stats = product.get("stats") or {}
        avg90 = stats.get("avg90")
        current = stats.get("current")
        est_sell = _csv_value(avg90, _IDX_BUY_BOX_SHIPPING) or _csv_value(avg90, _IDX_NEW)
        results[asin] = Stage1Result(
            asin=asin,
            title=product.get("title"),
            category=_category_name(product),
            sales_rank=_csv_value(current, _IDX_SALES_RANK),
            est_sell_price_pence=int(est_sell) if est_sell is not None else None,
            rank_history_days=_rank_history_days(product),
        )
    return results


def stage1_screen_passes(result: Stage1Result, buy_price_pence: int, cfg: DecisionConfig, fees: FeeProvider) -> tuple[bool, str | None]:
    """Cheap kill-switch on optimistic assumptions — category blocklist,
    rank threshold, and a rough ROI check assuming best-case offer
    competition (fba_offer_count=0). Returns (passes, reject_reason)."""
    if result.category in cfg.category_blocklist:
        return False, "category_blocklisted"
    rank_threshold = cfg.category_rank_thresholds.get(result.category, cfg.default_rank_threshold)
    if result.sales_rank is not None and result.sales_rank > rank_threshold:
        return False, f"sales_rank {result.sales_rank} worse than {result.category!r} threshold {rank_threshold}"
    if result.est_sell_price_pence is None:
        # No price history at all to screen on — let stage 2 make the real call.
        return True, None

    fee_input = fees.get_fees(result.category or "", result.est_sell_price_pence, dims=None)
    fee_vat_mult = 1.0 if cfg.vat_registered else 1.20
    total_fees = round((fee_input.referral_fee_pence + fee_input.fba_fulfilment_fee_pence) * fee_vat_mult)
    optimistic_net_profit = (
        result.est_sell_price_pence - total_fees - fee_input.monthly_storage_fee_pence
        - cfg.inbound_shipping_pence - buy_price_pence
    )
    optimistic_roi = optimistic_net_profit / buy_price_pence
    if optimistic_roi < cfg.min_roi:
        return False, f"optimistic roi {optimistic_roi:.1%} < {cfg.min_roi:.0%} on best-case assumptions"
    return True, None


def stage2_full(db: Session, asins: list[str]) -> dict[str, Stage2Result]:
    """Only call for stage-1 survivors. Batch up to 100 per call."""
    client = _get_client()
    tokens_before = client.tokens_left
    products = client.query(
        asins,
        domain=KEEPA_DOMAIN,
        stats=90,
        offers=20,
        product_code_is_asin=True,
        wait=True,
    )
    _log_tokens(db, "stage2_full", len(asins), tokens_before, client.tokens_left)

    results: dict[str, Stage2Result] = {}
    for product in products:
        asin = product.get("asin")
        if not asin:
            continue
        stats = product.get("stats") or {}
        current = stats.get("current")
        avg90 = stats.get("avg90")

        buybox_price = stats.get("buyBoxPrice")
        if buybox_price is not None and buybox_price < 0:   # -2 = no buy box
            buybox_price = None

        fba_offer_count = _csv_value(current, _IDX_COUNT_NEW_FBA)
        lowest_fba = _csv_value(current, _IDX_NEW_FBA)
        sales_rank = _csv_value(current, _IDX_SALES_RANK)
        buybox_avg_90d = _csv_value(avg90, _IDX_BUY_BOX_SHIPPING)

        monthly_sold = product.get("monthlySold")
        rank_drops_30 = stats.get("salesRankDrops30")
        est_monthly_sales = float(monthly_sold) if monthly_sold else (float(rank_drops_30) if rank_drops_30 else None)

        weight_g = product.get("packageWeight")
        dims_mm = [d for d in (product.get(k) for k in ("packageHeight", "packageLength", "packageWidth")) if d]

        results[asin] = Stage2Result(
            asin=asin,
            title=product.get("title"),
            category=_category_name(product),
            sales_rank=int(sales_rank) if sales_rank is not None else None,
            buybox_price_pence=int(buybox_price) if buybox_price is not None else None,
            amazon_on_listing=bool(stats.get("buyBoxIsAmazon")),
            fba_offer_count=int(fba_offer_count) if fba_offer_count is not None else 0,
            lowest_fba_offer_pence=int(lowest_fba) if lowest_fba is not None else None,
            est_monthly_sales=est_monthly_sales,
            buybox_avg_90d_pence=int(buybox_avg_90d) if buybox_avg_90d is not None else None,
            rank_history_days=_rank_history_days(product),
            hazmat=bool(product.get("hazardousMaterials")),
            package_weight_kg=(weight_g / 1000) if weight_g else None,
            package_longest_cm=(max(dims_mm) / 10) if dims_mm else None,
            package_dims_sum_cm=(sum(dims_mm) / 10) if dims_mm else None,
            fba_fulfilment_fee_pence=_fba_fulfilment_fee_pence(product),
        )
    return results
