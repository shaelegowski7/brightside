"""Loader: scans the VP vitamin/supplement trade list (data/vp_feed.csv) via
wholesale_scan.run_scan().

DIFFERENT FROM EVERY OTHER LOADER HERE: the supplier's list arrived WITHOUT
prices -- 46 SKUs of code / description / PIP code / barcode and nothing
else. So this cannot answer "does it make money", only "what could you pay
and still make money".

It therefore sets buy_price_pence to a nominal 1p. That makes every row sail
through the optimistic stage1 screen, which is the point: we want stage2's
real Amazon numbers (buy box, fees, rank, offer count) for all 46, not a
shortlist filtered on a price we do not have. The 1p also means the ROI and
net-profit columns in the report are meaningless -- read the derived max buy
price from report_max_buy.py instead, which backs the fees out of stage2 and
applies the same 30% ROI / £3 net hurdle as the real scans.

PIP codes mean this is a UK pharmacy-channel list. That matters: pharmacy
wholesale is account-gated, and account-gated lists are the only kind that
has ever produced a pass here (Tropicana, 5 passes) -- every publicly-priced
catalogue tested so far has failed, most recently Faroma, Vending Superstore
and Happy Donkey.
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.wholesale_scan import FeedRow, run_scan

FEED_PATH = Path(__file__).resolve().parent.parent / "data" / "vp_feed.csv"

# Nominal. See module docstring -- there are no supplier prices in this list.
PLACEHOLDER_BUY_PENCE = 1


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
            seen.add(ean)

            desc = " ".join(x for x in (
                (r.get("name") or "").strip(),
                (r.get("strength") or "").strip(),
                (r.get("size") or "").strip(),
            ) if x and x.upper() != "OAD")

            rows.append(FeedRow(
                ean=ean,
                brand=(r.get("code") or "VP").strip(),
                name=desc,
                buy_price_pence=PLACEHOLDER_BUY_PENCE,
                list_price=0.0,
                note=f"{r.get('code','')} PIP {r.get('pip','')} - NO SUPPLIER PRICE, max-buy only",
            ))
    return rows


if __name__ == "__main__":
    feed = load_feed(FEED_PATH)
    print(f"[VP] loaded {len(feed)} SKUs with barcodes", flush=True)
    run_scan("vp", feed)
