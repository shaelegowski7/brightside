"""Diffing helper for restock/new-release monitors (Phase 3) — a stock
*status* transition, not a price comparison, so it's deliberately separate
from crawl_state.py (price-diffing for clearance scrapers like Argos)."""
import hashlib

from sqlalchemy.orm import Session

from .. import models


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def diff_and_record(db: Session, retailer: str, url: str, in_stock: bool) -> bool:
    """Returns True exactly when this poll counts as a "drop" — in stock now
    and wasn't (or wasn't tracked at all yet) last poll. An item that's
    still in stock from the previous poll doesn't re-emit every time."""
    h = url_hash(url)
    row = db.get(models.StockState, (retailer, h))

    if row is None:
        db.add(models.StockState(retailer=retailer, url_hash=h, in_stock=in_stock, last_seen=models.utcnow()))
        db.commit()
        return in_stock

    was_in_stock = row.in_stock
    row.in_stock = in_stock
    row.last_seen = models.utcnow()
    db.commit()
    return in_stock and not was_in_stock
