"""Orchestrates one deal end-to-end: dedupe against `deals`, resolve the
HUKD redirect, match to an ASIN (Amazon-URL shortcut -> JSON-LD -> give up),
run the two-stage Keepa lookup + decision engine, and ping Discord subject
to the ASIN cooldown. Shared by the scheduler's RSS poll job today; the
Phase 2 /scan endpoint reuses the same process_deal() for its synchronous
scan-and-verdict flow.
"""
from sqlalchemy.orm import Session

from . import discord_notifier, keepa_client, models, resolver
from .config import get_settings
from .decision.engine import DecisionConfig, ScoreInput, Verdict, score_deal
from .matching import amazon_url, cache, jsonld
from .pricing.fees import FeeProvider, SizeDims
from .sources.base import RawDeal

# Statuses safe to re-run the pipeline on if the deal resurfaces at the same
# price. Everything else (matched a category-reject, already pinged, already
# suppressed by cooldown, etc.) is a stable outcome at that price — retrying
# would just waste Keepa tokens for the same answer. fetch_blocked is
# retriable because a Cloudflare/bot block is transient infra, not a
# judgement about the deal.
_RETRIABLE_STATUSES = {"new", "resolved", "fetch_blocked"}


def process_deal(db: Session, raw: RawDeal, decision_cfg: DecisionConfig, fee_provider: FeeProvider, app_cfg: dict) -> None:
    deal = _upsert_deal(db, raw)
    if deal is None:
        return   # unchanged price, already at a stable terminal status

    if raw.html is not None:
        # Retailer scraper deal: raw.url is already the final retailer page
        # and raw.html is already fetched — no HUKD redirect to follow.
        resolved = resolver.ResolvedDeal(final_url=raw.url, html=raw.html, status_code=200, blocked=False)
    else:
        resolved = resolver.resolve(raw.url)
        if resolved is None:
            deal.status = "unresolvable"
            db.commit()
            print(f"[PIPELINE] {raw.url}: not a recognisable thread URL, skipping")
            return

    deal.retailer_url = resolved.final_url
    if resolved.blocked:
        deal.status = "fetch_blocked"
        db.commit()
        print(f"[PIPELINE] {raw.url}: retailer fetch blocked, will retry next poll")
        return
    deal.status = "resolved"
    db.commit()

    asin = amazon_url.extract_asin(resolved.final_url)
    ean = None
    if asin:
        matched_via, confidence = "amazon_url", "high"
    else:
        ean = jsonld.extract_ean(resolved.final_url, resolved.html or "")
        matched_via, confidence = ("jsonld", "high") if ean else (None, None)

    if not asin and not ean:
        deal.status = "no_ean_match"
        db.commit()
        _ping_unverified(db, deal)
        return

    product, stage1 = _resolve_product(db, ean=ean, asin=asin, title=deal.title, matched_via=matched_via, confidence=confidence)
    if product.asin is None:
        deal.status = "no_ean_match"
        db.commit()
        _ping_unverified(db, deal)
        return

    deal.product_id = product.id
    deal.status = "matched"
    db.commit()

    if stage1 is None:
        stage1 = keepa_client.stage1_screen(db, [product.asin], is_ean=False).get(product.asin)
    if stage1 is None:
        deal.status = "stage1_rejected"
        db.commit()
        print(f"[PIPELINE] {product.asin}: not found on Keepa")
        return

    passes, reason = keepa_client.stage1_screen_passes(stage1, deal.buy_price, decision_cfg, fee_provider)
    if not passes:
        deal.status = "stage1_rejected"
        db.commit()
        print(f"[PIPELINE] {product.asin}: stage1 screen rejected ({reason})")
        return

    stage2 = keepa_client.stage2_full(db, [product.asin]).get(product.asin)
    if stage2 is None:
        deal.status = "stage1_rejected"
        db.commit()
        print(f"[PIPELINE] {product.asin}: stage2 returned no data")
        return

    score = _score_and_record(db, deal, product, stage2, decision_cfg, fee_provider)
    deal.status = "stage2_scored"
    db.commit()

    if score.verdict == Verdict.REJECT.value:
        print(f"[PIPELINE] {product.asin}: REJECT ({score.verdict_reason})")
        return

    thresholds = app_cfg["thresholds"]
    if not discord_notifier.should_ping(
        db, product.asin, deal.buy_price, thresholds["cooldown_hours"], thresholds["cooldown_price_improve_pct"]
    ):
        deal.status = "cooldown_suppressed"
        db.commit()
        print(f"[PIPELINE] {product.asin}: cooldown suppressed")
        return

    result = ScoreResultView(score)
    embed = discord_notifier.build_matched_embed(
        title=deal.title,
        retailer_url=deal.retailer_url,
        image_url=deal.image_url,
        retailer=deal.retailer,
        asin=product.asin,
        buy_price_pence=deal.buy_price,
        result=result,
        est_monthly_sales=stage2.est_monthly_sales,
        offer_count=stage2.fba_offer_count,
        amazon_on_listing=stage2.amazon_on_listing,
        gated=None,
        match_confidence=product.confidence,
    )
    sent = discord_notifier.send_ping(get_settings().discord_webhook_url, embed)
    if sent:
        discord_notifier.record_ping(db, product.asin, deal.id, score.id)
        deal.status = "pinged"
    else:
        deal.status = "ping_failed"
    db.commit()


