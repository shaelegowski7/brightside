"""NDA Toys wholesale deals scraper. NDA Toys is a UK toy wholesaler/
distributor -- this crawls their public "Deals" listing
(https://www.nda-toys.com/wholesale-toy-deals), not their gated Product
Data CSV export (that requires a 30-day-trial application with real trade
references, invoices, and a utility bill -- see the site's own CSV page).
Confirmed live 2026-08-28: every field this needs (title, unit price inc
VAT, pack size, stock status) is visible on the listing page to a
logged-out visitor -- no account/session needed at all, despite the tile
showing "Create Account" / "Sign in to order" buttons alongside the real
price data.

Bot protection: NDA Toys rejects ScraperAPI's standard proxy pool and even
`premium=true` outright ("Protected domains may require adding premium=true
OR ultra_premium=true" -- confirmed live). Only `ultra_premium=true` gets a
real 200. This is the most expensive ScraperAPI tier, which is exactly why
this source is manual-trigger-only (see crawl_runner.py's _SOURCES),
NOT wired into scheduler.py's periodic jobs, and why _process_product below
only pays for the expensive per-product fetch on a new/changed item (same
crawl_state-gated two-stage pattern as Smyths).

Pagination is a genuine `<link rel="next" href="...">` in <head> -- far more
reliable than Smyths' unconfirmed `?page=N` guess. Stops when that tag is
absent (real last page) rather than an empty-listing heuristic.

Barcode is NOT on the listing page (confirmed absent site-wide across every
tile checked) -- only on the individual product page, as a plain
`<td>Barcode</td><td>NNNNNNNNNNNNN</td>` table row. Registered into
jsonld.RETAILER_EXTRACTORS same as Smyths, since NDA Toys' JSON-LD (if any)
was not checked -- this table row is a confirmed-working source regardless.

VAT: the tile shows both an ex-VAT "Unit Price" and an inc-VAT figure
("Unit Price £5.35 ... Inc VAT: £6.42"). buy_price_pence uses the
inc-VAT figure deliberately -- what actually leaves the account at
checkout, consistent with every other source here (Screwfix/Smyths/HUKD
retail prices are already VAT-inclusive), and doesn't assume VAT is
reclaimable.

Resume cursor: with 261 brand pages in category_urls (config.yaml), a
manual-trigger-only crawl that gets interrupted (e.g. a deploy) needs to
pick back up without re-walking every already-covered brand's listing
pages from scratch -- crawl_state alone only protects individual products,
not the listing-page fetches themselves. See models.NdaToysCrawlProgress:
a single persisted index into category_urls, advanced only once a brand's
listing is walked to a genuine last page, never on a fetch failure."""
import re
import time
from dataclasses import dataclass
from random import uniform
from typing import Callable

from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from . import crawl_state, scraperapi
from .base import RawDeal
from .. import models
from ..matching import jsonld

_BASE_URL = "https://www.nda-toys.com"
_NEXT_PAGE_RE = re.compile(r'<link rel="next" href="([^"]+)"\s*/?>')
_BARCODE_RE = re.compile(r"<td>Barcode</td>\s*<td>(\d{8,14})</td>")
_MAX_PAGES = 15  # safety cap -- rel="next" is the real stop condition


def _extract_nda_toys_ean(html: str) -> str | None:
    match = _BARCODE_RE.search(html)
    return match.group(1) if match else None


jsonld.RETAILER_EXTRACTORS["www.nda-toys.com"] = _extract_nda_toys_ean


@dataclass
class _ListingProduct:
    title: str
    url: str
    buy_price_pence: int   # inc-VAT unit price -- see module docstring
    image_url: str | None
    in_stock: bool


def _parse_price_pence(text: str) -> int | None:
    match = re.search(r"£\s?(\d+)\.(\d{2})", text)
    return int(match.group(1)) * 100 + int(match.group(2)) if match else None


def _parse_listing(html: str) -> list[_ListingProduct]:
    """Returns [] on anything unexpected (redesign, wrong page) so one
    malformed page can't crash the whole crawl -- same fail-open contract
    as every other source's listing parser."""
    soup = BeautifulSoup(html, "html.parser")
    products = []
    seen_urls = set()
    for card in soup.find_all("div", class_="nda-shadow"):
        card_html = str(card)
        url_match = re.search(r'href="(https://www\.nda-toys\.com/product/\d+/[^"]*)"', card_html)
        title_tag = card.find("h3", class_="card-title")
        if url_match is None or title_tag is None:
            continue
        url = url_match.group(1)
        if url in seen_urls:
            continue

        unit_price_match = re.search(r"Unit Price\s*<span[^>]*>£[\d.]+</span>\s*Inc VAT:\s*£([\d.]+)", card_html)
        if unit_price_match is None:
            continue
        buy_price_pence = _parse_price_pence(f"£{unit_price_match.group(1)}")
        if buy_price_pence is None:
            continue

        availability_match = re.search(r"Availability:\s*<span[^>]*>\s*<span[^>]*>([^<]+)</span>", card_html)
        in_stock = bool(availability_match) and "in stock" in availability_match.group(1).strip().lower()

        image_match = re.search(r'<img src="([^"]+)"', card_html)

        seen_urls.add(url)
        products.append(_ListingProduct(
            title=title_tag.get_text(strip=True),
            url=url,
            buy_price_pence=buy_price_pence,
            image_url=image_match.group(1) if image_match else None,
            in_stock=in_stock,
        ))
    return products


