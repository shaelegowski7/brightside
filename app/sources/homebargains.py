"""Home Bargains clearance/offers scraper. www.homebargains.co.uk unconditionally
301-redirects every path (even /offers itself) to the bare short domain
https://home.bargains (confirmed live 2026-07-24 via curl -I on both) --
category_urls in config.yaml must point at home.bargains directly, not the
www.homebargains.co.uk host, or every fetch wastes a round-trip on a
redirect this app doesn't need to follow twice.

No __NEXT_DATA__/API blob for the product list (a React app, but the
listing HTML itself is fully server-rendered) -- product tiles are DOM-
scraped from a confirmed-stable `<section class="productcard">` component,
identical across the homepage's deal carousels and a dedicated category page
like /category/999/starbuys ("Star Buys" is this retailer's own term for
its featured-deals section). Price renders as split spans/comments
(`<div class="price">£<!-- -->8.99</div>`), hence the regex in
_parse_price_pence rather than a plain text pull.

GTIN is NOT present anywhere -- checked a real product page's JSON-LD
Product schema (confirmed live 2026-07-24: only id/sku/name/description/
image/brand/offers, no gtin/gtin13/ean key, and sku is just this site's own
internal UUID, not a barcode). Every deal from this source falls through to
the pipeline's title-search fallback (spec priority #2), same as Screwfix.

Pagination is UNCONFIRMED: the page embeds `currentPage`/`totalPages`
fields showing more pages exist, but a `?page=2` query param left
`currentPage` at 1 in testing (confirmed live 2026-07-24) -- the real
next-page mechanism wasn't found. Capped at one page per category_url (same
"wrong guess degrades to under-coverage, not a crash" contract as Smyths/
Screwfix) until it is.

No bot protection detected (confirmed live 2026-07-24: plain requests with a
browser User-Agent succeed) -- fetches go through direct_fetch, not
scraperapi.fetch(); no SCRAPERAPI_KEY needed for this module to work."""
import random
import re
import time
from dataclasses import dataclass
from typing import Callable

from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from . import crawl_state, direct_fetch
from .base import RawDeal

_PRICE_RE = re.compile(r"[^\d.]*?(\d+\.\d{2})")


@dataclass
class _ListingProduct:
    title: str
    price_pence: int
    url: str


def _parse_price_pence(price_tag) -> int | None:
    text = price_tag.get_text(strip=True)   # collapses the "£<!-- -->8.99" comment split into "£8.99"
    match = _PRICE_RE.search(text)
    if not match:
        return None
    return round(float(match.group(1)) * 100)


def _parse_listing(html: str) -> list[_ListingProduct]:
    """Returns [] on anything unexpected (redesign, wrong URL) so one
    malformed page can't crash the whole crawl; the caller treats an empty
    page as "no products this category"."""
    soup = BeautifulSoup(html, "html.parser")
    products = []
    seen_urls = set()
    for card in soup.find_all("section", class_="productcard"):
        link = card.find("a", href=re.compile(r"^/product/"))
        title_tag = card.find("a", class_="title")
        price_tag = card.find("div", class_="price")
        if link is None or title_tag is None or price_tag is None:
            continue
        url = "https://home.bargains" + link["href"]
        if url in seen_urls:
            continue
        price_pence = _parse_price_pence(price_tag)
        if price_pence is None:
            continue
        seen_urls.add(url)
        products.append(_ListingProduct(title=title_tag.get_text(strip=True), price_pence=price_pence, url=url))
    return products


class HomeBargainsAdapter:
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
            self._delay()
            result = direct_fetch.fetch(category_url)
            if result is None or result[0] != 200:
                print(f"[HOMEBARGAINS] {category_url}: fetch failed ({result[0] if result else 'error'}), skipping")
                continue

            products = _parse_listing(result[1])
            if not products:
                print(f"[HOMEBARGAINS] {category_url}: no products parsed (page structure changed?)")
            for product in products:
                if self._process_product(db, product, on_deal):
                    count += 1
        return count

    def _process_product(self, db: Session, product: _ListingProduct, on_deal: Callable[[RawDeal], None]) -> bool:
        diff = crawl_state.check(db, "homebargains", product.url, product.price_pence)
        if diff == "unchanged":
            crawl_state.record(db, "homebargains", product.url, product.price_pence)
            return False

        # No EAN available anywhere on this site (see module docstring) --
        # no second product-page fetch buys us anything the listing data
        # doesn't already have, so html is left empty and the pipeline falls
        # through to its title-search fallback unchanged.
        raw = RawDeal(
            source="homebargains", retailer="Home Bargains", title=product.title,
            url=product.url, buy_price_pence=product.price_pence,
            image_url=None, html="",
        )
        on_deal(raw)
        crawl_state.record(db, "homebargains", product.url, product.price_pence)
        return True

    def _delay(self) -> None:
        time.sleep(random.uniform(self.min_delay_s, self.max_delay_s))
