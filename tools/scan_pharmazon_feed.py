"""Loader: scans Pharmazon Global's trade price list (user-supplied,
PHARMAZON-STORE-PRODUCTS-JULY-2026.xlsx, sent by Pharmazon directly after a
trade-account enquiry -- real trade pricing, not a public storefront price)
via wholesale_scan.run_scan(). 1,659 rows, genuine third-party branded UK
pharmacy/health stock (Vitabiotics, Seven Seas, Sudocrem, Centrum, Bio-Oil,
Corsodyl, Dulcolax, Johnson & Johnson, GSK Consumer Healthcare, ...) --
Pharmazon is an MHRA-licensed pharmaceutical wholesaler, not a generalist
mixed-goods catalog, matching the specialist-distributor/established-brand
shape that produced the sports-nutrition exception (see
brightside-branded-wholesale-dead-ends memory).

Categories present: GSL, OTC, SKIN CARE, Vitamins & Supplements, FMCG,
Personal Care, SURGICAL, SANITARY. Any GSL/OTC/Vitamins/Skincare item sold
is a medicine or medicine-adjacent product -- selling these on Amazon needs
its own category approval/compliance (MHRA batch tracking, PIL leaflets for
actual medicines), separate from and likely on top of the Human Ingestible
gate already blocking the existing sports-nutrition win. Don't treat a PASS
here as ready to order without checking that first.

VAT: confirmed with the user (2026-09-05) that "Sale price" is quoted
before VAT, standard UK trade-list convention -- no per-row tax code, unlike
the sports-nutrition list. UK VAT treatment: non-prescription medicines
(GSL/OTC), vitamins/supplements, and skincare/personal-care are all
standard-rated (20%); sanitary/period products are zero-rated since Jan
2021. So: 20% on everything except a Categories value of "SANITARY".
Brightside is not VAT-registered, so the 20% (where it applies) is real,
non-reclaimable cash cost -- same convention as scan_price_list.py.

Header row: the sheet has a title row and blank spacer rows above the real
header ("PHARMAZON - ALL PRODUCTS ON STORE" at row 4, header at row 6 in
Excel's 1-indexing) -- pandas header=5 (0-indexed) lands on the real header.

Brands column mixes a product-specific brand and a parent company
("Anusol, Church & Dwight"), HTML-entity-escaped ("&amp;"), and is blank for
~30% of rows -- unescaped and, when blank, falls back to the product Name so
every row still has something to show in the report.
"""
import html
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.wholesale_scan import FeedRow, run_scan

FEED_PATH = r"C:\Users\shael\Downloads\PHARMAZON-STORE-PRODUCTS-JULY-2026.xlsx"


def load_feed(path: str) -> list[FeedRow]:
    df = pd.read_excel(path, header=5)
    ean = df["EAN"].astype(str).str.strip()
    df = df[ean.str.isdigit() & ean.str.len().between(12, 14)]
    df = df[df["Sale price"].notna()]

    rows: list[FeedRow] = []
    seen: set[str] = set()
    for _, r in df.iterrows():
        e = str(r["EAN"]).strip()
        if e in seen:
            continue
        seen.add(e)

        price = float(r["Sale price"])
        category = str(r["Categories"]).strip() if pd.notna(r["Categories"]) else ""
        vat_mult = 1.00 if category.upper() == "SANITARY" else 1.20

        name = str(r["Name"]).strip() if pd.notna(r["Name"]) else ""
        brand_raw = r["Brands"]
        brand = html.unescape(str(brand_raw).strip()) if pd.notna(brand_raw) else (name or "Unknown")

        rows.append(FeedRow(
            ean=e,
            brand=brand,
            name=name,
            buy_price_pence=round(price * vat_mult * 100),
            list_price=price,
            note=category,
        ))
    return rows


if __name__ == "__main__":
    run_scan("pharmazon", load_feed(FEED_PATH))
