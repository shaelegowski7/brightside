"""Thin plain-HTTP fetch helper, same (status_code, body)|None signature as
scraperapi.fetch() — for retailers confirmed to have no bot protection
(B&Q/diy.com, Screwfix, Home Bargains; confirmed live 2026-07-24: all three
serve full listing/product content to a plain browser-UA request, no proxy
needed), so their adapters don't need a ScraperAPI key at all."""
import requests

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_TIMEOUT_SECONDS = 20


def fetch(url: str) -> tuple[int, str] | None:
    """Returns (status_code, body), or None on a request-level failure
    (timeout, connection error) — same contract as scraperapi.fetch()."""
    try:
        resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        print(f"[DIRECT_FETCH] {url}: request failed: {e}")
        return None
    return resp.status_code, resp.text
