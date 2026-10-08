"""Read-through cache for keepa_client's parsed lookups.

Two kinds of data with different shelf lives (config.yaml keepa_cache):
- market data (stage1/stage2: prices, sales, sellers, Amazon) -- max_age_days
- barcode -> ASIN matches, including "no match" -- code_map_max_age_days

A max age of 0 means "always fetch fresh"; callers pass it to force a
current read before money is spent (pipeline pings, --fresh scans).
"""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from . import models
from .config import get_config

# Tests flip this off (tests/conftest.py) so a test that queries the same
# ASIN twice with different fake payloads isn't answered from the first.
READS_ENABLED = True


def max_age(kind: str, override_days: float | None = None) -> timedelta:
    """kind: "market" or "code_map"."""
    if override_days is None:
        cfg = get_config().get("keepa_cache") or {}
        key = "code_map_max_age_days" if kind == "code_map" else "max_age_days"
        override_days = cfg.get(key, 60 if kind == "code_map" else 7)
    return timedelta(days=override_days)


def _fresh(fetched_at: datetime | None, age: timedelta) -> bool:
    if not READS_ENABLED or fetched_at is None or age <= timedelta(0):
        return False
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    return fetched_at >= datetime.now(timezone.utc) - age


def _rows(db: Session, asins: list[str]) -> dict[str, "models.KeepaCache"]:
    if not asins:
        return {}
    return {r.asin: r for r in db.query(models.KeepaCache).filter(models.KeepaCache.asin.in_(asins))}


def get(db: Session, stage: int, asins: list[str], age: timedelta, cls) -> dict:
    """Fresh cached results for `asins` as `cls` instances, keyed by ASIN."""
    out = {}
    for asin, row in _rows(db, asins).items():
        data, fetched = (row.stage1, row.stage1_fetched_at) if stage == 1 else (row.stage2, row.stage2_fetched_at)
        if data is not None and _fresh(fetched, age):
            out[asin] = cls(**data)
    return out


def put(db: Session, stage: int, results: dict) -> None:
    if not results:
        return
    now = datetime.now(timezone.utc)
    rows = _rows(db, list(results))
    for asin, result in results.items():
        row = rows.get(asin)
        if row is None:
            row = models.KeepaCache(asin=asin)
            db.add(row)
        if stage == 1:
            row.stage1, row.stage1_fetched_at = asdict(result), now
        else:
            row.stage2, row.stage2_fetched_at = asdict(result), now
    db.commit()


def get_codes(db: Session, codes: list[str], age: timedelta) -> dict[str, str | None]:
    """Fresh barcode matches: code -> ASIN, or None for a known no-match."""
    if not codes:
        return {}
    rows = db.query(models.KeepaCodeMap).filter(models.KeepaCodeMap.code.in_(codes))
    return {r.code: r.asin for r in rows if _fresh(r.fetched_at, age)}


def put_codes(db: Session, mapping: dict[str, str | None]) -> None:
    if not mapping:
        return
    now = datetime.now(timezone.utc)
    rows = {r.code: r for r in db.query(models.KeepaCodeMap).filter(models.KeepaCodeMap.code.in_(list(mapping)))}
    for code, asin in mapping.items():
        row = rows.get(code)
        if row is None:
            row = models.KeepaCodeMap(code=code)
            db.add(row)
        row.asin, row.fetched_at = asin, now
    db.commit()
