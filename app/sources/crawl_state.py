"""Diffing helper for retailer clearance scrapers against the last crawl.
Shared across retailer modules (Argos, and later Currys/Smyths). Spec: emit
events only for a new item or a price drop — an unchanged price is not
re-emitted.

check()/record() are split (rather than one atomic diff_and_record) so
callers can defer marking an item "seen" until *after* they've successfully
acted on it — see sources/argos.py. Confirmed live 2026-07-21: with a single
atomic diff-and-write, a crawl interrupted mid-run (e.g. a deploy) left
items marked "seen" in this table with their RawDeal only ever held in
memory, silently dropped — 60 real Argos items were lost this way. Deferred
recording makes an interrupted item retry on the next crawl instead."""
import hashlib
from typing import Literal

from sqlalchemy.orm import Session

from .. import models

Diff = Literal["new", "price_drop", "unchanged"]


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def check(db: Session, retailer: str, url: str, price_pence: int) -> Diff:
    """Read-only — does not write. See record()."""
    row = db.get(models.CrawlState, (retailer, url_hash(url)))
    if row is None:
        return "new"
    return "price_drop" if price_pence < row.last_price else "unchanged"


def record(db: Session, retailer: str, url: str, price_pence: int) -> None:
    """Upserts the last-seen price/timestamp. Price *rises* still update the
    stored price (so a later drop is measured against the true last-seen
    price)."""
    h = url_hash(url)
    row = db.get(models.CrawlState, (retailer, h))
    if row is None:
        db.add(models.CrawlState(retailer=retailer, url_hash=h, last_price=price_pence, last_seen=models.utcnow()))
    else:
        row.last_price = price_pence
        row.last_seen = models.utcnow()
    db.commit()


def diff_and_record(db: Session, retailer: str, url: str, price_pence: int) -> Diff:
    """Convenience wrapper for callers that don't need interruption-safe
    deferred recording (i.e. don't call anything fallible between checking
    and recording)."""
    diff = check(db, retailer, url, price_pence)
    record(db, retailer, url, price_pence)
    return diff
