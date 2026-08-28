"""Thin wrapper around ScraperAPI's proxy endpoint. Retailer scrapers (Argos,
and later Currys/Smyths) route fetches through this instead of hitting the
retailer directly — Argos's Akamai edge flat-403s every direct request from
this app's Railway IP (confirmed 2026-07-20, including robots.txt itself),
so a residential-pool proxy is the only way to reach these pages at all."""
import requests

_BASE_URL = "http://api.scraperapi.com/"
_TIMEOUT_SECONDS = 60


def fetch(url: str, api_key: str, ultra_premium: bool = False) -> tuple[int, str] | None:
    """Returns (status_code, body), or None on a request-level failure
    (timeout, connection error) — kept distinct from a non-200 response,
    which is still returned so callers can log/skip on their own terms
    rather than this module deciding what counts as "blocked".

    ultra_premium routes through ScraperAPI's most expensive tier (full
    headless browser + CAPTCHA-solving) -- confirmed live 2026-08-28 that
    NDA Toys' bot protection rejects the standard proxy pool and even
    `premium=true` outright ("Protected domains may require adding
    premium=true OR ultra_premium=true"); only ultra_premium got a real
    200. Default False since every other source here works on the
    standard tier and ultra_premium costs meaningfully more per request."""
    params = {"api_key": api_key, "url": url}
    if ultra_premium:
        params["ultra_premium"] = "true"
    try:
        resp = requests.get(_BASE_URL, params=params, timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        print(f"[SCRAPERAPI] {url}: request failed: {e}")
        return None
    return resp.status_code, resp.text