def _next_page_url(html: str) -> str | None:
    match = _NEXT_PAGE_RE.search(html)
    if match is None:
        return None
    href = match.group(1)
    return href if href.startswith("http") else _BASE_URL + href


class NdaToysAdapter:
    """category_urls/min_delay_s/max_delay_s match every other
    _ClearanceSource adapter's constructor shape (see scheduler.py's
    _run_clearance_poll) even though NDA Toys only really has one listing
    URL -- kept as a list for that shared calling convention, not because
    multiple deal pages exist today."""

    def __init__(self, category_urls: list[str], api_key: str, min_delay_s: float, max_delay_s: float):
        self.category_urls = category_urls
        self.api_key = api_key
        self.min_delay_s = min_delay_s
        self.max_delay_s = max_delay_s

    def crawl(self, db: Session, on_deal: Callable[[RawDeal], None]) -> int:
        """Resumes from wherever nda_toys_crawl_progress left off -- see
        models.NdaToysCrawlProgress's docstring. A brand's index only
        advances once its listing is walked all the way to a genuine last
        page (rel="next" absent), never on a fetch failure mid-walk, so an
        incomplete brand always gets retried in full next time rather than
        silently skipped."""
        start_index = self._resume_index(db)
        count = 0
        for i, start_url in enumerate(self.category_urls):
            if i < start_index:
                continue
            brand_count, completed = self._crawl_listing(db, start_url, on_deal)
            count += brand_count
            if completed:
                self._advance_progress(db, i)
        return count

    def _resume_index(self, db: Session) -> int:
        progress = db.get(models.NdaToysCrawlProgress, 1)
        return (progress.completed_through_index + 1) if progress else 0

    def _advance_progress(self, db: Session, index: int) -> None:
        progress = db.get(models.NdaToysCrawlProgress, 1)
        if progress is None:
            progress = models.NdaToysCrawlProgress(id=1, completed_through_index=index)
            db.add(progress)
        else:
            progress.completed_through_index = index
        db.commit()

    def _crawl_listing(self, db: Session, start_url: str, on_deal: Callable[[RawDeal], None]) -> tuple[int, bool]:
        count = 0
        url = start_url
        for _page_num in range(_MAX_PAGES):
            self._delay()
            result = scraperapi.fetch(url, self.api_key, ultra_premium=True)
            if result is None or result[0] != 200:
                print(f"[NDA_TOYS] {url}: listing fetch failed ({result[0] if result else 'error'}), stopping")
                return count, False

            status, html = result
            for product in _parse_listing(html):
                if not product.in_stock:
                    continue
                if self._process_product(db, product, on_deal):
                    count += 1

            next_url = _next_page_url(html)
            if next_url is None:
                return count, True
            url = next_url
        return count, False   # hit _MAX_PAGES without a genuine last page -- treat as incomplete

    def _process_product(self, db: Session, product: _ListingProduct, on_deal: Callable[[RawDeal], None]) -> bool:
        diff = crawl_state.check(db, "nda_toys", product.url, product.buy_price_pence)
        if diff == "unchanged":
            crawl_state.record(db, "nda_toys", product.url, product.buy_price_pence)
            return False

        # Only the per-product page carries the barcode -- this is the
        # expensive ultra_premium fetch, gated on new/changed items only
        # (see module docstring).
        self._delay()
        result = scraperapi.fetch(product.url, self.api_key, ultra_premium=True)
        if result is None or result[0] != 200:
            print(f"[NDA_TOYS] {product.url}: product page fetch failed, skipping -- will retry next crawl")
            return False  # not recorded as seen -- self-heals on the next crawl

        raw = RawDeal(
            source="nda_toys", retailer="NDA Toys", title=product.title,
            url=product.url, buy_price_pence=product.buy_price_pence,
            image_url=product.image_url, html=result[1],
        )
        on_deal(raw)
        crawl_state.record(db, "nda_toys", product.url, product.buy_price_pence)
        return True

    def _delay(self) -> None:
        time.sleep(uniform(self.min_delay_s, self.max_delay_s))
