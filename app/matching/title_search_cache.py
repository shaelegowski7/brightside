"""7-day negative-result cache (permanent for positive matches) for the
Keepa model-number/title search fallback — spec: "cache negative results
too (don't re-search the same failed title within 7 days)"."""
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .. import models

_NEGATIVE_TTL_DAYS = 7


def get_cached(db: Session, search_term: str) -> tuple[bool, str | None]:
    """Returns (cache_hit, asin). cache_hit=True means "don't call Keepa
    again" — either a permanent positive match, or a negative result still
    within its 7-day window."""
    row = db.get(models.TitleSearchCache, search_term)
    if row is None:
        return False, None
    if row.asin is not None:
        return True, row.asin

    cutoff = datetime.now(timezone.utc) - timedelta(days=_NEGATIVE_TTL_DAYS)
    searched_at = row.searched_at if row.searched_at.tzinfo else row.searched_at.replace(tzinfo=timezone.utc)
    return (True, None) if searched_at >= cutoff else (False, None)


def record(db: Session, search_term: str, asin: str | None) -> None:
    row = db.get(models.TitleSearchCache, search_term)
    if row is None:
        row = models.TitleSearchCache(search_term=search_term)
        db.add(row)
    row.asin = asin
    row.searched_at = models.utcnow()
    db.commit()
