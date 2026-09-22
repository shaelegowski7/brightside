"""Loader: scans a UK sports-nutrition wholesaler's price list (Price_List_
September_2026.xlsx, user-supplied) via wholesale_scan.run_scan(). 5,119
unique products, 171 real brands (Optimum Nutrition, USN, MyProtein, Bulk,
Cellucor, Reflex, Quest, Animal, ...) -- a meaningfully different catalog
from Rashmian/NDA Toys: a specialist trade distributor for an established
brand category, not a generic mixed-goods wholesaler.

VAT: the file carries a per-row Tax Code (T1 = standard-rated 20%, T0 =
zero-rated), so unlike Rashmian (flat 1.20x) this applies VAT conditionally
per row -- GBP Price * 1.20 for T1, * 1.00 for T0. Brightside is not
VAT-registered, so VAT paid on a T1 purchase is real, non-reclaimable cash
cost -- same "use the inc-VAT figure" convention as nda_toys.py and the
Rashmian scan.

Barcode column is mixed-length EAN/UPC/GTIN-14 (12/13/14 digits observed)
and includes some junk (blank/whitespace, a stray 1- and 30-digit value) --
filtered to digit-only 12-14 length before matching. ~6,169 raw rows collapse
to ~4,949 unique valid barcodes after dedup.

NOTE 2026-09-02: the currently-running scan of this file was launched before
this loader (and wholesale_scan.py's checkpointing) existed -- it's on the
old uncheckpointed, un-refactored code, already loaded in a running process,
and unaffected by this file changing on disk. It will still finish and write
to price_list_candidates.txt directly. This loader takes over for any future
rerun (a resume if that process dies, or a scan of a later month's list).
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.wholesale_scan import FeedRow, run_scan

FEED_PATH = r"C:\Users\shael\.claude\uploads\a846c866-edfb-457a-afbe-6ef9e20d4f68\0dfd5ebf-Price_List_September_2026.xlsx"


def load_feed(path: str) -> list[FeedRow]:
    df = pd.read_excel(path)
    bc = df["Barcode"].astype(str).str.strip()
    df = df[bc.str.isdigit() & bc.str.len().between(12, 14)]
    df = df[df["GBP Price"].notna()]
    rows: list[FeedRow] = []
    seen: set[str] = set()
    for _, r in df.iterrows():
        ean = str(r["Barcode"]).strip()
        if ean in seen:
            continue
        seen.add(ean)
        price = float(r["GBP Price"])
        tax_code = str(r["Tax Code"]).strip()
        vat_mult = 1.20 if tax_code == "T1" else 1.00
        rows.append(FeedRow(
            ean=ean,
            brand=str(r["Brand Name"]),
            name=str(r["Product Description"]),
            buy_price_pence=round(price * vat_mult * 100),
            list_price=price,
            note=tax_code,
        ))
    return rows


if __name__ == "__main__":
    run_scan("price_list", load_feed(FEED_PATH))
