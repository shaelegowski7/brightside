"""Direct ASIN extraction when the resolved retailer URL is already
amazon.co.uk — higher confidence than JSON-LD since it skips the Keepa EAN
lookup entirely (Keepa is queried by ASIN directly). Confirmed live
2026-07-19 against a real hotukdeals -> amazon.co.uk redirect
(.../dp/{ASIN}?tag=...). Amazon UK only — Keepa/decision engine are UK-only
throughout this project."""
import re
from urllib.parse import urlparse

_ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?]|$)")


def extract_asin(url: str) -> str | None:
    host = urlparse(url).netloc.split(":")[0].lower()
    if host != "amazon.co.uk" and not host.endswith(".amazon.co.uk"):
        return None
    match = _ASIN_RE.search(urlparse(url).path)
    return match.group(1) if match else None
