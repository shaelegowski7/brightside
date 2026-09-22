"""Loader: scans the Rashmian wholesale feed (RashmianFeedExcel.xls) via
wholesale_scan.run_scan(). Answers "which of Rashmian's products would
actually clear brightside's ROI/profit bar on Amazon today?"

TRADE PRICE is confirmed EX-VAT (rashmian.com/conditions-of-use: "All prices
on our website are exclusive of VAT"). Brightside is not VAT-registered
(config.yaml vat_registered: false), so VAT paid on purchase is a real,
non-reclaimable cash cost -- buy_price_pence below is TRADE PRICE * 1.20, the
actual cash outlay per unit. Same "use the inc-VAT figure" convention as
app/sources/nda_toys.py.

CLI: `python scan_rashmian_feed.py [in_stock|limited|all]` -- first run only
checked exact "In Stock" and silently dropped 64 rows marked "In Stock -
Limited Stock Available", a distinct status string; the preset covers both.
Source name (and so the checkpoint/report filename prefix) is
"rashmian_<preset>" except the default "in_stock" preset, which stays plain
"rashmian" for continuity with the original run's output files.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.wholesale_scan import FeedRow, run_scan

FEED_PATH = r"C:\Users\shael\Downloads\RashmianFeedExcel.xls"

_AVAILABILITY_PRESETS = {
    "in_stock": {"In Stock"},
    "limited": {"In Stock - Limited Stock Available"},
    "all": {"In Stock", "In Stock - Limited Stock Available"},
}


def load_feed(path: str, availability_values: set[str]) -> list[FeedRow]:
    df = pd.read_excel(path)
    df = df[df["Availability"].isin(availability_values)]
    df = df[df["Barcode"].notna() & df["TRADE PRICE"].notna()]
    rows: list[FeedRow] = []
    seen: set[str] = set()
    for _, r in df.iterrows():
        ean = str(int(r["Barcode"]))
        if len(ean) not in (12, 13) or ean in seen:
            continue
        seen.add(ean)
        trade_price = float(r["TRADE PRICE"])
        multiple = r["Sold in Multiples of"]
        multiple = int(multiple) if pd.notna(multiple) else 1
        note = f"{trade_price:.2f} ex-VAT" + (f", x{multiple} multiple" if multiple != 1 else "")
        rows.append(FeedRow(
            ean=ean,
            brand=str(r["Brand"]),
            name=str(r["Product Name"]),
            buy_price_pence=round(trade_price * 1.20 * 100),
            list_price=trade_price,
            note=note,
        ))
    return rows


if __name__ == "__main__":
    preset = sys.argv[1] if len(sys.argv) > 1 else "in_stock"
    values = _AVAILABILITY_PRESETS[preset]
    source_name = "rashmian" if preset == "in_stock" else f"rashmian_{preset}"
    run_scan(source_name, load_feed(FEED_PATH, values))
