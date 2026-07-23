"""hotukdeals RSS parsing, tested offline against a fixture that mirrors the
real feed structure captured live 2026-07-19 from
https://www.hotukdeals.com/rss/trending (pepper:merchant is a single dict
per entry, not a list — verified against the real feedparser output)."""
import feedparser

from app.sources.hotukdeals import HotUKDealsAdapter, _clean_title, _image_url, _merchant_name, _parse_price_pence

_FIXTURE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:pepper="http://www.pepper.com/rss" xmlns:media="http://search.yahoo.com/mrss/" version="2.0">
<channel><title>hotukdeals</title><link>https://www.hotukdeals.com/hot</link>
<item>
<category><![CDATA[Electronics]]></category>
<pepper:merchant name="Amazon" price="£19.99"/>
<media:thumbnail url="https://images.hotukdeals.com/threads/raw/abc/1_1/re/150x150/qt/55/1_1.jpg" width="100" height="100"/>
<title><![CDATA[111° - Jameson Original Triple Distilled Blended Irish Whiskey, 70cl]]></title>
<description><![CDATA[<strong>£19.99 - Amazon</strong>]]></description>
<link>https://www.hotukdeals.com/deals/jameson-original-triple-distilled-blended-irish-whiskey-70cl-4938754</link>
<pubDate>Sun, 19 Jul 2026 01:30:17 +0100</pubDate>
<guid>https://www.hotukdeals.com/deals/jameson-original-triple-distilled-blended-irish-whiskey-70cl-4938754</guid>
</item>
<item>
<category><![CDATA[Groceries]]></category>
<title><![CDATA[100° - 24 birdseye chicken nuggets instore Blakenall/Walsall - £1.50 @ Heron Foods]]></title>
<description><![CDATA[No structured merchant tag on this one]]></description>
<link>https://www.hotukdeals.com/deals/24-birds-eye-chicken-nuggets-heron-blakenallwalsall-4936111</link>
<pubDate>Sun, 19 Jul 2026 01:30:52 +0100</pubDate>
<guid>https://www.hotukdeals.com/deals/24-birds-eye-chicken-nuggets-heron-blakenallwalsall-4936111</guid>
</item>
<item>
<category><![CDATA[Home]]></category>
<title><![CDATA[No price anywhere in this title]]></title>
<link>https://www.hotukdeals.com/deals/no-price-item-1234567</link>
<guid>https://www.hotukdeals.com/deals/no-price-item-1234567</guid>
</item>
</channel></rss>
"""


def _parse_fixture():
    return feedparser.parse(_FIXTURE_RSS.encode("utf-8"))


def test_pepper_merchant_parsed_as_price_and_name():
    entries = _parse_fixture().entries
    e = entries[0]
    assert _merchant_name(e) == "Amazon"
    assert _parse_price_pence(e) == 1999
    assert _image_url(e) == "https://images.hotukdeals.com/threads/raw/abc/1_1/re/150x150/qt/55/1_1.jpg"


def test_falls_back_to_title_regex_when_no_pepper_merchant():
    entries = _parse_fixture().entries
    e = entries[1]
    assert _merchant_name(e) is None
    assert _parse_price_pence(e) == 150


def test_returns_none_when_no_price_anywhere():
    entries = _parse_fixture().entries
    e = entries[2]
    assert _parse_price_pence(e) is None


def test_clean_title_strips_leading_heat_indicator():
    # Confirmed live 2026-07-23 against the real /rss/trending feed -- it's
    # a degree symbol ("111°"), not a zero-width character.
    assert _clean_title("111° - Jameson Original Triple Distilled Blended Irish Whiskey, 70cl") == \
        "Jameson Original Triple Distilled Blended Irish Whiskey, 70cl"
    assert _clean_title("No price anywhere in this title") == "No price anywhere in this title"


def test_poll_returns_cleaned_titles():
    deals = HotUKDealsAdapter(_FIXTURE_RSS).poll()
    assert deals[0].title == "Jameson Original Triple Distilled Blended Irish Whiskey, 70cl"
    assert deals[1].title == "24 birdseye chicken nuggets instore Blakenall/Walsall - £1.50 @ Heron Foods"
