"""Online-arbitrage check: for each shortlisted product you can already sell
(gating "ungated"), find the cheapest current UK retail price on Google
Shopping and compare it with the product's max buy price.

    railway run -- python tools/retail_check.py <rows.json> <out.json> [--rescore]

--rescore re-applies the matching rules to the offers already stored in
<out.json> without new searches.

<rows.json> is the merged shortlist (source_batch.py rows, with
target_buy_price_pence). Retail receipts don't ungate a brand on Amazon,
so only ungated products are checked.

Each offer's title must match the product (verify_leads' brand-anchored name
match) and its pack count must agree with the Amazon title's: a "pack of 6"
or a single gel sits next to the 30-pack in every result list (Science in
Sport BETA Fuel, 2026-10-12). Marketplaces (eBay, Amazon, OnBuy, ...) are
left out: not a retailer you can buy from with a clean receipt.

Prices are Google Shopping's, inc VAT, before delivery and cashback. The
result links route through ScraperAPI with the API key in them, so they are
never stored.
"""
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.verify_leads import _tokens, _verdict  # noqa: E402

_MARKETPLACES = re.compile(r"ebay|amazon|onbuy|aliexpress|temu|wish\b|fruugo|etsy|vinted|depop|facebook|"
                           r"cdiscount|allegro|shein|dhgate|alibaba|manomano|bol\.com|hollandbikeshop|"
                           # import resellers and second-hand: no clean UK retail receipt for new stock
                           r"u-?buy|desertcart|wafuu|japan|lmching|cex\b|music ?magpie|back ?market|"
                           r"\bused\b|refurb", re.I)
# Not the retail item itself: a sample of it, a single from a multipack, a
# booster from a tin (2026-10-12 run: 30 of 34 first-pass "buys" were these).
_NOT_THE_ITEM = re.compile(r"\b(sample|samples|sachet|tester|travel size|decant|vial|mini tin|booster|"
                           r"single|refill only|pre-?owned|used|refurbished|spare part)\b", re.I)
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(fl oz|ml|ltr|litre|liter|l|kg|g|oz)\b", re.I)
_TO_BASE = {"ml": 1, "l": 1000, "ltr": 1000, "litre": 1000, "liter": 1000, "g": 1, "kg": 1000, "oz": 28.35,
            "fl oz": 29.57}
_COUNT = re.compile(r"(?:pack of|box of|set of|pk-?|\bx)\s*(\d{1,3})\b(?!\s*(?:ml|g|l|kg|oz|cm|mm))|"
                    r"\b(\d{1,3})\s*(?:x\s*\d|-?\s*(?:pack|pk|pcs|pieces|count|ct|sachets|capsules|tablets|bags|blades|pods)\b)",
                    re.I)
_WORKERS = 5


def _count(title: str) -> int | None:
    nums = [int(a or b) for a, b in _COUNT.findall(title or "")]
    nums = [n for n in nums if 1 < n < 1000]
    return max(nums) if nums else None


def _size(title: str) -> float | None:
    """Largest per-item size in base units (ml or g)."""
    vals = [float(n) * _TO_BASE[u.lower()] for n, u in _SIZE_RE.findall(title or "")]
    return max(vals) if vals else None


def _query(title: str) -> str:
    """Brand-led product name without the marketing tail: up to the second
    ' - ' and at most 12 words."""
    parts = re.split(r"\s+[-|–]\s+", title)
    t = " ".join(parts[:2]) if len(parts) > 1 and len(parts[0].split()) < 5 else parts[0]
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9.'&+]*", t)
    return " ".join(words[:12])


def _headroom(target: int) -> int:
    """Invert candidate_finder.target_buy_price_pence (10% ROI / 80p): the
    sell-minus-costs headroom that produced this target."""
    return round(target * 1.10) if target > 800 else target + 80


def fetch(title: str, key: str) -> tuple[list[dict], str | None]:
    """Raw Google Shopping offers as {retailer, title, price_pence}; links
    dropped (they carry the API key)."""
    try:
        r = requests.get("https://api.scraperapi.com/structured/google/shopping",
                         params={"api_key": key, "query": _query(title), "country_code": "uk", "tld": "co.uk"},
                         timeout=90)
        results = (r.json().get("shopping_results") or []) if r.ok else []
        err = None if r.ok else f"http {r.status_code}"
    except (requests.RequestException, ValueError) as e:
        results, err = [], type(e).__name__
    raw = [{"retailer": it.get("source") or "", "title": (it.get("title") or "")[:200],
            "price_pence": round(float(it["extracted_price"]) * 100)}
           for it in results if it.get("extracted_price")]
    return raw, err


