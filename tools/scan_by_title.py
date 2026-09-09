"""Fallback scan for feed rows whose barcode is missing or unrepairable:
find the ASIN by product title instead, then score it exactly as a normal
scan would.

    python tools/scan_by_title.py <feed.csv> <source_name> [limit] [offset]

WHY THIS EXISTS. Cherry Cosmetics' price list (2026-09-08) arrived with 256
of 1,275 barcodes destroyed by Excel — 58 blank, and 198 truncated past the
point where zero-padding can honestly reconstruct them (see
tools/scan_pricelist_csv.py's normalise_ean). Those rows were dropped, which
left a fair question unanswered: were the profitable items in the part we
could not check? This closes that gap rather than assuming.

COST AND ACCURACY, both worse than an EAN scan — this is a fallback, not a
default. Keepa's product_finder is one call per title (an EAN batch does 100
at a time), and a title match is a guess where a barcode is an identity. So
it is capped, and every match is printed with both strings for eyeballing.
Treat a hit as "worth checking", not "confirmed".
"""
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import keepa_client
from app.config import get_config
from app.database import SessionLocal
from app.decision.engine import DecisionConfig
from app.pricing.fees import build_fee_provider
from tools.scan_pricelist_csv import _money, normalise_ean

VAT_MULT = 1.20


def rows_without_usable_ean(path: Path) -> list[dict]:
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if normalise_ean(r.get("EAN")):
                continue
            price = _money(r.get("Pack Price"))
            name = (r.get("Name") or "").strip()
            if price and price > 0 and name:
                out.append({"name": name, "price": price, "sku": r.get("SKU", "")})
    return out


def main(path: Path, source: str, limit: int, offset: int = 0) -> None:
    db = SessionLocal()
    app_cfg = get_config()
    cfg = DecisionConfig.from_app_config(app_cfg)
    fees = build_fee_provider(db, app_cfg)

    rows = rows_without_usable_ean(path)
    # Most expensive first: cheap stock cannot carry FBA fees, so if the
    # search budget runs out it should run out on the rows least likely to
    # matter. offset resumes after an earlier partial run instead of
    # re-searching (and re-paying for) titles already covered.
    rows.sort(key=lambda r: -r["price"])
    total = len(rows)
    rows = rows[offset:offset + limit]
    print(f"[TITLE] {total} rows have no usable barcode; searching "
          f"{len(rows)} of them from offset {offset}\n", flush=True)

    matched = survived = 0
    for i, r in enumerate(rows, offset + 1):
        try:
            res = keepa_client.search_by_term(db, r["name"])
        except Exception as e:                       # noqa: BLE001
            print(f"  {i:>3}. SEARCH FAILED ({type(e).__name__}) {r['name'][:48]}", flush=True)
            continue
        if not res:
            print(f"  {i:>3}. no match          £{r['price']:>6.2f}  {r['name'][:52]}", flush=True)
            continue
        matched += 1
        buy = round(r["price"] * VAT_MULT * 100)
        ok, why = keepa_client.stage1_screen_passes(res, buy, cfg, fees)
        sell = f"£{res.est_sell_price_pence/100:.2f}" if res.est_sell_price_pence else "?"
        if ok:
            survived += 1
            print(f"  {i:>3}. SURVIVES SCREEN    buy £{r['price']:.2f} sell {sell} rank {res.sales_rank}",
                  flush=True)
        else:
            print(f"  {i:>3}. rejected           buy £{r['price']:>6.2f} sell {sell:>8}  {str(why)[:40]}",
                  flush=True)
        print(f"       feed:   {r['name'][:66]}", flush=True)
        print(f"       amazon: {(res.title or '')[:66]}  {res.asin}", flush=True)

    print(f"\n[TITLE] {matched} matched an ASIN, {survived} survived the cheap screen")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: scan_by_title.py <feed.csv> <source_name> [limit] [offset]")
    main(Path(sys.argv[1]), sys.argv[2],
         int(sys.argv[3]) if len(sys.argv) > 3 else 40,
         int(sys.argv[4]) if len(sys.argv) > 4 else 0)
