"""Manual purchase/outcome logging (spec phase 3) -- app/models.py's
Purchase/Outcome tables are the feedback loop that lets category rank
thresholds and fee estimates eventually be tuned against realised
sell-through instead of staying permanent guesses (see config.yaml's own
"tune against realised sell-through once scores/outcomes data exists"
comment). A user only ever sees an ASIN (Discord embed links, Keepa/Amazon
URLs) -- never a raw score_id -- so purchases are logged by ASIN and
resolved to the most recent Score server-side."""
from datetime import datetime

from sqlalchemy.orm import Session

from . import models


class NoScoreFoundError(Exception):
    pass


class PurchaseNotFoundError(Exception):
    pass


class OutcomeAlreadyExistsError(Exception):
    pass


def resolve_latest_score_for_asin(db: Session, asin: str) -> models.Score | None:
    """products.asin -> deals.product_id -> scores.deal_id, most recent by
    ts. A product can resurface via more than one Deal row (different
    sources/re-pings), so this joins through Deal rather than assuming a
    1:1 product-to-deal relationship."""
    return (
        db.query(models.Score)
        .join(models.Deal, models.Score.deal_id == models.Deal.id)
        .join(models.Product, models.Deal.product_id == models.Product.id)
        .filter(models.Product.asin == asin)
        .order_by(models.Score.ts.desc())
        .first()
    )


def log_purchase(db: Session, asin: str, qty: int, actual_buy_price: int, notes: str | None) -> models.Purchase:
    score = resolve_latest_score_for_asin(db, asin)
    if score is None:
        raise NoScoreFoundError(f"no score found for asin {asin!r}")
    purchase = models.Purchase(score_id=score.id, qty=qty, actual_buy_price=actual_buy_price, notes=notes)
    db.add(purchase)
    db.commit()
    db.refresh(purchase)
    return purchase


def log_outcome(
    db: Session, purchase_id: int, sold_price: int, sold_date: datetime, actual_fees: int | None, notes: str | None
) -> models.Outcome:
    purchase = db.get(models.Purchase, purchase_id)
    if purchase is None:
        raise PurchaseNotFoundError(f"no purchase {purchase_id}")
    if db.get(models.Outcome, purchase_id) is not None:
        raise OutcomeAlreadyExistsError(f"purchase {purchase_id} already has an outcome logged")
    outcome = models.Outcome(
        purchase_id=purchase_id, sold_price=sold_price, sold_date=sold_date, actual_fees=actual_fees, notes=notes,
    )
    db.add(outcome)
    db.commit()
    db.refresh(outcome)
    return outcome