def evaluate(row: dict, raw: list[dict]) -> list[dict]:
    title = row.get("keepa_title") or row.get("title") or ""
    brand = row.get("brand")
    tokens, amz_count, amz_size = _tokens(title, brand), _count(title), _size(title)
    head = _headroom(row["target_buy_price_pence"])
    offers = []
    for o in raw:
        src, otitle, pence = o["retailer"], o["title"], o["price_pence"]
        if _MARKETPLACES.search(src):
            continue
        status, _ = _verdict(otitle, None, tokens, brand, brand_site=bool(brand and brand.lower() in src.lower()))
        oc, osz = _count(otitle), _size(otitle)
        why = None
        if status != "listed":
            why = "different product"
        elif _NOT_THE_ITEM.search(otitle) and not _NOT_THE_ITEM.search(title):
            why = "sample, single or part, not the item"
        elif amz_size and osz and not 0.9 <= osz / amz_size <= 1.1:
            why = f"size {osz:g} vs Amazon {amz_size:g}"
        elif (amz_count or 1) != (oc or 1):
            why = f"pack of {oc or 1}, Amazon listing is {amz_count or 1}"
        elif pence < row["buybox_price_pence"] * 0.35:
            why = "under 35% of the Amazon price: check it's the same item"
        offers.append({"retailer": src, "title": otitle, "price_pence": pence, "profit_pence": head - pence,
                       "roi": round((head - pence) / pence, 3) if pence else None,
                       "match": why is None, "why_not": why})
    return offers


def decide(row: dict, offers: list[dict]) -> tuple[str, dict | None]:
    matched = sorted([o for o in offers if o["match"]], key=lambda o: o["price_pence"])
    best = matched[0] if matched else None
    verdict = ("buy" if best and best["price_pence"] <= row["target_buy_price_pence"] else
               "close" if best and best["price_pence"] <= row["target_buy_price_pence"] * 1.15 else
               "too dear" if best else "not found in UK shops")
    return verdict, best


def check(row: dict, key: str, raw: list[dict] | None = None, err: str | None = None) -> dict:
    title = row.get("keepa_title") or row.get("title") or ""
    if raw is None:
        raw, err = fetch(title, key)
    offers = evaluate(row, raw)
    verdict, best = decide(row, offers)
    return {"asin": row["asin"], "query": _query(title), "verdict": verdict, "best": best, "raw": raw,
            "offers": sorted(offers, key=lambda o: (not o["match"], o["price_pence"]))[:12], "error": err}


def main(rows_path: Path, out_path: Path, rescore: bool = False) -> None:
    rows = [r for r in json.loads(rows_path.read_text(encoding="utf-8")) if r.get("gating") == "ungated"]
    done = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    if rescore:
        # Re-apply the matching rules to the stored offers: no credits spent.
        for r in rows:
            if r["asin"] in done:
                prev = done[r["asin"]]
                raw = prev.get("raw") or [{k: o[k] for k in ("retailer", "title", "price_pence")}
                                          for o in prev.get("offers") or []]
                done[r["asin"]] = check(r, "", raw, prev.get("error"))
        out_path.write_text(json.dumps(done, indent=1), encoding="utf-8")
        rows = []
    key = os.environ.get("SCRAPERAPI_KEY", "")
    todo = [r for r in rows if r["asin"] not in done or done[r["asin"]].get("error")]
    print(f"[RETAIL] {len(rows)} ungated products, {len(todo)} to check", flush=True)
    with ThreadPoolExecutor(_WORKERS) as pool:
        for n, res in enumerate(pool.map(lambda r: check(r, key), todo), 1):
            done[res["asin"]] = res
            if n % 10 == 0 or n == len(todo):
                out_path.write_text(json.dumps(done, indent=1), encoding="utf-8")
                print(f"[RETAIL] {n}/{len(todo)}", flush=True)
    counts: dict[str, int] = {}
    for v in done.values():
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
    print(f"[RETAIL] done: {counts}", flush=True)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 2:
        sys.exit(__doc__)
    main(Path(args[0]), Path(args[1]), rescore="--rescore" in sys.argv)
