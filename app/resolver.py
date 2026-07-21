"""Resolves a hotukdeals thread link to the final retailer URL + page HTML,
in a single HTTP round-trip. HUKD does not expose the outbound merchant URL
in RSS or server-rendered thread HTML (both `link` and `cpcLink` come back
null in the page's own embedded JSON state) — the real mechanism is a
cloaking redirect at /visit/threadmain/{threadId}, confirmed live
2026-07-19:

    https://www.hotukdeals.com/visit/threadmain/4938754
    -> 302 path.hotukdeals.com/pepper-uk/redirect?url=...
    -> 302 https://www.amazon.co.uk/dp/B000WIQLG2?...

threadId is just the trailing digits of the thread URL's slug, so no
thread-page fetch is needed before resolving — one GET to /visit/threadmain/
both follows the redirect chain and returns the retailer page body.

HUKD's own domain has never been observed blocking this direct fetch —
27% of all HUKD deals were landing on `fetch_blocked` in production
(confirmed live 2026-07-21), but that's downstream retailers (Tesco,
Waitrose, etc.) blocking us, not HUKD. When the direct fetch reaches the
retailer but gets blocked, we retry that exact resolved URL through
ScraperAPI — NOT the whole /visit/threadmain/ redirect chain: a live test
routing the redirect chain itself through ScraperAPI 504'd ("Protected
domains may require premium=true"), whereas fetching the already-resolved
Tesco URL directly through ScraperAPI's plain tier succeeded first try.
Retrying only the known final URL is also far cheaper (one proxy request
per genuinely-blocked item, not one per HUKD item).
"""
import re
from dataclasses import dataclass

import requests

from .sources import scraperapi

_THREAD_ID_RE = re.compile(r"-(\d+)/?$")
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_TIMEOUT_SECONDS = 15


@dataclass
class ResolvedDeal:
    final_url: str
    html: str | None      # None when the retailer blocked us / returned non-200
    status_code: int | None
    blocked: bool


def thread_id_from_url(hukd_url: str) -> str | None:
    match = _THREAD_ID_RE.search(hukd_url.rstrip("/"))
    return match.group(1) if match else None


def resolve(hukd_url: str, scraperapi_key: str = "") -> ResolvedDeal | None:
    """None means hukd_url doesn't look like a thread link at all (no
    threadId found) — a parsing problem, distinct from a network/`blocked`
    outcome, so callers can tell "can't even try" from "tried and failed".
    scraperapi_key is optional — pass "" (default) to skip the proxy retry
    entirely, e.g. in tests or if the key isn't configured."""
    thread_id = thread_id_from_url(hukd_url)
    if not thread_id:
        return None

    visit_url = f"https://www.hotukdeals.com/visit/threadmain/{thread_id}"
    try:
        resp = requests.get(
            visit_url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        print(f"[RESOLVER] {hukd_url}: request failed: {e}")
        return ResolvedDeal(final_url=visit_url, html=None, status_code=None, blocked=True)

    if resp.status_code == 200:
        return ResolvedDeal(final_url=resp.url, html=resp.text, status_code=200, blocked=False)

    # Direct fetch resolved the redirect chain fine but the retailer itself
    # blocked us (Cloudflare/Akamai or similar) — retry that exact URL via
    # ScraperAPI's proxy pool before giving up.
    if scraperapi_key:
        proxy_result = scraperapi.fetch(resp.url, scraperapi_key)
        if proxy_result is not None and proxy_result[0] == 200:
            print(f"[RESOLVER] {hukd_url}: retailer returned {resp.status_code} direct, recovered via proxy")
            return ResolvedDeal(final_url=resp.url, html=proxy_result[1], status_code=200, blocked=False)

    # Spec says skip + log rather than fight it further.
    print(f"[RESOLVER] {hukd_url}: retailer returned {resp.status_code}, skipping")
    return ResolvedDeal(final_url=resp.url, html=None, status_code=resp.status_code, blocked=True)
