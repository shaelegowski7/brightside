"""Screwfix clearance scraper. Screwfix's storefront is Next.js SSR, same
family as Argos, but the clearance *landing* page
(https://www.screwfix.com/landingpage/clearance) turned out to be a curated
CMS hub with tiles linking to sub-collections, not a paginated product grid
(confirmed live 2026-07-24 -- its __NEXT_DATA__ has no product array at
all, only Sitecore-style content-graph nodes). The real paginated listing is
the search-results page instead: https://www.screwfix.com/search?search=
clearancepromo (found via a homepage link, confirmed live 2026-07-24) embeds
a genuine flat `products` array with 20 items, real prices, and a "Stock
Clearance" badge on results, directly in __NEXT_DATA__ under a page-specific
component key (search props.pageProps.page.page.*.models.enrichedData —
the exact key is a per-render hash, not a stable path, hence the generic
walk in _find_enriched_data below rather than a hardcoded key).

GTIN is NOT present anywhere on this site -- checked both the search
results JSON and an individual product page's JSON-LD Product schema (only
sku/name/description/offers, no gtin/gtin13/ean/mpn key at all). Every deal
from this source falls through to the pipeline's title-search fallback
(spec priority #2), same as an HUKD post naming a product with no scraped
identifier -- lower match confidence, but a real fallback, not a dead end.

Pagination is UNCONFIRMED beyond page 1: neither `&offset=20` nor `&page=2`
advanced the result set in testing (confirmed live 2026-07-24 -- both came
back with 0 products, as if the param were treated as an unmatched facet
filter rather than real pagination), despite `totalProducts` reporting far
more than 20 are available and a `pagination: {offset, limit}` field
existing in the payload. Capped at one page per search term/category URL
(same "wrong guess degrades to under-coverage, not a crash" contract as
Smyths' unconfirmed pagination) until the real mechanism (likely a
client-side-only "load more" AJAX call) is found.

No bot protection detected (confirmed live 2026-07-24: plain requests with a
browser User-Agent succeed) -- fetches go through direct_fetch, not
scraperapi.fetch(); no SCRAPERAPI_KEY needed for this module to work."""
import json
import random
import re
import time
from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from . import crawl_state, direct_fetch
from .base import RawDeal

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


@dataclass
class _ListingProduct:
    title: str
    price_pence: int
    url: str


def _find_enriched_data(node) -> dict | None:
    """Depth-first search for the first dict carrying a `products` list --
    see module docstring on why the exact page-component key can't be
    hardcoded. Returns None if the page shape doesn't match at all
    (redesign, wrong URL) so the caller can treat it as "no products"
    rather than crash."""
    if isinstance(node, dict):
        if isinstance(node.get("products"), list):
            return node
        for value in node.values():
            found = _find_enriched_data(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_enriched_data(item)
            if found is not None:
                return found
    return None


def _parse_listing(html: str) -> list[_ListingProduct]:
    match = _NEXT_DATA_RE.search(html)
    if match is None:
        return []
    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError) as e:
        print(f"[SCREWFIX] unexpected __NEXT_DATA__ shape: {e}")
        return []

    enriched = _find_enriched_data(data.get("props", {}).get("pageProps", {}))
    if enriched is None:
        return []

    products = []
    for p in enriched["products"]:
        try:
            price_pence = round(p["priceInformation"]["currentPriceIncVat"]["amount"] * 100)
            products.append(_ListingProduct(
                title=p["longDescription"],
                price_pence=price_pence,
                url="https://www.screwfix.com" + p["detailPageUrl"],
            ))
        except (KeyError, TypeError):
            continue
    return products


class ScrewfixAdapter:
    def __init__(self, category_urls: list[str], min_delay_s: float, max_delay_s: float):
        self.category_urls = category_urls
        self.min_delay_s = min_delay_s
        self.max_delay_s = max_delay_s

    def crawl(self, db: Session, on_deal: Callable[[RawDeal], None]) -> int:
        """One fetch per configured search/category URL (pagination
        unconfirmed -- see module docstring), calling on_deal(raw)
        immediately for each new/changed item, same crash-safety reasoning
        as ArgosAdapter.crawl. Returns the count of deals found."""
        count = 0
        for category_url in self.category_urls:
            self._delay()
            result = direct_fetch.fetch(category_url)
            if result is None or result[0] != 200:
                print(f"[SCREWFIX] {category_url}: fetch failed ({result[0] if result else 'error'}), skipping")
                continue

            products = _parse_listing(result[1])
            if not products:
                print(f"[SCREWFIX] {category_url}: no products parsed (page structure changed?)")
            for product in products:
                if self._process_product(db, product, on_deal):
                    count += 1
        return count

    def _process_product(self, db: Session, product: _ListingProduct, on_deal: Callable[[RawDeal], None]) -> bool:
        diff = crawl_state.check(db, "screwfix", product.url, product.price_pence)
        if diff == "unchanged":
            crawl_state.record(db, "screwfix", product.url, product.price_pence)
            return False

        # No EAN available anywhere on this site (see module docstring) --
        # no second product-page fetch buys us anything the listing data
        # doesn't already have, so html is left empty and the pipeline falls
        # through to its title-search fallback unchanged.
        raw = RawDeal(
            source="screwfix", retailer="Screwfix", title=product.title,
            url=product.url, buy_price_pence=product.price_pence,
            image_url=None, html="",
        )
        on_deal(raw)
        crawl_state.record(db, "screwfix", product.url, product.price_pence)
        return True

    def _delay(self) -> None:
        time.sleep(random.uniform(self.min_delay_s, self.max_delay_s))
