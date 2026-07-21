"""Diffing helper for restock/new-release monitors (Phase 3) — a stock
*status* transition, not a price comparison, so it's deliberately separate
from crawl_state.py (price-diffing for clearance scrapers like Argos).

check()/record() are split (rather than one atomic diff_and_record) so
callers can defer marking an item "seen" until *after* they've successfully
acted on it — see sources/pokemon_center.py. Confirmed live 2026-07-21: with
a single atomic diff-and-write, a crawl interrupted mid-run left 32 real
Pokemon Center items marked "seen" here with their RawDeal only ever held
in memory, silently dropped. Deferred recording makes an interrupted item
retry on the next crawl instead."""
import hashlib

from sqlalchemy.orm import Session

from .. import models


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def check(db: Session, retailer: str, url: str, in_stock: bool) -> bool:
    """Read-only — does not write. Returns True exactly when this counts as
    a "drop" (in stock now, wasn't last poll or never tracked). See
    record()."""
    row = db.get(models.StockState, (retailer, url_hash(url)))
    if row is None:
        return in_stock
    return in_stock and not row.in_stock


def record(db: Session, retailer: str, url: str, in_stock: bool) -> None:
    h = url_hash(url)
    row = db.get(models.StockState, (retailer, h))
    if row is None:
        db.add(models.StockState(retailer=retailer, url_hash=h, in_stock=in_stock, last_seen=models.utcnow()))
    else:
        row.in_stock = in_stock
        row.last_seen = models.utcnow()
    db.commit()


def diff_and_record(db: Session, retailer: str, url: str, in_stock: bool) -> bool:
    """Convenience wrapper for callers that don't need interruption-safe
    deferred recording (i.e. don't call anything fallible between checking
    and recording)."""
    is_drop = check(db, retailer, url, in_stock)
    record(db, retailer, url, in_stock)
    return is_drop
