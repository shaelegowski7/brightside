"""Loader: scans Honeypot Cosmetics' trade catalogue (scraped by
tools/scrape_honeypot.py into data/honeypot_feed.csv) via
wholesale_scan.run_scan().

Honeypot are a fragrance/cosmetics wholesaler (est. 1981, #23 on the
2026-09-04 wholesaler application list). Chosen because they publish trade
prices without a login AND print a barcode on every product record -- the
two things a scan needs, and rare enough together that most suppliers
require an approved account before either is visible.

WHY A FORWARD SCAN AND NOT A CHECK AGAINST THE KEEPA SHORTLIST: the
2026-09-07 reverse scan produced 175 target ASINs spread across 142 brands,
and spot-checking Honeypot against them found essentially no overlap -- they
carry 6 Paco Rabanne lines, none of which are the two on the list, and 14
Lattafa lines whose variants differ from the 5 wanted (Asad vs Asad Elixir,
Yara Pink vs Yara Elixir). That is the expected result for any single
wholesaler against a list scattered over 142 brands. The right question for
a supplier you can actually buy from is "which of THEIR products work on
Amazon", which is what this does.

VAT: the site states "Minimum Order is £100 + Freight + VAT", so listed
prices are ex-VAT. brightside is not VAT registered, so the 20% is real
non-reclaimable cash cost -- same convention as scan_pharmazon_feed.py and
scan_price_list.py.

PACK SIZE IS IN THE PRODUCT NAME, and not in a form wholesale_scan's
_pack_multiplier recognises. Names end in "(EACH)", "(3 UNITS)",
"(6 UNITS)" and similar; _PACK_PATTERNS matches "pack of N" / "case of N" /
"N x" / "x N" / "N-pack", none of which catch "3 UNITS". Left alone, a
3-unit trade pack would be priced as though it were one sellable item --
the same class of error that put phantom 300-1500% ROIs on the Pharmazon
and Novanex reports, arriving from the supplier side instead of the Amazon
side. Caught here by dividing the pack price down to a real per-unit cost
before scoring, and the multiplier is recorded in the note so it is visible
in the report.

Minimum order: £100 + freight + VAT. Not enforced here (this scores per
SKU); whatever passes needs to collectively clear that before an order is
actually placeable.
"""
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.wholesale_scan import FeedRow, run_scan

FEED_PATH = Path(__file__).resolve().parent.parent / "data" / "honeypot_feed.csv"
VAT_MULT = 1.20

# "(3 UNITS)", "(6 UNITS)", "(12 UNIT)" -- Honeypot's own trade pack size.
# "(EACH)" means one, which is also the default when nothing is stated.
_UNITS_RE = re.compile(r"\((\d+)\s*UNITS?\)", re.I)


def _pack_units(name: str) -> int:
    m = _UNITS_RE.search(name)
    if not m:
        return 1
    n = int(m.group(1))
    return n if 1 <= n <= 100 else 1


def load_feed(path: Path) -> list[FeedRow]:
    rows: list[FeedRow] = []
    seen: set[str] = set()
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ean = (r.get("ean") or "").strip()
            if not ean.isdigit() or not 12 <= len(ean) <= 14:
                continue
            if ean in seen:
                continue
            try:
                pack_price = float(r["price_ex_vat"])
            except (KeyError, ValueError, TypeError):
                continue
            if pack_price <= 0:
                continue
            seen.add(ean)

            name = (r.get("name") or "").strip()
            units = _pack_units(name)
            unit_price = pack_price / units          # real cost of ONE sellable item

            note = f"{unit_price:.2f} ex-VAT/unit"
            if units > 1:
                note += f" (trade pack of {units} at {pack_price:.2f})"
            rrp = (r.get("rrp") or "").strip()
            if rrp:
                note += f", RRP {rrp}"

            rows.append(FeedRow(
                ean=ean,
                brand=name.split()[0] if name else "Unknown",
                name=name,
                buy_price_pence=round(unit_price * VAT_MULT * 100),
                list_price=unit_price,
                note=note,
            ))
    return rows


if __name__ == "__main__":
    run_scan("honeypot", load_feed(FEED_PATH))
