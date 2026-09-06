"""Loader: scans Novanex's UK trade price list (user-supplied,
"Novanex UK Price List 9.2026_Sheet1.csv") via wholesale_scan.run_scan().
4,707 rows, general household/baby/toiletries FMCG -- own-brand-leaning
lines (1001, 123 Baby, etc.) rather than the big international CPG names
(Dove, Nivea, Dettol...) Novanex advertises on their wholesale page, so
don't assume this export matches that pitch; the actual hit rate will say
which is true.

**Minimum order value: £3,000 ex VAT, stated in the file's own header.**
Not enforced by this loader or wholesale_scan.py (both score per-SKU ROI
only) -- whatever candidates come out the other end need to collectively
clear that spend threshold before this supplier is actually usable, which
is a separate check after the scan, not before.

File format quirks:
- Encoding is UTF-8 (confirmed via raw bytes: "£" is \xc2\xa3). Terminal
  output while debugging this loader *displayed* it as a replacement
  character under both utf-8 and cp1252 reads, which was a red herring --
  checking the actual in-memory codepoint (ord() == 0xa3) showed utf-8 was
  correct all along and cp1252 mangled it; the display was just lying.
- Real header is row 7 (Excel 1-indexed) / index 6 (pandas header=) -- six
  blank/title rows above it, same shape as the Pharmazon file.
- EAN must be read as dtype=str from the start. Pandas' default float64
  inference for this column corrupts long barcodes via scientific notation
  and trailing-zero drift on the way back to a string -- read it as text or
  the barcodes are silently wrong, not just formatted oddly.
- "Inner Case" / "Outer Case" / "Outer Per PLT" are packaging/ordering
  constraints (you can only order in multiples of Inner Case, ideally Outer
  Case), NOT price multipliers -- "Unit Price" is already per single
  sellable item regardless of case size. Confirmed by column naming and by
  rows like "123 BABY BATH TIME BOATS 4M+ 5 PACK" (Inner Case=1) where the
  multi-item pack is already part of the product's own identity, priced as
  one retail unit. Recorded in the note field so the report shows the real
  minimum order quantity per SKU, not just the price.
- No separate Brand column -- Description embeds it as the leading token(s)
  (e.g. "1001 300ML CARPET FRESH..." -> brand "1001", a real UK carpet-care
  brand). Approximated as the first word; imperfect but better than nothing
  for the report.

VAT: the file's own header states prices are "Ex Vat", so unlike Pharmazon
this doesn't need confirming. No per-row category to distinguish zero-rated
exceptions (children's clothing etc.) -- flat 20%, same simplifying
assumption as the original Rashmian scan. Brightside is not VAT-registered,
so this is real non-reclaimable cost where it applies.
"""
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.wholesale_scan import FeedRow, run_scan

FEED_PATH = r"C:\Users\shael\Downloads\Novanex UK Price List 9.2026_Sheet1.csv"
VAT_MULT = 1.20

# Days of shelf life a dated SKU must still have at scan time. Amazon's own
# floor for consumables is 90 days remaining at FBA receipt; 120 leaves a
# month for ordering, prep and inbound shipping on top of that. Raise it if
# inbound starts taking longer, don't drop it below 90.
_MIN_SHELF_LIFE_DAYS = 120


def load_feed(path: str) -> list[FeedRow]:
    df = pd.read_csv(path, header=6, encoding="utf-8", dtype={"EAN": str})
    ean = df["EAN"].astype(str).str.strip()
    df = df[ean.str.isdigit() & ean.str.len().between(12, 14)]
    price_col = [c for c in df.columns if c.startswith("Unit Price")][0]
    price = df[price_col].astype(str).str.replace("£", "", regex=False).str.strip()
    df = df[price.str.match(r"^\d+(\.\d+)?$")]

    # Stock guard. Every row in the 9.2026 list is >=12 so this drops
    # nothing today -- it is here because scan_ancientwisdom_feed.py had a
    # stock column it never checked and put six OutofStock SKUs on the
    # candidate list (2026-09-06). Cheap to hold the rail up before the
    # next revision of this list arrives with real zeros in it.
    units = pd.to_numeric(
        df["Units Available"].astype(str).str.replace(",", "", regex=False),
        errors="coerce",
    )
    df = df[units.fillna(0) > 0]

    # Shelf-life guard. Novanex is household/toiletries FMCG and Amazon
    # refuses consumables that arrive at FBA short-dated, so an expiry
    # inside the shipping+shelf window is a reject, not a flag: in the
    # 9.2026 list 9 rows are already expired and 53 have under 90 days
    # left. Blank EXPIRY DATE (~3,500 rows) means non-perishable, not
    # unknown -- those are kept. dayfirst=True because the column is UK
    # format; without it 03/04/2027 silently parses as 4 March.
    expiry = pd.to_datetime(df["EXPIRY DATE"], errors="coerce", dayfirst=True)
    floor = pd.Timestamp.today().normalize() + pd.Timedelta(days=_MIN_SHELF_LIFE_DAYS)
    df = df[expiry.isna() | (expiry >= floor)]

    rows: list[FeedRow] = []
    seen: set[str] = set()
    for _, r in df.iterrows():
        e = str(r["EAN"]).strip()
        if e in seen:
            continue
        seen.add(e)

        unit_price = float(str(r[price_col]).replace("£", "").strip())
        desc = str(r["Description"]).strip()
        brand = desc.split()[0] if desc else "Unknown"
        inner_case = r["Inner Case"]

        rows.append(FeedRow(
            ean=e,
            brand=brand,
            name=desc,
            buy_price_pence=round(unit_price * VAT_MULT * 100),
            list_price=unit_price,
            note=f"min order qty {inner_case}/inner case",
        ))
    return rows


if __name__ == "__main__":
    run_scan("novanex", load_feed(FEED_PATH))
