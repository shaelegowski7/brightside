"""Diffing helper for retailer clearance scrapers against the last crawl.
Shared across retailer modules (Argos, and later Currys/Smyths) so each one
just calls diff_and_record() per item instead of reimplementing the
crawl_state upsert. Spec: emit events only for a new item or a price drop —
an unchanged price is not re-emitted."""
import hashlib
from typing import Literal

from sqlalchemy.orm import Session

from .. import models

Diff = Literal["new", "price_drop", "unchanged"]


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def diff_and_record(db: Session, retailer: str, url: str, price_pence: int) -> Diff:
    """Compares price_pence against the last recorded crawl for this
    (retailer, url), upserts the new price/timestamp, and returns what
    changed. Price *rises* still update the stored price (so a later drop is
    measured against the true last-seen price) but are reported as
    "unchanged" — the spec only wants an emit on new items or price drops."""
    h = url_hash(url)
    row = db.get(models.CrawlState, (retailer, h))

    if row is None:
        db.add(models.CrawlState(retailer=retailer, url_hash=h, last_price=price_pence, last_seen=models.utcnow()))
        db.commit()
        return "new"

    previous_price = row.last_price
    row.last_price = price_pence
    row.last_seen = models.utcnow()
    db.commit()

    return "price_drop" if price_pence < previous_price else "unchanged"
