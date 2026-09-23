"""Loader: scans a generic SKU/Name/Pack Price/EAN price list via
wholesale_scan.run_scan().

Written for the 2026-09-08 clearance cosmetics list (1,275 lines of
Maybelline, L'Oreal, Essie, Rimmel, NYX at a £1.25 median), but the shape
is the common one, so it takes the path as an argument rather than hard-coding
a single supplier.

    python tools/scan_pricelist_csv.py <path.csv> [source_name]

EAN REPAIR IS THE INTERESTING PART. Only 602 of the 1,275 barcodes arrived as
13 digits. The rest had been through Excel, which treats a barcode as a number
and eats its leading zeros — so a UPC-A came out at 11 digits, an EAN-13
beginning 0 at 12. Left alone those simply fail to match anything and the
scan silently loses half the list. Zero-padding 11- and 12-digit values back
to 13 recovers 417 of them.

Values shorter than 11 digits are NOT padded: at that length the value is no
longer a mangled barcode, it is a different identifier (an internal code, or
a number that lost too much to reconstruct), and padding it would invent a
barcode that matches some unrelated product. Those rows are dropped and
counted.

VAT: prices are assumed ex-VAT, the near-universal trade convention and the
same assumption as scan_honeypot_feed.py and scan_pharmazon_feed.py. If this
supplier turns out to quote inc-VAT, everything here is 20% pessimistic —
worth confirming before rejecting the list on price.
"""
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.wholesale_scan import FeedRow, run_scan

VAT_MULT = 1.20
_MONEY_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")


def _money(s: str | None) -> float | None:
    if not s:
        return None
    m = _MONEY_RE.search(s.replace(",", ""))
    return float(m.group()) if m else None


def normalise_ean(raw: str | None) -> str | None:
    """Undo Excel's leading-zero stripping. See module docstring."""
    e = (raw or "").strip()
    if not e.isdigit():
        return None
    if len(e) == 13:
        return e
    if len(e) in (11, 12):          # UPC-A / EAN-13 that lost its leading zeros
        return e.zfill(13)
    return None                      # too short to reconstruct honestly


def load_feed(path: Path) -> list[FeedRow]:
    rows: list[FeedRow] = []
    seen: set[str] = set()
    dropped_ean = dropped_price = dupes = 0

    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            ean = normalise_ean(r.get("EAN"))
            if not ean:
                dropped_ean += 1
                continue
            price = _money(r.get("Pack Price"))
            if price is None or price <= 0:
                dropped_price += 1
                continue
            if ean in seen:
                dupes += 1
                continue
            seen.add(ean)

            name = (r.get("Name") or "").strip()
            rows.append(FeedRow(
                ean=ean,
                brand=name.split()[0] if name else "Unknown",
                name=name,
                buy_price_pence=round(price * VAT_MULT * 100),
                list_price=price,
                note=f"{r.get('SKU','')} @ {price:.2f} ex-VAT",
            ))

    print(f"[FEED] {len(rows)} usable | dropped: {dropped_ean} unusable EAN, "
          f"{dropped_price} no price, {dupes} duplicate", flush=True)
    return rows


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: scan_pricelist_csv.py <path.csv> [source_name]")
    src = Path(sys.argv[1])
    name = sys.argv[2] if len(sys.argv) > 2 else src.stem[:24]
    run_scan(name, load_feed(src))
