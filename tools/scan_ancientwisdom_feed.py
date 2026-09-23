"""Loader: scans Ancient Wisdom's live product data feed
(https://www.ancientwisdom.biz/data-feed.csv, public/unauthenticated despite
being served from behind the trade-account login) via wholesale_scan.run_scan().
~11,800 rows, giftware/home-fragrance/candles specialist -- the trade account
was created and instantly approved 2026-09-05 (see wholesale_applications
tracker), so this is the first real catalog check for that supplier.

PRICE IS PER OUTER (case), NOT PER UNIT: "Price" is the cost for a whole
outer of "Units per outer" identical units under one Barcode -- e.g. Price
8.77 for 12 units of one Simmering Granules scent is 73p/unit. The feed
already does this division for us in "Unit price", so that column (not
"Price") is the real per-unit cash cost and what buy_price_pence is built
from. Assumption, not confirmed against a physical unit: the Barcode is the
EAN printed on each individual retail unit (what would appear on an Amazon
listing), not a separate case-level barcode -- re-check if match rates look
implausibly low.

No Brand column in this feed (Ancient Wisdom's own giftware lines rather
than reselling third-party brands) -- "Family" (the named product
collection, e.g. "Vintage Style Boxes") is used as the closest analogue,
informational only, doesn't affect scoring.

VAT: Ancient Wisdom is VAT-registered (GB764298589, seen in their footer),
and B2B trade prices are quoted ex-VAT by market convention -- same "not
VAT-registered so VAT paid is real cash cost" treatment as every other
loader here (buy_price_pence = ex-VAT unit price * 1.20).

Availability: Status == "Active", "For sale" == "Yes", AND Stock !=
"OutofStock". The For-sale flag alone is NOT sufficient, contrary to what
this docstring claimed until 2026-09-06: it is a listing flag, not a stock
flag. In the 2026-09-04 feed 755 rows are For sale=Yes *and* OutofStock --
including every colour of Bulk Solid Colour Dinner Candles, which is how
six unbuyable SKUs reached the candidate list and got recommended as a
first order. "Available Quantity" is NaN on those rows too, so it cannot
stand in for the check either. Remaining Stock values (Normal, Low,
VeryLow, Discontinuing) are all purchasable; Discontinuing is kept but
noted, since a line being wound down is a poor base for a repeatable
wholesale listing even when it is in stock today.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.wholesale_scan import FeedRow, run_scan

FEED_PATH = Path(__file__).resolve().parent.parent / "data" / "ancientwisdom_feed.csv"


def load_feed(path: Path) -> list[FeedRow]:
    df = pd.read_csv(path, dtype=str)
    df = df[(df["Status"] == "Active") & (df["For sale"] == "Yes")]
    df = df[df["Stock"] != "OutofStock"]   # see Availability in the module docstring
    bc = df["Barcode"].astype(str).str.strip()
    df = df[bc.str.isdigit() & bc.str.len().between(12, 14)]
    df = df[df["Unit price"].notna()]

    rows: list[FeedRow] = []
    seen: set[str] = set()
    for _, r in df.iterrows():
        ean = str(r["Barcode"]).strip()
        if ean in seen:
            continue
        seen.add(ean)
        unit_price = float(r["Unit price"])
        outer_price = r.get("Price")
        units_per_outer = r.get("Units per outer")
        stock = str(r.get("Stock") or "")
        note = f"{unit_price:.2f} ex-VAT/unit (outer GBP{outer_price} for {units_per_outer})"
        if stock != "Normal":
            note += f", stock={stock}"
        rows.append(FeedRow(
            ean=ean,
            brand=str(r.get("Family") or ""),
            name=str(r.get("Unit Name") or ""),
            buy_price_pence=round(unit_price * 1.20 * 100),
            list_price=unit_price,
            note=note,
        ))
    return rows


if __name__ == "__main__":
    run_scan("ancientwisdom", load_feed(FEED_PATH))
