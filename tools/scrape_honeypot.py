"""Scrapes Honeypot Cosmetics' public trade catalogue to a CSV that
scan_honeypot_feed.py can load.

Honeypot (honeypotcosmetics.com, est. 1981, #23 on the 2026-09-04 wholesaler
application list) publish trade prices WITHOUT a login, and every product
record carries a barcode -- the two things a scan needs. There is also a
"Download Price List" behind a trade account, which would be one file
instead of ~100 requests; prefer that if the account exists. This exists
for the case where it doesn't yet.

PRICES ARE EX-VAT. The site states "Minimum Order is £100 + Freight + VAT",
so the displayed figure excludes VAT -- same convention as every other
trade list here, and since brightside is not VAT registered the 20% is real
non-reclaimable cash cost (applied in the loader, not here).

Politeness: 15 products per page over ~100 pages against a small
wholesaler's site, one request per second, real User-Agent. Don't lower the
delay.
"""
import csv
import re
import sys
import time
from pathlib import Path

import requests

BASE = "https://www.honeypotcosmetics.com/products_all.html"
OUT = Path(__file__).resolve().parent.parent / "data" / "honeypot_feed.csv"
# 2s, not 1s. At 1s the server closed the connection mid-handshake on page 67
# of a 100-page run (SSLEOFError, 2026-09-07) -- 66 pages is a lot of rapid
# requests for a small wholesaler and this is their shop, not an API.
DELAY_S = 2.0
TIMEOUT_S = 20
RETRIES = 3
RETRY_BACKOFF_S = 15
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

# Real markup per product (checked live 2026-09-07):
#   <a href="...-p-8810.html"><strong>NAME</strong></a>
#   Model: 740383
#   Price: <span class="productBasePrice">&pound;6.45</span>
#   ...description... Barcode:4011700740383.&nbsp;RRP &pound;14.99.
#
# Three details that broke the first attempt: the name sits inside <strong>
# (each product also has a separate image <a> to the same URL, so anchoring
# on <strong> picks the text link exactly once), "Barcode:" has NO space
# before the digits, and prices are HTML entities in the source rather than
# a literal "£".
#
# Parsed per block rather than as three independent lists, so a product
# missing a barcode can't shift every later name/price pairing by one and
# silently mis-price the rest of the page.
_BLOCK_RE = re.compile(
    r'<a[^>]+href="[^"]*-p-(\d+)\.html"[^>]*>\s*<strong>(?P<name>.{4,250}?)</strong>\s*</a>'
    r'(?P<tail>.*?)(?=<a[^>]+href="[^"]*-p-\d+\.html"[^>]*>\s*<strong>|$)',
    re.S,
)
_BARCODE_RE = re.compile(r"Barcode:\s*(\d{8,14})")
_PRICE_RE = re.compile(r'productBasePrice"[^>]*>\s*(?:&pound;|£)\s*([\d,]+\.\d{2})')
_RRP_RE = re.compile(r"RRP\s*(?:&pound;|£|.)\s*([\d,]+\.\d{2})")
_TOTAL_RE = re.compile(r"\(of\s*<strong>(\d+)</strong>\s*products\)|of\s*(\d+)\s*products")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).replace("&amp;", "&").strip()


def _load_existing() -> tuple[list[dict], set[str]]:
    """Resume support. A 100-page scrape is long enough to be interrupted --
    the first real run died on page 67 and would have thrown away 606
    products without this."""
    if not OUT.exists():
        return [], set()
    with open(OUT, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows, {r["ean"] for r in rows if r.get("ean")}


def scrape(max_pages: int = 200, start_page: int = 1) -> list[dict]:
    sess = requests.Session()
    sess.headers.update(HEADERS)
    rows, seen = ([], set()) if start_page == 1 else _load_existing()
    if rows:
        print(f"[HONEYPOT] resuming with {len(rows)} products already scraped", flush=True)
    total = None

    for page in range(start_page, max_pages + 1):
        url = BASE if page == 1 else f"{BASE}?page={page}"
        r = None
        for attempt in range(RETRIES):
            try:
                r = sess.get(url, timeout=TIMEOUT_S)
                r.raise_for_status()
                break
            except requests.RequestException as e:
                if attempt == RETRIES - 1:
                    print(f"[HONEYPOT] page {page} failed after {RETRIES} tries: {e}", flush=True)
                    break
                wait = RETRY_BACKOFF_S * (attempt + 1)
                print(f"[HONEYPOT] page {page} {type(e).__name__}, retrying in {wait}s", flush=True)
                time.sleep(wait)
                sess = requests.Session()      # the old connection is the problem
                sess.headers.update(HEADERS)
        if r is None:
            break

        if total is None:
            m = _TOTAL_RE.search(r.text)
            if m:
                total = int(m.group(1) or m.group(2))
                print(f"[HONEYPOT] catalogue reports {total} products", flush=True)

        found = 0
        for m in _BLOCK_RE.finditer(r.text):
            tail = m.group("tail")
            bc = _BARCODE_RE.search(tail)
            px = _PRICE_RE.search(tail)
            if not bc or not px:
                continue          # no barcode or no price -> nothing to match on
            ean = bc.group(1)
            if ean in seen:
                continue
            seen.add(ean)
            rrp = _RRP_RE.search(tail)
            rows.append({
                "ean": ean,
                "name": _clean(m.group("name")),
                "price_ex_vat": px.group(1).replace(",", ""),
                "rrp": rrp.group(1).replace(",", "") if rrp else "",
                "product_id": m.group(1),
            })
            found += 1

        print(f"[HONEYPOT] page {page:>3}: +{found:>2} new, total {len(rows)}", flush=True)
        if found == 0:
            print("[HONEYPOT] no new products on this page -- stopping", flush=True)
            break
        if total and len(rows) >= total:
            break
        time.sleep(DELAY_S)

    return rows


if __name__ == "__main__":
    # argv: [max_pages] [start_page].  start_page > 1 resumes from the
    # existing CSV rather than starting over.
    pages = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    start = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    rows = scrape(pages, start)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ean", "name", "price_ex_vat", "rrp", "product_id"])
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {len(rows)} products to {OUT}")
