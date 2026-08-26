"""Query backing the confirmed-deals view (Score.verdict in
PASS/PASS_WITH_FLAGS) -- the full set of everything the pipeline has
actually financially vetted, not just whatever got pinged to Discord
(cooldown/ping failures are excluded there but not here -- a verdict is
"confirmed good" the moment scoring says so, independent of notification
plumbing). Used by GET /deals.json (see main.py), which the PWA's "Green
Deals" tab renders -- this module used to also render a standalone HTML
dashboard at GET /deals, retired once that tab got real Supabase login."""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models

_GOOD_VERDICTS = ("PASS", "PASS_WITH_FLAGS")


@dataclass
class DealRow:
    title: str
    retailer: str | None
    retailer_url: str | None
    asin: str | None
    match_confidence: str | None
    buy_price_pence: int
    sell_price_pence: int | None
    net_profit_pence: int | None
    roi: float | None
    est_monthly_sales: float | None
    verdict: str
    flags: list[str]
    ts: datetime | None


def get_confirmed_deals(db: Session, limit: int = 300) -> list[DealRow]:
    """Latest Score per deal (a deal can be re-scored across price changes
    -- see pipeline.py), filtered to genuinely good verdicts, newest first."""
    latest_score_ids = db.query(func.max(models.Score.id)).group_by(models.Score.deal_id)
    rows = (
        db.query(models.Score, models.Deal, models.Product)
        .join(models.Deal, models.Score.deal_id == models.Deal.id)
        .outerjoin(models.Product, models.Deal.product_id == models.Product.id)
        .filter(models.Score.id.in_(latest_score_ids.subquery().select()))
        .filter(models.Score.verdict.in_(_GOOD_VERDICTS))
        .order_by(models.Score.ts.desc())
        .limit(limit)
        .all()
    )
    out = []
    for score, deal, product in rows:
        # A scan's retailer_url is the synthetic "scan:<ean>:<uuid>" dedup
        # key (see app/scan.py), not a real link -- same caveat pipeline.py
        # already works around for the Discord embed.
        retailer_url = None if deal.source == "scan" else (deal.retailer_url or deal.url)
        out.append(DealRow(
            title=(product.title if product and product.title else deal.title),
            retailer=deal.retailer,
            retailer_url=retailer_url,
            asin=product.asin if product else None,
            match_confidence=product.confidence if product else None,
            buy_price_pence=deal.buy_price,
            sell_price_pence=score.sell_price,
            net_profit_pence=score.net_profit,
            roi=score.roi,
            est_monthly_sales=score.est_monthly_sales,
            verdict=score.verdict,
            flags=score.flags_json or [],
            ts=score.ts,
        ))
    return out
