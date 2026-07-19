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
"""
import re
from dataclasses import dataclass

import requests

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


def resolve(hukd_url: str) -> ResolvedDeal | None:
    """None means hukd_url doesn't look like a thread link at all (no
    threadId found) — a parsing problem, distinct from a network/`blocked`
    outcome, so callers can tell "can't even try" from "tried and failed"."""
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

    if resp.status_code != 200:
        # Cloudflare/Akamai bot-block or similar — spec says skip + log
        # rather than fight it.
        print(f"[RESOLVER] {hukd_url}: retailer returned {resp.status_code}, skipping")
        return ResolvedDeal(final_url=resp.url, html=None, status_code=resp.status_code, blocked=True)

    return ResolvedDeal(final_url=resp.url, html=resp.text, status_code=200, blocked=False)
