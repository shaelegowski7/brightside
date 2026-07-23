"""Generic JSON-LD / microdata GTIN extraction from a resolved retailer
page. Per-retailer overrides register into RETAILER_EXTRACTORS (Phase 2
seam — e.g. an Argos-specific parser); empty for Phase 1. The generic
extractor covers standards-compliant retailers per the spec ("Most large UK
retailers embed this — Google Shopping requirement")."""
import json
import re
from typing import Callable
from urllib.parse import urlparse

from bs4 import BeautifulSoup

_GTIN_KEYS = ("gtin13", "gtin", "gtin12", "gtin8", "gtin14", "ean")
_VALID_GTIN_RE = re.compile(r"^\d{8}$|^\d{12,14}$")   # EAN-8, UPC-12, EAN-13, GTIN-14


def _clean_gtin(value) -> str | None:
    if not isinstance(value, (str, int)):
        return None
    digits = re.sub(r"\D", "", str(value))
    return digits if _VALID_GTIN_RE.match(digits) else None


def _search_ld_json_node(node) -> str | None:
    if isinstance(node, dict):
        for key in _GTIN_KEYS:
            if key in node:
                gtin = _clean_gtin(node[key])
                if gtin:
                    return gtin
        for value in node.values():   # Product may nest under "offers", "@graph", etc.
            found = _search_ld_json_node(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _search_ld_json_node(item)
            if found:
                return found
    return None


def _extract_from_ld_json(soup: BeautifulSoup) -> str | None:
    for tag in soup.find_all("script", type="application/ld+json"):
        if not tag.string:
            continue
        try:
            data = json.loads(tag.string)
        except (json.JSONDecodeError, TypeError):
            continue
        found = _search_ld_json_node(data)
        if found:
            return found
    return None


def _extract_from_microdata(soup: BeautifulSoup) -> str | None:
    for key in _GTIN_KEYS:
        tag = soup.find(attrs={"itemprop": key})
        if tag:
            gtin = _clean_gtin(tag.get("content") or tag.get_text(strip=True))
            if gtin:
                return gtin
    return None


RETAILER_EXTRACTORS: dict[str, Callable[[str], "str | None"]] = {}


def extract_ean(url: str, html: str) -> str | None:
    host = urlparse(url).netloc.split(":")[0].lower()
    override = RETAILER_EXTRACTORS.get(host)
    if override:
        result = override(html)
        if result:
            return result

    soup = BeautifulSoup(html, "html.parser")
    return _extract_from_ld_json(soup) or _extract_from_microdata(soup)


def _price_from_offers(offers) -> str | None:
    if isinstance(offers, dict):
        if "price" in offers:
            return offers["price"]
        spec = offers.get("priceSpecification")
        if isinstance(spec, dict) and "price" in spec:
            return spec["price"]
        return None
    if isinstance(offers, list):
        for item in offers:
            found = _price_from_offers(item)
            if found is not None:
                return found
    return None


def _search_ld_json_node_for_price(node) -> str | None:
    if isinstance(node, dict):
        if "offers" in node:
            found = _price_from_offers(node["offers"])
            if found is not None:
                return found
        for value in node.values():
            found = _search_ld_json_node_for_price(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _search_ld_json_node_for_price(item)
            if found is not None:
                return found
    return None


def _clean_price_pence(value) -> int | None:
    if value is None:
        return None
    try:
        return round(float(str(value).replace(",", "")) * 100)
    except (TypeError, ValueError):
        return None


def extract_price_pence(html: str) -> int | None:
    """Reads the retailer page's own listed price from Product JSON-LD's
    `offers.price` (or `offers.priceSpecification.price`) — used as a
    sanity check against the scraped/regex-parsed buy price (see
    pipeline.py's price-sanity reject) to catch parse mis-fires like a
    stray spec number being read as the price."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        if not tag.string:
            continue
        try:
            data = json.loads(tag.string)
        except (json.JSONDecodeError, TypeError):
            continue
        pence = _clean_price_pence(_search_ld_json_node_for_price(data))
        if pence is not None:
            return pence
    return None
