"""Validates a matched product's real Amazon title against the deal's own
title — Fix Build Guide phase 2: "compare retail title/brand vs the Amazon
title... Zero overlap -> reject as bad source EAN." This is what catches a
wrong EAN-catalog mapping or a bad Keepa title-search top result (the
guide's "DOOM/webcam" example) before a mismatched product ever reaches
Discord.

Requires >=2 significant words to overlap -- deliberately NOT requiring the
specific "brand word" (the deal title's first significant word) to be among
them, despite the guide's literal wording. Relaxed 2026-07-23 after a live
run produced a real false-positive reject: a correct EAN match ("Lattafa Oud
Najdia Eau De Parfum" -> Amazon's own title "Oud Najdia Eau de Parfum") got
rejected only because Amazon's title omits the brand (common on beauty/
perfume listings, where brand lives in a separate field). Two independent
overlapping words is still a strong bar against coincidental matches (the
DOOM/webcam case has zero overlap either way) without that brand-specific
false-positive mode.

Only meaningful for matches where the Amazon title is independent evidence
(jsonld EAN lookups, title_search fallback) -- an amazon_url match has no
independent title to check against, so pipeline.py skips this for that path.
"""
import re

_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "with", "of", "in", "to", "on",
    "new", "set", "pack", "packs", "kit", "piece", "pieces", "pcs",
    "official", "genuine", "original", "brand",
}
_WORD_RE = re.compile(r"[a-z0-9]+")


def _significant_words(title: str) -> list[str]:
    words = _WORD_RE.findall(title.lower())
    return [w for w in words if len(w) >= 2 and w not in _STOPWORDS and not w.isdigit()]


def titles_plausibly_match(deal_title: str, amazon_title: str | None) -> bool:
    """True if the two titles are plausibly the same product, or if there's
    no independent Amazon title to check (fail-open only for that narrow
    "can't validate" case -- the caller's overall no-match path stays
    fail-closed regardless of this function)."""
    if not amazon_title:
        return True

    deal_words = _significant_words(deal_title)
    if not deal_words:
        return True   # nothing to check against (e.g. an all-stopword title)

    amazon_words = set(_significant_words(amazon_title))
    if not amazon_words:
        return True

    overlap = set(deal_words) & amazon_words
    return len(overlap) >= 2
