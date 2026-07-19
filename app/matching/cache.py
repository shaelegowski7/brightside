"""Products-cache lookups — checked before any Keepa call, including
negative results (spec: "Check the products cache before ANY Keepa call.
Cache negative search results too.")."""
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
    product = models.Product(ean=ean, asin=asin, title=title, matched_via=matched_via, confidence=confidence)
    db.add(product)
    db.commit()
    db.refresh(product)
    return product
