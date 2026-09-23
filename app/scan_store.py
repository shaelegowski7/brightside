"""Persist catalogue-scan outcomes to supplier_scan_results.

Separate from tools/wholesale_scan.py so the backfill script and the live
scan share one definition of how a checkpoint record becomes a row, and so
the mapping is testable without running a scan.

See app/models.py SupplierScanResult for why this exists at all.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .models import SupplierScanResult


def _row_from_stage2(supplier: str, rec: dict) -> dict:
    return {
        "supplier": supplier,
        "ean": rec["ean"],
        "asin": rec.get("asin"),
        "brand": rec.get("brand"),
        "name": rec.get("name"),
        "title": rec.get("title"),
        "stage": 2,
        "buy_price_pence": rec.get("buy_price_pence"),
        "units_per_sale": rec.get("units_per_sale"),
        "bundle_cost_pence": rec.get("bundle_cost_pence"),
        "sell_price_pence": rec.get("sell_price_pence"),
        "net_profit_pence": rec.get("net_profit_pence"),
        "roi": rec.get("roi"),
        "sales_rank": rec.get("sales_rank"),
        "fba_offer_count": rec.get("fba_offer_count"),
        "est_monthly_sales": rec.get("est_monthly_sales"),
        "est_monthly_sales_source": rec.get("est_monthly_sales_source"),
        "verdict": rec.get("verdict"),
        "verdict_reason": (str(rec["verdict_reason"])[:500]
                           if rec.get("verdict_reason") is not None else None),
        "flags": rec.get("flags"),
        "note": rec.get("note"),
    }


def _row_from_stage1(supplier: str, rec: dict) -> dict:
    """A stage-1-only record: either no Amazon match, or matched but
    screened out before it was worth a stage-2 lookup. Deliberately kept —
    'never seen' and 'seen and cheaply rejected' are different facts."""
    return {
        "supplier": supplier,
        "ean": rec["ean"],
        "asin": rec.get("asin"),
        "title": rec.get("title"),
        "stage": 1,
        "sell_price_pence": rec.get("est_sell_price_pence"),
        "sales_rank": rec.get("sales_rank"),
    }


def persist_scan(db: Session, supplier: str,
                 stage1: list[dict], stage2: list[dict]) -> tuple[int, int]:
    """Upsert one scan's results. Stage-2 records win over stage-1 for the
    same EAN — a fully scored product is strictly more information.

    Returns (written, skipped). Prices and verdicts are overwritten on a
    re-scan because both the supplier's quote and Amazon's buy box move;
    first_seen is preserved so the history of when a product was first
    considered survives.
    """
    scored = {r["ean"]: _row_from_stage2(supplier, r) for r in stage2 if r.get("ean")}
    payload = {}
    for r in stage1:
        ean = r.get("ean")
        if ean and ean not in scored:
            payload[ean] = _row_from_stage1(supplier, r)
    payload.update(scored)

    if not payload:
        return 0, 0

    existing = {
        e.ean: e for e in db.query(SupplierScanResult)
        .filter(SupplierScanResult.supplier == supplier,
                SupplierScanResult.ean.in_(list(payload)))
        .all()
    }
    now = datetime.now(timezone.utc)
    written = 0
    for ean, data in payload.items():
        row = existing.get(ean)
        if row is None:
            db.add(SupplierScanResult(**data, first_seen=now, last_seen=now))
        else:
            for k, v in data.items():
                setattr(row, k, v)
            row.last_seen = now
        written += 1
    db.commit()
    return written, 0
