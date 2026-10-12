"""Build the rank -> monthly sales model (app/sales_model.py) from Keepa data.

Like SellerAmp's "Est. Sales": a power law per Amazon root category,
log10(sales) = a + b * log10(30-day average rank), fitted on real
"bought in past month" figures.

Amazon only shows that figure from 50 a month, so products without it are
in the sample too and counted as selling 25 (the middle of 0-49) --
fitting on badged products alone would overstate sales at slow ranks,
because at those ranks only the unusual sellers have a badge. Fits use the
median per rank band, which a censored value can't drag far.

    python tools/build_sales_model.py collect   # ~1,800 Keepa tokens; training data to the vault
    python tools/build_sales_model.py fit       # writes app/data/sales_model.json
"""
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_VAULT = Path(os.environ.get("BRIGHTSIDE_VAULT", r"C:\Users\shael\brainz\Projects\Brightside"))
TRAINING = _VAULT / "Scans" / "sales_model_training.jsonl"
MODEL = Path(__file__).resolve().parent.parent / "app" / "data" / "sales_model.json"

# Rank only means something within its root category (rank 500 in Business
# & Industry sells far less than rank 500 in Home & Garden), so each
# category is sampled on its own. Amazon UK root catIds via Keepa, 2026-10-08.
CATEGORIES = {
    3146281: "Home & Garden", 79903031: "DIY & Tools", 468292: "Toys & Games", 117332031: "Beauty",
    65801031: "Health & Personal Care", 340840031: "Pet Supplies", 318949011: "Sports & Outdoors",
    192413031: "Stationery & Office Supplies", 560798: "Electronics & Photo",
    340831031: "Computers & Accessories", 59624031: "Baby Products", 340834031: "Grocery",
    248877031: "Automotive",
}
# Log-spaced root-rank bands, each sampled at PER_BAND products.
BANDS = [(1, 300), (300, 1000), (1000, 2500), (2500, 6000), (6000, 12000), (12000, 25000),
         (25000, 50000), (50000, 100000), (100000, 180000), (180000, 300000)]
PER_BAND = 20
CENSORED_SALES = 25       # no badge = under 50 a month
MIN_POINTS = 40           # per category; fewer falls back to the all-category fit


def collect() -> None:
    from dotenv import load_dotenv
    load_dotenv()
    from app import keepa_client
    from app.database import SessionLocal
    db = SessionLocal()
    client = keepa_client._get_client()
    TRAINING.parent.mkdir(parents=True, exist_ok=True)
    done = {json.loads(l)["asin"] for l in TRAINING.open(encoding="utf-8")} if TRAINING.exists() else set()
    with TRAINING.open("a", encoding="utf-8") as out:
        for cat_id, cat_name in CATEGORIES.items():
            for lo, hi in BANDS:
                asins = keepa_client.find_asins(db, {
                    "rootCategory": [str(cat_id)], "current_SALES_gte": lo, "current_SALES_lte": hi,
                    "productType": [0], "sort": [["current_SALES", "asc"]],
                }, 50)
                asins = [a for a in asins if a not in done][:PER_BAND]
                for i in range(0, len(asins), 100):
                    batch = asins[i:i + 100]
                    tokens_before = client.tokens_left
                    products = keepa_client._query_with_retry(
                        client, batch, domain=keepa_client.KEEPA_DOMAIN, stats=30, offers=None,
                        product_code_is_asin=True, wait=True)
                    keepa_client._log_tokens(db, "sales_model", len(batch), tokens_before, client.tokens_left)
                    for p in products:
                        stats = p.get("stats") or {}
                        rank30 = keepa_client._csv_value(stats.get("avg30"), keepa_client._IDX_SALES_RANK)
                        tree = p.get("categoryTree") or []
                        if not p.get("asin") or not rank30 or not tree:
                            continue
                        out.write(json.dumps({"asin": p["asin"], "category": tree[0]["name"], "rank30": rank30,
                                              "monthly_sold": p.get("monthlySold") or None,
                                              "parent": p.get("parentAsin")}) + "\n")
                        done.add(p["asin"])
                    out.flush()
                print(f"[MODEL] {cat_name} {lo}-{hi}: {len(asins)} sampled, {len(done)} total", flush=True)


def _fit(points: list[tuple[float, float]]) -> dict | None:
    """Median sales per log-rank band, then least squares on the band medians."""
    bands: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for rank, sales in points:
        bands[round(math.log10(rank) * 4)].append((rank, sales))   # quarter-decade bands
    xs, ys = [], []
    for members in bands.values():
        if len(members) >= 3:
            xs.append(math.log10(statistics.median(r for r, _ in members)))
            ys.append(math.log10(statistics.median(s for _, s in members)))
    if len(xs) < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    return {"a": round(my - b * mx, 4), "b": round(b, 4), "n": len(points),
            "rank_min": round(10 ** min(xs)), "rank_max": round(10 ** max(xs))}


def fit() -> None:
    # Variation children are kept: their badge and rank are per child
    # (checked 2026-10-01). Categories outside CATEGORIES (Fashion above all,
    # where clothing sizes distort rank) don't feed any fit.
    wanted = set(CATEGORIES.values())
    rows = [r for r in map(json.loads, TRAINING.open(encoding="utf-8")) if r["category"] in wanted]
    by_cat: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append((r["rank30"], r["monthly_sold"] or CENSORED_SALES))
    everything = [p for pts in by_cat.values() for p in pts]
    model = {"built": f"{date.today()}", "censored_sales": CENSORED_SALES,
             "all": _fit(everything), "categories": {}}
    for cat, pts in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        if len(pts) >= MIN_POINTS and (f := _fit(pts)):
            model["categories"][cat] = f
    MODEL.parent.mkdir(parents=True, exist_ok=True)
    MODEL.write_text(json.dumps(model, indent=1), encoding="utf-8")

    from app.sales_model import estimate_with
    badged = [r for r in rows if r["monthly_sold"]]
    errs = [abs(math.log10(estimate_with(model, r["category"], r["rank30"]) / r["monthly_sold"])) for r in badged]
    within2 = sum(e <= math.log10(2) for e in errs) / len(errs) if errs else 0
    print(f"[MODEL] {len(rows)} products ({len(badged)} with a sales figure), "
          f"{len(model['categories'])} category fits + all-category fallback")
    print(f"[MODEL] on products with a real figure: {within2:.0%} estimated within 2x")
    for cat, f in model["categories"].items():
        print(f"  {cat:32} n={f['n']:4}  sales = {10 ** f['a']:.0f} x rank^{f['b']:.2f}   "
              f"(rank 1,000 -> {10 ** (f['a'] + f['b'] * 3):.0f}/mo, 10,000 -> {10 ** (f['a'] + f['b'] * 4):.0f}/mo)")
    print(f"[MODEL] wrote {MODEL}")


if __name__ == "__main__":
    {"collect": collect, "fit": fit}.get((sys.argv[1:] or [""])[0], lambda: sys.exit(__doc__))()
