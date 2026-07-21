"""Argos clearance scraper. Argos's storefront is Next.js SSR — the listing
page embeds a full product array (id, name, price, wasPrice) in a
`__NEXT_DATA__` script tag, so one page fetch covers up to 60 products with
no DOM/CSS scraping needed; per crawl_state, only new items or price drops
trigger a second fetch of the product page itself (needed for the EAN — see
_extract_argos_ean below). Confirmed live 2026-07-20 via ScraperAPI against
https://www.argos.co.uk/clearance/technology/c:29949/ — direct requests from
this app's Railway IP get a flat Akamai 403 on every path, even robots.txt,
so ScraperAPI (SCRAPERAPI_KEY) is required for this module to work at all.

robots.txt disallows /wishlist and /list/* for all agents; /clearance/* is
not disallowed and no Crawl-delay is specified — politeness beyond that is
our own config-driven random delay (see config.yaml's argos.*_delay_seconds).

Unlike HotUKDealsAdapter, this needs a DB session mid-crawl (to diff each
listed item against crawl_state *before* deciding whether the expensive
product-page fetch is worth it), so it doesn't implement SourceAdapter's
plain poll() — see scheduler.poll_argos_clearance for its own job wiring.
"""
import json
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

_EAN_IN_DESCRIPTION_RE = re.compile(r"\bEAN:\s*(\d{8,14})\b")
_MAX_PAGES_PER_CATEGORY = 30   # safety cap; observed categories run ~10 pages at 60 items/page


def _extract_argos_ean(html: str) -> str | None:
    """Argos embeds the EAN as free text inside the product page's Product
    JSON-LD `description` field ("...EAN: 196388561070...") rather than a
    structured gtin13 field, so the generic extractor (which only looks at
    known GTIN keys) never finds it — this is exactly why RETAILER_EXTRACTORS
    exists. Returns None (not every product's description lists one) rather
    than raising, matching the generic extractor's "give up gracefully"
    contract."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        if not tag.string:
            continue
        try:
            data = json.loads(tag.string)
        except (json.JSONDecodeError, TypeError):
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        for node in nodes if isinstance(nodes, list) else [nodes]:
            if not isinstance(node, dict) or node.get("@type") != "Product":
                continue
            match = _EAN_IN_DESCRIPTION_RE.search(node.get("description") or "")
            if match:
                return match.group(1)
    return None


jsonld.RETAILER_EXTRACTORS["www.argos.co.uk"] = _extract_argos_ean


@dataclass
class _ListingProduct:
    title: str
    price_pence: int
    url: str


def _page_url(category_url: str, page: int) -> str:
    return category_url.rstrip("/") + f"/opt/page:{page}/"


def _parse_listing(html: str) -> list[_ListingProduct]:
    """Pulls productData straight out of __NEXT_DATA__ (see module docstring)
    — no DOM scraping. Returns [] on anything unexpected (redesign, A/B test
    variant, etc.) so one malformed page can't crash the whole crawl; the
    caller treats an empty page as "no more results"."""
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag is None or not tag.string:
        return []
    try:
        data = json.loads(tag.string)
        raw_products = data["props"]["pageProps"]["productData"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"[ARGOS] unexpected __NEXT_DATA__ shape: {e}")
        return []

    products = []
    for p in raw_products:
        try:
            attrs = p["attributes"]
            products.append(_ListingProduct(
                title=attrs["name"],
                price_pence=round(attrs["price"] * 100),
                url=f"https://www.argos.co.uk/product/{p['id']}",
            ))
        except (KeyError, TypeError):
            continue
    return products


class ArgosAdapter:
    def __init__(self, category_urls: list[str], api_key: str, min_delay_s: float, max_delay_s: float):
        self.category_urls = category_urls
        self.api_key = api_key
        self.min_delay_s = min_delay_s
        self.max_delay_s = max_delay_s

    def crawl(self, db: Session, on_deal: Callable[[RawDeal], None]) -> int:
        """Calls on_deal(raw) immediately for each new/changed item as it's
        found, rather than collecting a list to hand back after the whole
        crawl finishes — a crawl can run long (10 pages x up to 60 items x
        up to max_delay_seconds each), and if the process is interrupted
        mid-crawl, any RawDeal only held in memory would be lost even
        though crawl_state had already marked those items "seen" (confirmed
        live 2026-07-21 — see crawl_state.py's module docstring). Returns
        the count of deals found, for the caller's own logging."""
        count = 0
        for category_url in self.category_urls:
            count += self._crawl_category(db, category_url, on_deal)
        return count

    def _crawl_category(self, db: Session, category_url: str, on_deal: Callable[[RawDeal], None]) -> int:
        count = 0
        for page in range(1, _MAX_PAGES_PER_CATEGORY + 1):
            page_url = category_url if page == 1 else _page_url(category_url, page)
            self._delay()
            result = scraperapi.fetch(page_url, self.api_key)
            if result is None or result[0] != 200:
                print(f"[ARGOS] {page_url}: fetch failed ({result[0] if result else 'error'}), stopping category")
                break

            products = _parse_listing(result[1])
            if not products:
                break

            for product in products:
                if self._process_product(db, product, on_deal):
                    count += 1
        return count

    def _process_product(self, db: Session, product: _ListingProduct, on_deal: Callable[[RawDeal], None]) -> bool:
        diff = crawl_state.check(db, "argos", product.url, product.price_pence)
        if diff == "unchanged":
            crawl_state.record(db, "argos", product.url, product.price_pence)
            return False

        self._delay()
        result = scraperapi.fetch(product.url, self.api_key)
        if result is None or result[0] != 200:
            print(f"[ARGOS] {product.url}: product page fetch failed, skipping — will retry next crawl")
            return False   # not recorded as seen -- self-heals on the next crawl

        raw = RawDeal(
            source="argos", retailer="Argos", title=product.title,
            url=product.url, buy_price_pence=product.price_pence,
            image_url=None, html=result[1],
        )
        on_deal(raw)
        crawl_state.record(db, "argos", product.url, product.price_pence)   # only mark "seen" once on_deal has run
        return True

    def _delay(self) -> None:
        time.sleep(random.uniform(self.min_delay_s, self.max_delay_s))
