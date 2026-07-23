"""hotukdeals RSS polling. Uses the pepper:merchant RSS extension for
merchant name + price — feedparser exposes it as entry.pepper_merchant, a
single dict {"name": ..., "price": "£X.XX"} (confirmed live 2026-07-19
against https://www.hotukdeals.com/rss/trending — note /rss/hot redirects
there). Falls back to regexing the title for feeds/items that lack it."""
import re

import feedparser

from .base import RawDeal, SourceAdapter

_PRICE_RE = re.compile(r"£\s?(\d+(?:\.\d{1,2})?)")
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# HUKD titles carry a leading "heat" indicator, e.g. "111° - Halfords 24 LED
# Tent Light" (confirmed live 2026-07-23 against the real /rss/trending
# feed — it's a degree symbol, not the zero-width character an earlier read
# of a mangled terminal dump suggested). Display/model-number-regex noise
# only — strip it rather than let it leak into Discord embeds.
_LEADING_ARTIFACT_RE = re.compile(r"^\d+°?\s*-\s*")


def _clean_title(title: str) -> str:
    return _LEADING_ARTIFACT_RE.sub("", title).strip()


def _parse_price_pence(entry) -> int | None:
    merchant = entry.get("pepper_merchant")
    price_str = merchant.get("price") if merchant else None
    match = _PRICE_RE.search(price_str) if price_str else _PRICE_RE.search(entry.get("title", ""))
    return round(float(match.group(1)) * 100) if match else None


def _merchant_name(entry) -> str | None:
    merchant = entry.get("pepper_merchant")
    return merchant.get("name") if merchant else None


def _image_url(entry) -> str | None:
    thumbs = entry.get("media_thumbnail") or []
    return thumbs[0].get("url") if thumbs else None


class HotUKDealsAdapter(SourceAdapter):
    def __init__(self, feed_url: str):
        self.feed_url = feed_url

    def poll(self) -> list[RawDeal]:
        parsed = feedparser.parse(self.feed_url, agent=_USER_AGENT)
        if parsed.bozo and not parsed.entries:
            print(f"[HUKD] failed to parse {self.feed_url}: {parsed.get('bozo_exception')}")
            return []

        deals = []
        for entry in parsed.entries:
            price_pence = _parse_price_pence(entry)
            link = entry.get("link")
            title = _clean_title(entry.get("title", "").strip())
            if price_pence is None or not link:
                print(f"[HUKD] skipping (missing price or link): {title!r}")
                continue
            deals.append(RawDeal(
                source="hotukdeals",
                retailer=_merchant_name(entry),
                title=title,
                url=link,
                buy_price_pence=price_pence,
                image_url=_image_url(entry),
            ))
        return deals
