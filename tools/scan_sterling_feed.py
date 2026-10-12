"""Loader: scans Sterling Trading Group's trade price list (account approved
2026-10-10) via wholesale_scan.run_scan().

    railway run -- python tools/scan_sterling_feed.py <STG-price-list.csv> [out_dir]

Columns: Barcode, Brand, Product, Type, Size, For, Availability,
"Trade price (ex VAT)", Page. 5,619 rows on the 2026-10-10 list, mostly
fragrance plus salon hair care (Wella, Goldwell, Schwarzkopf).

VAT: prices are ex-VAT and Brightside isn't VAT-registered, so x1.20.

Barcodes have been through Excel (706 at 12 digits, 397 at 11), so they go
through scan_pricelist_csv.normalise_ean's zero-padding. The 41 at 8-10
digits are dropped by the same rule.

Availability is NOT filtered: 4,450 of 5,619 rows say "Register interest"
and only 115 "In stock", so filtering would throw away nearly the whole
list. It rides along in the note instead, so a pass shows whether it can
actually be ordered today.
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.scan_pricelist_csv import _money, normalise_ean  # noqa: E402
from tools.wholesale_scan import FeedRow, run_scan  # noqa: E402

VAT_MULT = 1.20


def load_feed(path: Path) -> list[FeedRow]:
    rows: list[FeedRow] = []
    seen: set[str] = set()
    dropped_ean = dropped_price = dupes = 0
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        for r in csv.DictReader(f):
            ean = normalise_ean(r.get("Barcode"))
            if not ean:
                dropped_ean += 1
                continue
            price = _money(r.get("Trade price (ex VAT)"))
            if price is None or price <= 0:
                dropped_price += 1
                continue
            if ean in seen:
                dupes += 1
                continue
            seen.add(ean)
            rows.append(FeedRow(
                ean=ean,
                brand=(r.get("Brand") or "Unknown").strip(),
                name=(r.get("Product") or "").strip(),
                buy_price_pence=round(price * VAT_MULT * 100),
                list_price=price,
                note=f"{(r.get('Availability') or '?').strip()} @ {price:.2f} ex-VAT",
            ))
    print(f"[FEED] {len(rows)} usable | dropped: {dropped_ean} unusable EAN, "
          f"{dropped_price} no price, {dupes} duplicate", flush=True)
    return rows


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    run_scan("sterling", load_feed(Path(sys.argv[1])),
             out_dir=Path(sys.argv[2]) if len(sys.argv) > 2 else None)