def _upsert_deal(db: Session, raw: RawDeal) -> models.Deal | None:
    existing = db.query(models.Deal).filter(models.Deal.url == raw.url).first()
    if existing is None:
        deal = models.Deal(
            source=raw.source, retailer=raw.retailer, title=raw.title,
            image_url=raw.image_url, url=raw.url, buy_price=raw.buy_price_pence,
            status="new",
        )
        db.add(deal)
        db.commit()
        db.refresh(deal)
        return deal

    if existing.buy_price == raw.buy_price_pence and existing.status not in _RETRIABLE_STATUSES:
        existing.last_seen = models.utcnow()
        db.commit()
        return None

    existing.buy_price = raw.buy_price_pence
    existing.last_seen = models.utcnow()
    existing.status = "new"
    db.commit()
    return existing


def _resolve_product(
    db: Session, *, ean: str | None, asin: str | None, title: str, matched_via: str | None, confidence: str | None
) -> tuple[models.Product, "keepa_client.Stage1Result | None"]:
    if asin:
        cached = cache.get_cached_by_asin(db, asin)
        if cached:
            return cached, None
        return cache.cache_product(db, ean=None, asin=asin, title=title, matched_via=matched_via, confidence=confidence), None

    cached = cache.get_cached_by_ean(db, ean)
    if cached:
        return cached, None

    # Cheap Keepa product-by-code lookup (~1-2 tokens, same price as stage 1)
    # resolves EAN -> ASIN and doubles as this product's stage-1 data, so the
    # caller skips a redundant second stage-1 call for it.
    lookup = keepa_client.stage1_screen(db, [ean], is_ean=True)
    result = next(iter(lookup.values()), None)
    if result is None:
        product = cache.cache_product(db, ean=ean, asin=None, title=title, matched_via="jsonld_no_match", confidence="none")
        return product, None
    product = cache.cache_product(db, ean=ean, asin=result.asin, title=result.title or title, matched_via=matched_via, confidence=confidence)
    return product, result


def _ping_unverified(db: Session, deal: models.Deal) -> None:
    embed = discord_notifier.build_unverified_embed(
        title=deal.title, retailer_url=deal.retailer_url or deal.url,
        image_url=deal.image_url, retailer=deal.retailer, buy_price_pence=deal.buy_price,
    )
    sent = discord_notifier.send_ping(get_settings().discord_webhook_url, embed)
    deal.status = "unverified_pinged" if sent else "ping_failed"
    db.commit()


def _score_and_record(
    db: Session,
    deal: models.Deal,
    product: models.Product,
    stage2: "keepa_client.Stage2Result",
    decision_cfg: DecisionConfig,
    fee_provider: FeeProvider,
) -> models.Score:
    dims = None
    if stage2.package_weight_kg and stage2.package_longest_cm and stage2.package_dims_sum_cm:
        dims = SizeDims(stage2.package_weight_kg, stage2.package_longest_cm, stage2.package_dims_sum_cm)
    price_for_fees = stage2.buybox_price_pence or stage2.lowest_fba_offer_pence or 0
    fees = fee_provider.get_fees(stage2.category or "", price_for_fees, dims)
    oversize = fee_provider.classify_size_tier(dims) == "oversize"

    score_input = ScoreInput(
        buy_price_pence=deal.buy_price,
        match_confidence=product.confidence,
        category=stage2.category or "",
        fba_offer_count=stage2.fba_offer_count,
        amazon_on_listing=stage2.amazon_on_listing,
        fees=fees,
        sales_rank=stage2.sales_rank,
        est_monthly_sales=stage2.est_monthly_sales,
        buybox_price_pence=stage2.buybox_price_pence,
        lowest_fba_offer_pence=stage2.lowest_fba_offer_pence,
        buybox_avg_90d_pence=stage2.buybox_avg_90d_pence,
        rank_history_days=stage2.rank_history_days,
        hazmat=stage2.hazmat,
        oversize=oversize,
        gated=None,   # not checked — no SP-API yet
    )
    result = score_deal(score_input, decision_cfg)

    score = models.Score(
        deal_id=deal.id,
        sell_price=result.sell_price_pence,
        fees_json=result.fees_breakdown,
        net_profit=result.net_profit_pence,
        roi=result.roi,
        rank=stage2.sales_rank,
        est_monthly_sales=stage2.est_monthly_sales,
        offer_count=stage2.fba_offer_count,
        amazon_on_listing=stage2.amazon_on_listing,
        gated=None,
        flags_json=result.flags,
        verdict=result.verdict.value,
        verdict_reason=result.verdict_reason,
    )
    db.add(score)
    db.commit()
    db.refresh(score)
    return score


class ScoreResultView:
    """Adapts a persisted Score row back to the shape discord_notifier's
    build_matched_embed() expects (a decision.engine.ScoreResult), so the
    embed builder doesn't need to know about the DB layer."""

    def __init__(self, score: models.Score):
        self.verdict = Verdict(score.verdict)
        self.verdict_reason = score.verdict_reason
        self.sell_price_pence = score.sell_price
        self.net_profit_pence = score.net_profit
        self.roi = score.roi
        self.flags = score.flags_json or []
