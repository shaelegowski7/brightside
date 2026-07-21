"""Pokemon Center UK restock/new-release monitor (Phase 3). Different
signal from the Argos-style clearance scraper: a "drop" is a stock-status
transition to available (see stock_state.py), not a price change — buy_price
here is the item's normal RRP, not a clearance discount. Confirmed live
2026-07-21 via ScraperAPI against
https://www.pokemoncenter.com/en-gb/category/new-releases — direct requests
from this app's Railway IP get a flat 403, same as Argos.

No EAN/GTIN anywhere on this site — checked listing-page JSON-LD, product-page
JSON-LD, and raw product-page HTML text (gtin/EAN/UPC/barcode: none found).
Only their own `mpn` and the product title exist as identifiers. Per project
decision 2026-07-21, no Keepa title-search fallback was built for this, so
every drop pipelines through pipeline.py's existing no-EAN/no-ASIN path and
pings as "UNVERIFIED MATCH — check manually", with no ROI/price scoring.

robots.txt disallows their internal AJAX endpoints (/availabilities,
/prices, /items, etc., no Crawl-delay given) — deliberately not used; only
the category listing page and individual product pages are fetched, same
as a human browsing the site would load them. The listing page is a
Next.js app but __NEXT_DATA__.pageProps is empty (that data comes from a
disallowed AJAX call), so per-tile JSON-LD is the only usable structured
source for the listing — and it's occasionally low-quality on some SKUs
(one observed listing tile mislabeled priceCurrency as USD with blank
sku/image), hence _product_page_details() re-derives price/image from the
product page itself rather than trusting the listing tile.
"""
import json
import random
import time
from dataclasses import dataclass
from typing import Callable

from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from . import scraperapi, stock_state
from .base import RawDeal


@dataclass
class _ListingProduct:
    title: str
    price_pence: int | None
    url: str
    in_stock: bool


def _parse_listing(html: str) -> list[_ListingProduct]:
    soup = BeautifulSoup(html, "html.parser")
    products = []
    for tag in soup.find_all("script", type="application/ld+json"):
        if not tag.string:
            continue
        try:
            data = json.loads(tag.string)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("@type") != "Product":
            continue
        offer = data.get("offers") or {}
        url = offer.get("url") or data.get("url")
        if not url:
            continue
        price = offer.get("price")
        products.append(_ListingProduct(
            title=data.get("name") or "",
            price_pence=round(price * 100) if isinstance(price, (int, float)) else None,
            url=url,
            in_stock=offer.get("availability") == "http://schema.org/InStock",
        ))
    return products


def _product_page_details(html: str) -> tuple[int | None, str | None]:
    """Re-derives price (GBP only — see module docstring on listing-tile
    data quality) and the first product image from the product page's own
    JSON-LD, which is more reliable than the listing tile's."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        if not tag.string:
            continue
        try:
            data = json.loads(tag.string)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("@type") != "Product":
            continue
        offer = data.get("offers") or {}
        price_pence = None
        if offer.get("priceCurrency") == "GBP" and isinstance(offer.get("price"), (int, float)):
            price_pence = round(offer["price"] * 100)
        images = [i for i in (data.get("image") or []) if i]
        return price_pence, (images[0] if images else None)
    return None, None


class PokemonCenterAdapter:
    def __init__(self, category_urls: list[str], api_key: str, min_delay_s: float, max_delay_s: float):
        self.category_urls = category_urls
        self.api_key = api_key
        self.min_delay_s = min_delay_s
        self.max_delay_s = max_delay_s

    def crawl(self, db: Session, on_deal: Callable[[RawDeal], None]) -> int:
        """Calls on_deal(raw) immediately for each drop as it's found,
        rather than collecting a list to hand back after the whole crawl
        finishes — see argos.py's crawl() docstring for why (confirmed live
        2026-07-21: an interrupted crawl silently lost 32 real items this
        way). Returns the count of drops found, for the caller's own
        logging."""
        count = 0
        for category_url in self.category_urls:
            self._delay()
            result = scraperapi.fetch(category_url, self.api_key)
            if result is None or result[0] != 200:
                print(f"[POKEMON_CENTER] {category_url}: fetch failed ({result[0] if result else 'error'}), skipping")
                continue

            products = _parse_listing(result[1])
            if not products:
                print(f"[POKEMON_CENTER] {category_url}: no products parsed (page structure changed?)")
            for product in products:
                if self._process_product(db, product, on_deal):
                    count += 1
        return count

    def _process_product(self, db: Session, product: _ListingProduct, on_deal: Callable[[RawDeal], None]) -> bool:
        is_drop = stock_state.check(db, "pokemon_center", product.url, product.in_stock)
        if not is_drop:
            stock_state.record(db, "pokemon_center", product.url, product.in_stock)
            return False

        self._delay()
        result = scraperapi.fetch(product.url, self.api_key)
        if result is None or result[0] != 200:
            print(f"[POKEMON_CENTER] {product.url}: product page fetch failed, using listing data only")
            raw = RawDeal(
                source="pokemon_center", retailer="Pokemon Center", title=product.title,
                url=product.url, buy_price_pence=product.price_pence or 0,
                image_url=None, html="<html></html>",
            )
        else:
            price_pence, image_url = _product_page_details(result[1])
            raw = RawDeal(
                source="pokemon_center", retailer="Pokemon Center", title=product.title,
                url=product.url, buy_price_pence=price_pence or product.price_pence or 0,
                image_url=image_url, html=result[1],
            )

        on_deal(raw)
        stock_state.record(db, "pokemon_center", product.url, product.in_stock)   # only mark "seen" once on_deal has run
        return True

    def _delay(self) -> None:
        time.sleep(random.uniform(self.min_delay_s, self.max_delay_s))
