"""Smyths Toys UK clearance/sale scraper. Smyths' storefront is Nuxt 3 (Vue
SSR) -- NOT Next.js like Argos. Confirmed via Wayback Machine archive
snapshots (no SCRAPERAPI_KEY was available in the environment this was
written in to test live against the real site -- see CAVEATS below).
Product tiles render directly into server-rendered HTML as:

    <a href="/uk/en-gb/<category-path>/<slug>/p/<id>">
      ...<div data-test="card-title"><h3>Title</h3></div>...
      ...<span class="text-price-lg">109</span>...<span class="notranslate">.99</span>...
    </a>

confirmed identical across two different page types (a department hub page
and homepage carousels), so this is treated as a site-wide "product tile"
component -- presumed, but not directly confirmed, to also be what the real
sale/clearance listing page uses.

GTIN is NOT in the page's JSON-LD Product schema (checked on 2 independent
product pages -- it only has sku/name/description/image/brand/offers/
aggregateRating, no gtin/gtin13/ean key at all). The only structured GTIN
found is a `data-loadbee-gtin="<13-digit-EAN>"` attribute from an embedded
third-party (Loadbee) product-content widget, present and valid on both
product pages checked -- see _extract_smyths_ean below.

CAVEATS (could not be resolved without live ScraperAPI access):
- The real "browse all sale items" URL is UNCONFIRMED. /uk/en-gb/sale
  itself always returned Smyths' Incapsula bot-challenge page in this
  environment -- both fetched directly and via the Wayback Machine's own
  crawler (4 of 6 fetch attempts across the whole site hit this same
  block) -- so its actual content was never observed. /uk/en-gb/outdoor was
  used as a structural stand-in instead (a real snapshot was available)
  and turned out to be a curated CMS landing page (~20 featured tiles, no
  pagination markers at all), not a deep paginated catalog -- the true sale
  page may have the same limited-item shape, or may be a different URL
  entirely. `enabled: false` in config.yaml until verified live.
- Pagination is UNCONFIRMED. No pagination UI/state (page numbers, rel=next,
  "load more", currentPage/totalPages fields) was found in any archived
  snapshot. _page_url() below guesses a `?page=N` query param (common
  convention) -- if wrong, the crawl just sees 0 products on page 2 and
  stops after page 1 (same "empty page == no more results" contract as
  argos.py), so a wrong guess degrades to under-coverage, not a crash.
- Whether ScraperAPI's standard proxy pool can bypass Smyths' Incapsula/
  Imperva protection at all is unverified. Imperva does browser
  fingerprinting/JS challenges, a harder class of block than Argos's
  IP-based Akamai block that ScraperAPI is confirmed to handle -- may
  require ScraperAPI's premium/JS-render tier (higher cost per request).
"""
import random
import re
import time
from dataclasses import dataclass
from typing import Callable

from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from . import crawl_state, scraperapi
from .base import RawDeal
from ..matching import jsonld

_PRODUCT_URL_RE = re.compile(r"^/uk/en-gb/[a-zA-Z0-9%/_-]+/p/\d+")
_LOADBEE_GTIN_RE = re.compile(r'data-loadbee-gtin="(\d{8}|\d{12,14})"')
_MAX_PAGES_PER_CATEGORY = 10  # conservative cap -- pagination scheme unconfirmed, see module docstring


def _extract_smyths_ean(html: str) -> str | None:
    """Smyths' JSON-LD Product schema has no gtin/gtin13/ean field (confirmed
    on 2 independent product pages) -- the only structured GTIN is this
    third-party Loadbee widget attribute. Returns None (not every product is
    guaranteed to carry one) rather than raising, matching the generic
    extractor's "give up gracefully" contract."""
    match = _LOADBEE_GTIN_RE.search(html)
    return match.group(1) if match else None


jsonld.RETAILER_EXTRACTORS["www.smythstoys.com"] = _extract_smyths_ean


@dataclass
class _ListingProduct:
    title: str
    price_pence: int
    url: str


def _page_url(category_url: str, page: int) -> str:
    if page == 1:
        return category_url
    sep = "&" if "?" in category_url else "?"
    return f"{category_url}{sep}page={page}"


def _parse_price_pence(tile_html: str) -> int | None:
    """Price renders as split spans, e.g. <span class="text-price-lg">109</span>
    ...<span class="notranslate">.99</span> (confirmed live in an archived
    snapshot). Falls back to a plain £X.XX regex for resilience against
    template variations not seen in the pages checked."""
    whole = re.search(r'class="text-price-lg">(\d+)<', tile_html)
    frac = re.search(r'class="notranslate">\.(\d{2})<', tile_html)
    if whole and frac:
        return int(whole.group(1)) * 100 + int(frac.group(1))
    plain = re.search(r"£\s?(\d+)\.(\d{2})", tile_html)
    if plain:
        return int(plain.group(1)) * 100 + int(plain.group(2))
    return None


def _parse_listing(html: str) -> list[_ListingProduct]:
    """No __NEXT_DATA__-equivalent product array is exposed on listing pages
    -- Smyths' __NUXT_DATA__ carries CMS page-builder content (banners, nav,
    rich text), not a flat product list (confirmed on an archived department
    page). Instead scans for the product-tile anchor pattern confirmed
    identical across every page type observed. Returns [] on anything
    unexpected (redesign, A/B variant, wrong URL entirely) so one malformed
    page can't crash the whole crawl; the caller treats an empty page as "no
    more results"."""
    soup = BeautifulSoup(html, "html.parser")
    products = []
    seen_urls = set()
    for a in soup.find_all("a", href=_PRODUCT_URL_RE):
        href = a["href"]
        if href in seen_urls:
            continue
        title_tag = a.find(attrs={"data-test": "card-title"})
        if title_tag is None:
            continue
        price_pence = _parse_price_pence(str(a))
        if price_pence is None:
            continue
        seen_urls.add(href)
        products.append(_ListingProduct(
            title=title_tag.get_text(strip=True),
            price_pence=price_pence,
            url="https://www.smythstoys.com" + href,
        ))
    return products


class SmythsAdapter:
    def __init__(self, category_urls: list[str], api_key: str, min_delay_s: float, max_delay_s: float):
        self.category_urls = category_urls
        self.api_key = api_key
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
            result = scraperapi.fetch(page_url, self.api_key)
            if result is None or result[0] != 200:
                print(f"[SMYTHS] {page_url}: fetch failed ({result[0] if result else 'error'}), stopping category")
                break

            products = _parse_listing(result[1])
            if not products:
                break

            for product in products:
                if self._process_product(db, product, on_deal):
                    count += 1
        return count

    def _process_product(self, db: Session, product: _ListingProduct, on_deal: Callable[[RawDeal], None]) -> bool:
        diff = crawl_state.check(db, "smyths", product.url, product.price_pence)
        if diff == "unchanged":
            crawl_state.record(db, "smyths", product.url, product.price_pence)
            return False

        self._delay()
        result = scraperapi.fetch(product.url, self.api_key)
        if result is None or result[0] != 200:
            print(f"[SMYTHS] {product.url}: product page fetch failed, skipping -- will retry next crawl")
            return False  # not recorded as seen -- self-heals on the next crawl

        raw = RawDeal(
            source="smyths", retailer="Smyths Toys", title=product.title,
            url=product.url, buy_price_pence=product.price_pence,
            image_url=None, html=result[1],
        )
        on_deal(raw)
        crawl_state.record(db, "smyths", product.url, product.price_pence)  # only mark "seen" once on_deal has run
        return True

    def _delay(self) -> None:
        time.sleep(random.uniform(self.min_delay_s, self.max_delay_s))
