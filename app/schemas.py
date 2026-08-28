"""Pydantic request/response models for write endpoints, kept separate from
main.py so routes stay thin."""
from datetime import datetime

from pydantic import BaseModel


class PurchaseCreate(BaseModel):
    asin: str
    qty: int
    actual_buy_price: int   # pence, per unit
    notes: str | None = None


class OutcomeCreate(BaseModel):
    purchase_id: int
    sold_price: int   # pence, per unit
    sold_date: datetime
    actual_fees: int | None = None   # pence
    notes: str | None = None


class ScanRequest(BaseModel):
    ean: str
    buy_price: int   # pence


class CrawlTriggerRequest(BaseModel):
    # None (the default, and what an empty POST body sends) means "every
    # enabled source", same as the pre-existing behaviour -- selecting a
    # subset is opt-in, not required, so the PWA's plain "run crawl now"
    # button keeps working unchanged. Keys match crawl_runner._SOURCES.
    sources: list[str] | None = None


class ScanResponse(BaseModel):
    verdict: str
    reasons: list[str] = []
    flags: list[str] = []
    asin: str | None = None
    match_confidence: str | None = None
    buy_price_pence: int
    sell_price_pence: int | None = None
    net_profit_pence: int | None = None
    roi: float | None = None
    posted_to_discord: bool
    keepa_url: str | None = None
    amazon_url: str | None = None


class ConfirmedDeal(BaseModel):
    """JSON twin of dashboard.DealRow -- powers the PWA's confirmed-deals
    view, same underlying query as the /deals HTML dashboard."""
    title: str
    retailer: str | None = None
    retailer_url: str | None = None
    asin: str | None = None
    match_confidence: str | None = None
    buy_price_pence: int
    sell_price_pence: int | None = None
    net_profit_pence: int | None = None
    roi: float | None = None
    est_monthly_sales: float | None = None
    verdict: str
    flags: list[str] = []
    ts: datetime | None = None
    amazon_url: str | None = None
