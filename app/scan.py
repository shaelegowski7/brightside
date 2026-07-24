"""Phase 2 /scan support: builds a synthetic RawDeal carrying an already-
known EAN as fabricated JSON-LD (so process_deal()'s existing
jsonld.extract_ean() path picks it up unchanged, traced against the real
code -- see app/pipeline.py's _SKIP_TITLE_VALIDATION_SOURCES docstring for
the related title-validation caveat this required), runs it through
process_deal() completely unmodified, then re-queries the Deal it's
guaranteed to have created (_upsert_deal always creates-or-updates first)
plus its latest Score to build the verdict the PWA needs. process_deal()
itself still returns None by design (DB-mutation-only) -- deliberately not
changed here, to avoid touching its ~10 early-return sites for marginal
call-site benefit."""
from uuid import uuid4

from sqlalchemy.orm import Session

from . import models, pipeline
from .decision.engine import DecisionConfig
from .pricing.fees import FeeProvider
from .sources.base import RawDeal

_STATUS_REASONS = {
    "no_ean_match": "Could not match this barcode to an Amazon product",
    "title_mismatch": "Matched product looks wrong for this scan",
    "price_sanity_reject": "Buy price looked implausible",
    "fetch_blocked": "Lookup was blocked, try again",
    "unresolvable": "Could not process this scan",
    "stage1_rejected": "Failed an early profitability or rank screen",
}


def run_scan(
    db: Session, ean: str, buy_price_pence: int,
    decision_cfg: DecisionConfig, fee_provider: FeeProvider, app_cfg: dict,
) -> dict:
    scan_url = f"scan:{ean}:{uuid4().hex}"
    synthetic_html = f'<script type="application/ld+json">{{"@type":"Product","gtin13":"{ean}"}}</script>'
    raw = RawDeal(
        source="scan", retailer=None, title=f"Scanned item (EAN {ean})",
        url=scan_url, buy_price_pence=buy_price_pence, image_url=None, html=synthetic_html,
    )
    pipeline.process_deal(db, raw, decision_cfg, fee_provider, app_cfg)

    deal = db.query(models.Deal).filter(models.Deal.url == scan_url).first()   # guaranteed to exist
    score = (
        db.query(models.Score).filter(models.Score.deal_id == deal.id).order_by(models.Score.id.desc()).first()
        if deal is not None else None
    )
    return _build_result(db, deal, score, buy_price_pence)


def _build_result(db: Session, deal: models.Deal | None, score: models.Score | None, buy_price_pence: int) -> dict:
    product = db.get(models.Product, deal.product_id) if deal and deal.product_id else None
    asin = product.asin if product else None
    keepa_url = f"https://keepa.com/#!product/2-{asin}" if asin else None
    amazon_url = f"https://www.amazon.co.uk/dp/{asin}" if asin else None

    if score is None:
        status = deal.status if deal else "unresolvable"
        reason = _STATUS_REASONS.get(status, f"Not a match ({status})")
        return {
            "verdict": "REJECT",
            "reasons": [reason],
            "flags": [],
            "asin": asin,
            "match_confidence": product.confidence if product else None,
            "buy_price_pence": buy_price_pence,
            "sell_price_pence": None,
            "net_profit_pence": None,
            "roi": None,
            "posted_to_discord": False,
            "keepa_url": keepa_url,
            "amazon_url": amazon_url,
        }

    result = pipeline.ScoreResultView(score)
    return {
        "verdict": result.verdict.value,
        "reasons": [result.verdict_reason] if result.verdict_reason else [],
        "flags": result.flags,
        "asin": asin,
        "match_confidence": product.confidence if product else None,
        "buy_price_pence": buy_price_pence,
        "sell_price_pence": result.sell_price_pence,
        "net_profit_pence": result.net_profit_pence,
        "roi": result.roi,
        "posted_to_discord": deal.status == "pinged",
        "keepa_url": keepa_url,
        "amazon_url": amazon_url,
    }
