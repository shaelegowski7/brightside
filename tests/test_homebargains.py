"""Home Bargains clearance/offers scraper: DOM-based product-tile listing
parsing (a confirmed-stable `<section class="productcard">` component, see
homebargains.py's module docstring), price parsing from the split
comment-interleaved markup Home Bargains renders, and crawl_state-gated
crawling. No EAN mechanism on this site at all, so unlike Argos/Smyths there
is no second per-product fetch here -- every deal relies on the pipeline's
own title-search fallback."""
from app.sources import direct_fetch, homebargains


def _card(url_path: str, title: str, price_text: str) -> str:
    return (
        f'<section class="productcard undefined"><div class="innerwrapper">'
        f'<a href="{url_path}"><img/></a>'
        f'<div class="flex-grow"><a class="title line-clamp-3" href="{url_path}">{title}</a></div>'
        f'<div class="pricewrapper"><div class="price">£<!-- -->{price_text}</div></div>'
        f"</div></section>"
    )


def _listing_html(cards: list[str]) -> str:
    return f"<html><body>{''.join(cards)}</body></html>"


def test_parse_listing_extracts_products():
    html = _listing_html([
        _card("/product/aaa-111/widget-a", "Widget A", "12.50"),
        _card("/product/bbb-222/widget-b", "Widget B", "3.99"),
    ])
    products = homebargains._parse_listing(html)
    assert len(products) == 2
    assert products[0].title == "Widget A"
    assert products[0].price_pence == 1250
    assert products[0].url == "https://home.bargains/product/aaa-111/widget-a"
    assert products[1].price_pence == 399


def test_parse_listing_no_matching_cards_returns_empty():
    assert homebargains._parse_listing("<html><body>no products here</body></html>") == []


def test_parse_listing_ignores_duplicate_hrefs():
    card = _card("/product/aaa-111/widget-a", "Widget A", "12.50")
    html = _listing_html([card, card])
    assert len(homebargains._parse_listing(html)) == 1


def test_parse_listing_skips_card_missing_price():
    card = (
        '<section class="productcard"><div class="innerwrapper">'
        '<a href="/product/ccc-333/widget-c"></a>'
        '<a class="title" href="/product/ccc-333/widget-c">Widget C</a>'
        "</div></section>"
    )
    assert homebargains._parse_listing(_listing_html([card])) == []


def test_crawl_single_page_no_pagination(db_session, monkeypatch):
    """Pagination is unconfirmed (see module docstring) -- crawl() fetches
    exactly one page per configured category_url, no page-2 attempt."""
    listing_page = _listing_html([_card("/product/aaa-111/widget", "Widget", "10.00")])

    calls = []

    def fake_fetch(url):
        calls.append(url)
        return 200, listing_page

    monkeypatch.setattr(direct_fetch, "fetch", fake_fetch)
    monkeypatch.setattr(homebargains.time, "sleep", lambda s: None)

    adapter = homebargains.HomeBargainsAdapter(
        category_urls=["https://home.bargains/category/999/starbuys"], min_delay_s=0, max_delay_s=0,
    )

    deals = []
    count = adapter.crawl(db_session, on_deal=deals.append)
    assert count == 1
    assert len(deals) == 1
    assert deals[0].url == "https://home.bargains/product/aaa-111/widget"
    assert deals[0].buy_price_pence == 1000
    assert deals[0].html == ""   # no EAN mechanism -- pipeline falls to title-search
    assert calls == ["https://home.bargains/category/999/starbuys"]

    # Second crawl, same price -> unchanged -> no on_deal call.
    deals_second = []
    count_second = adapter.crawl(db_session, on_deal=deals_second.append)
    assert count_second == 0
    assert deals_second == []


def test_crawl_does_not_mark_seen_when_on_deal_raises(db_session, monkeypatch):
    listing_page = _listing_html([_card("/product/bbb-222/widget", "Widget", "10.00")])
    monkeypatch.setattr(direct_fetch, "fetch", lambda url: (200, listing_page))
    monkeypatch.setattr(homebargains.time, "sleep", lambda s: None)

    adapter = homebargains.HomeBargainsAdapter(
        category_urls=["https://home.bargains/category/999/starbuys"], min_delay_s=0, max_delay_s=0,
    )

    def _boom(raw):
        raise RuntimeError("simulated processing failure")

    from app.sources import crawl_state
    try:
        adapter.crawl(db_session, on_deal=_boom)
    except RuntimeError:
        pass

    assert crawl_state.check(db_session, "homebargains", "https://home.bargains/product/bbb-222/widget", 1000) == "new"


def test_crawl_skips_category_on_fetch_failure(db_session, monkeypatch):
    monkeypatch.setattr(direct_fetch, "fetch", lambda url: None)
    monkeypatch.setattr(homebargains.time, "sleep", lambda s: None)

    adapter = homebargains.HomeBargainsAdapter(
        category_urls=["https://home.bargains/category/999/starbuys"], min_delay_s=0, max_delay_s=0,
    )
    assert adapter.crawl(db_session, on_deal=lambda raw: None) == 0
