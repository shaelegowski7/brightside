"""Re-score a source's stored deals through the wholesale scanner without
re-crawling it -- for when the rules change (ROI, profit floor, hazmat) and
the crawl itself is the expensive part (NDA Toys needs ScraperAPI's
ultra_premium tier).

Reads deals joined to their matched products (barcode + inc-VAT buy price
as last crawled). Keepa reads go through the cache, so a rule-change rescan
of recently scanned products costs no tokens; --fresh re-reads everything.
Output goes to the vault's Scans/<date>/ folder.

    python tools/rescan_deals.py nda_toys
    python tools/rescan_deals.py nda_toys --fresh
"""
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.wholesale_scan import FeedRow, run_scan  # noqa: E402

_VAULT = Path(os.environ.get("BRIGHTSIDE_VAULT", r"C:\Users\shael\brainz\Projects\Brightside"))


def load_rows(db, source: str) -> list[FeedRow]:
    from app import models
    rows = (
        db.query(models.Deal, models.Product)
        .join(models.Product, models.Product.id == models.Deal.product_id)
        .filter(models.Deal.source == source, models.Product.ean.isnot(None), models.Deal.buy_price.isnot(None))
        .order_by(models.Deal.last_seen)
        .all()
    )
    latest: dict[str, FeedRow] = {}
    for deal, product in rows:   # oldest first, so the latest crawl wins
        latest[product.ean] = FeedRow(
            ean=product.ean, brand="", name=deal.title or product.title or "",
            buy_price_pence=deal.buy_price, list_price=deal.buy_price / 100,
            note=f"price as crawled {deal.last_seen:%d %b %Y}" if deal.last_seen else "",
        )
    return list(latest.values())


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        sys.exit(__doc__)
    from app.database import SessionLocal
    rows = load_rows(SessionLocal(), args[0])
    print(f"[RESCAN] {args[0]}: {len(rows)} stored deals with a barcode and price")
    run_scan(f"{args[0]}_rescan", rows, out_dir=_VAULT / "Scans" / f"{date.today():%Y-%m-%d}",
             max_age_days=0 if "--fresh" in sys.argv else None)
