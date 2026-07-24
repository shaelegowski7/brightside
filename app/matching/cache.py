"""Products-cache lookups — checked before any Keepa call, including
negative results (spec: "Check the products cache before ANY Keepa call.
Cache negative search results too.")."""
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models


def get_cached_by_ean(db: Session, ean: str) -> models.Product | None:
    return db.query(models.Product).filter(models.Product.ean == ean).first()


def get_cached_by_asin(db: Session, asin: str) -> models.Product | None:
    return db.query(models.Product).filter(models.Product.asin == asin).first()


def cache_product(
    db: Session,
    *,
    ean: str | None,
    asin: str | None,
    title: str | None,
    matched_via: str,
    confidence: str,
) -> models.Product:
    """Two different EANs can resolve to the same ASIN (regional barcode
    variants, multipacks, etc. -- confirmed live 2026-07-24: products.asin's
    unique constraint caught this exact case as an uncaught IntegrityError,
    silently dropping that deal). Check-then-insert covers the common case;
    the IntegrityError fallback covers the rare true race between two
    concurrent scheduler jobs both caching a new ASIN at once. Either way,
    the existing row wins -- not worth a write for a second EAN pointing at
    an already-cached product."""
    if asin:
        existing = get_cached_by_asin(db, asin)
        if existing:
            return existing

    product = models.Product(ean=ean, asin=asin, title=title, matched_via=matched_via, confidence=confidence)
    db.add(product)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if asin:
            existing = get_cached_by_asin(db, asin)
            if existing:
                return existing
        raise
    db.refresh(product)
    return product
