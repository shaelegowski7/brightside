"""B&Q (diy.com) clearance scraper. Confirmed live 2026-07-24 against
https://www.diy.com/clearance.cat: the listing page embeds a full
schema.org ItemList directly in its own JSON-LD -- no client-side rendering,
no separate API call. Each item's `sku` field is a real EAN/GTIN barcode
(8 or 13 digits, confirmed across 48 items on one page — 47 were 13-digit
EAN-13s, one was an 8-digit EAN-8, both valid GTIN lengths), NOT B&Q's own
internal catalog ID (that only appears as the numeric suffix in the
product's own `url`, e.g. ".../590633_BQ.prd" — a different, shorter number
than the sku on the same item).

This means, unlike Argos/Smyths, no second per-product page fetch is needed
at all: title, price, url AND the EAN all come from one listing-page
request. This adapter builds a small synthetic JSON-LD snippet (same
technique as app/scan.py) to feed the existing generic jsonld.extract_ean()
path unchanged, rather than fetching each product page for nothing.

No bot protection detected (confirmed live 2026-07-24: plain requests with a
browser User-Agent succeed) -- fetches go through direct_fetch, not
scraperapi.fetch(); no SCRAPERAPI_KEY needed for this module to work.

Pagination: `?page=N` query param, confirmed working (page 2 returns 48
different items, not a repeat of page 1) -- clearance.cat alone had 851
total products across ~18 pages at the time this was checked."""
import json
import random
import re
import time
from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from . import crawl_state, direct_fetch
from .base import RawDeal

_MAX_PAGES_PER_CATEGORY = 25   # ~851 items / 48 per page ~= 18 pages observed for clearance.cat
_EAN_RE = re.compile(r"^\d{8}$|^\d{12,14}$")


@dataclass
class _ListingProduct:
    title: str
    price_pence: int
    url: str
    ean: str | None


def _page_url(category_url: str, page: int) -> str:
    if page == 1:
        return category_url
    sep = "&" if "?" in category_url else "?"
    return f"{category_url}{sep}page={page}"


def _parse_listing(html: str) -> list[_ListingProduct]:
    """Pulls the schema.org ItemList straight out of the page's own JSON-LD
    (see module docstring) -- no DOM scraping. Returns [] on anything
    unexpected (redesign, A/B variant) so one malformed page can't crash the
    whole crawl; the caller treats an empty page as "no more results"."""
    products = []
    for match in re.finditer(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("@type") != "ItemList":
            continue
        for item in data.get("itemListElement", []):
            try:
                title = item["name"]
                url = item["url"]
                price_pence = round(float(item["offers"]["price"]) * 100)
            except (KeyError, TypeError, ValueError):
                continue
            sku_digits = re.sub(r"\D", "", str(item.get("sku") or ""))
            ean = sku_digits if _EAN_RE.match(sku_digits) else None
            products.append(_ListingProduct(title=title, price_pence=price_pence, url=url, ean=ean))
    return products


def _synthetic_html(product: _ListingProduct) -> str:
    """Feeds the confirmed EAN into the existing generic jsonld.extract_ean()
    path unchanged (same technique as app/scan.py) -- no retailer-specific
    RETAILER_EXTRACTORS override needed since we already know the real GTIN
    key/value here. Empty when no valid EAN was found on this item (falls
    through to the title-search fallback, same as any other no-EAN deal)."""
    if not product.ean:
        return ""
    price_pounds = product.price_pence / 100
    return (
        '<script type="application/ld+json">'
        f'{{"@type":"Product","gtin13":"{product.ean}","offers":{{"price":"{price_pounds}"}}}}'
        "</script>"
    )


class BQAdapter:
    def __init__(self, category_urls: list[str], min_delay_s: float, max_delay_s: float):
        self.category_urls = category_urls
        self.min_delay_s = min_delay_s
        self.max_delay_s = max_delay_s

    def crawl(self, db: Session, on_deal: Callable[[RawDeal], None]) -> int:
        """Calls on_deal(raw) immediately for each new/changed item as it's
        found, same reasoning as ArgosAdapter.crawl -- a crawl-interruption
        must not lose deals already found in memory. Returns the count of
        deals found, for the caller's own logging."""
        count = 0
        for category_url in self.category_urls:
            count += self._crawl_category(db, category_url, on_deal)
        return count

    def _crawl_category(self, db: Session, category_url: str, on_deal: Callable[[RawDeal], None]) -> int:
        count = 0
        for page in range(1, _MAX_PAGES_PER_CATEGORY + 1):
            page_url = _page_url(category_url, page)
            self._delay()
            result = direct_fetch.fetch(page_url)
            if result is None or result[0] != 200:
                print(f"[BQ] {page_url}: fetch failed ({result[0] if result else 'error'}), stopping category")
                break

            products = _parse_listing(result[1])
            if not products:
                break

            for product in products:
                if self._process_product(db, product, on_deal):
                    count += 1
        return count

    def _process_product(self, db: Session, product: _ListingProduct, on_deal: Callable[[RawDeal], None]) -> bool:
        diff = crawl_state.check(db, "bq", product.url, product.price_pence)
        if diff == "unchanged":
            crawl_state.record(db, "bq", product.url, product.price_pence)
            return False

        raw = RawDeal(
            source="bq", retailer="B&Q", title=product.title,
            url=product.url, buy_price_pence=product.price_pence,
            image_url=None, html=_synthetic_html(product),
        )
        on_deal(raw)
        crawl_state.record(db, "bq", product.url, product.price_pence)
        return True

    def _delay(self) -> None:
        time.sleep(random.uniform(self.min_delay_s, self.max_delay_s))
