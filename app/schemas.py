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
