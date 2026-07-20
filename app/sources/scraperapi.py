"""Thin wrapper around ScraperAPI's proxy endpoint. Retailer scrapers (Argos,
and later Currys/Smyths) route fetches through this instead of hitting the
retailer directly — Argos's Akamai edge flat-403s every direct request from
this app's Railway IP (confirmed 2026-07-20, including robots.txt itself),
so a residential-pool proxy is the only way to reach these pages at all."""
import requests

_BASE_URL = "http://api.scraperapi.com/"
_TIMEOUT_SECONDS = 60


def fetch(url: str, api_key: str) -> tuple[int, str] | None:
    """Returns (status_code, body), or None on a request-level failure
    (timeout, connection error) — kept distinct from a non-200 response,
    which is still returned so callers can log/skip on their own terms
    rather than this module deciding what counts as "blocked"."""
    try:
        resp = requests.get(_BASE_URL, params={"api_key": api_key, "url": url}, timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        print(f"[SCRAPERAPI] {url}: request failed: {e}")
        return None
    return resp.status_code, resp.text
