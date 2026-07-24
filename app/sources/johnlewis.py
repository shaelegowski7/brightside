"""John Lewis outlet/clearance scraper. John Lewis's storefront is Next.js
SSR, but the general "Special Offers" section is a curated CMS hub with no
inline product data (confirmed live 2026-07-24 -- its __NEXT_DATA__ only
carries category-tree metadata, no product array). The real paginated
listing is the site's actual "Outlet" section instead (an Endeca-style
faceted catalogue, confirmed via the page's own `endecaCanonical` field) --
https://www.johnlewis.com/browse/outlet/_/N-puqr ("Shop All" outlet, found
via a homepage nav link) embeds a genuine, flat `productListingData.products`
array directly in __NEXT_DATA__, at a stable page-prop path (unlike
Screwfix, where the equivalent key is a per-render hash).

GTIN IS present here -- confirmed live 2026-07-24 on a real outlet product
page's JSON-LD (`"gtin13":"5063682534824"`, nested inside the Product
node's `offers`). It is NOT present on the listing page itself, though, so
(unlike B&Q) this still needs a second per-product page fetch per new/
changed item -- same two-fetch shape as Argos/Smyths, just against the
*generic* jsonld.extract_ean() path with no retailer-specific override
needed, since John Lewis already uses the standard `gtin13` key name.

Pagination beyond page 1 is UNCONFIRMED and, unlike Smyths/Screwfix/Home
Bargains, actively appears broken rather than merely untested: the listing
page's own `<link rel="next" href="...?page=2">` was found and fetched
directly (confirmed live 2026-07-24), but returned a 404 -- tried with
session cookies carried over, a Referer header, and the Next.js internal
`/_next/data/<buildId>/...json` route directly, all 404. Real pagination
here likely only works via a client-side XHR call this app doesn't have
visibility into without a real browser. Capped at one page per
category_url (same "wrong guess degrades to under-coverage, not a crash"
contract as Smyths/Screwfix/Home Bargains) -- partially compensated for by
configuring multiple sibling category_urls (the overall "Shop All" outlet
view plus its two department sub-views) so each poll sees a different
~24-item slice rather than only ever the same 24 of ~168 total outlet
items.

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


def _parse_price_pence(value_min) -> int | None:
    try:
        return round(float(value_min) * 100)
    except (TypeError, ValueError):
        return None


def _parse_listing(html: str) -> list[_ListingProduct]:
    """Pulls productListingData.products straight out of __NEXT_DATA__ (see
    module docstring) -- no DOM scraping. Returns [] on anything unexpected
    (redesign, wrong URL) so one malformed page can't crash the whole
    crawl; the caller treats an empty page as "no products this category"."""
    match = _NEXT_DATA_RE.search(html)
    if match is None:
        return []
    try:
        data = json.loads(match.group(1))
        raw_products = data["props"]["pageProps"]["productListingData"]["products"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"[JOHNLEWIS] unexpected __NEXT_DATA__ shape: {e}")
        return []

    products = []
    for p in raw_products:
        try:
            price_pence = _parse_price_pence(p["variantPriceRange"]["value"]["min"])
            if price_pence is None:
                continue
            products.append(_ListingProduct(
                title=p["title"],
                price_pence=price_pence,
                url="https://www.johnlewis.com" + p["url"],
            ))
        except (KeyError, TypeError):
            continue
    return products


class JohnLewisAdapter:
    def __init__(self, category_urls: list[str], min_delay_s: float, max_delay_s: float):
        self.category_urls = category_urls
        self.min_delay_s = min_delay_s
        self.max_delay_s = max_delay_s

    def crawl(self, db: Session, on_deal: Callable[[RawDeal], None]) -> int:
        """One fetch per configured category_url (pagination unconfirmed --
        see module docstring), calling on_deal(raw) immediately for each
        new/changed item, same crash-safety reasoning as ArgosAdapter.crawl.
        Returns the count of deals found."""
        count = 0
        for category_url in self.category_urls:
            count += self._crawl_category(db, category_url, on_deal)
        return count

    def _crawl_category(self, db: Session, category_url: str, on_deal: Callable[[RawDeal], None]) -> int:
        self._delay()
        result = direct_fetch.fetch(category_url)
        if result is None or result[0] != 200:
            print(f"[JOHNLEWIS] {category_url}: fetch failed ({result[0] if result else 'error'}), skipping")
            return 0

        products = _parse_listing(result[1])
        if not products:
            print(f"[JOHNLEWIS] {category_url}: no products parsed (page structure changed?)")

        count = 0
        for product in products:
            if self._process_product(db, product, on_deal):
                count += 1
        return count

    def _process_product(self, db: Session, product: _ListingProduct, on_deal: Callable[[RawDeal], None]) -> bool:
        diff = crawl_state.check(db, "johnlewis", product.url, product.price_pence)
        if diff == "unchanged":
            crawl_state.record(db, "johnlewis", product.url, product.price_pence)
            return False

        self._delay()
        result = direct_fetch.fetch(product.url)
        if result is None or result[0] != 200:
            print(f"[JOHNLEWIS] {product.url}: product page fetch failed, skipping -- will retry next crawl")
            return False   # not recorded as seen -- self-heals on the next crawl

        raw = RawDeal(
            source="johnlewis", retailer="John Lewis", title=product.title,
            url=product.url, buy_price_pence=product.price_pence,
            image_url=None, html=result[1],
        )
        on_deal(raw)
        crawl_state.record(db, "johnlewis", product.url, product.price_pence)   # only mark "seen" once on_deal has run
        return True

    def _delay(self) -> None:
        time.sleep(random.uniform(self.min_delay_s, self.max_delay_s))
