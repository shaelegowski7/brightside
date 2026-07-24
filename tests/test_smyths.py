"""Smyths Toys clearance scraper: DOM-based product-tile listing parsing
(Smyths is Nuxt 3 SSR — no __NEXT_DATA__-style product array like Argos, see
smyths.py's module docstring), EAN extraction from a data-loadbee-gtin
attribute (Smyths' JSON-LD has no structured gtin field), and crawl_state-
gated crawling — a product page is only fetched when the listing price is
new or has changed."""
from app.sources import scraperapi, smyths


def _tile(url_path: str, title: str, pounds: int, pence: int) -> str:
    return (
        f'<a href="{url_path}">'
        f'<div data-test="card-title"><h3>{title}</h3></div>'
        f'<span class="text-price-lg">{pounds}</span>'
        f'<span class="notranslate">.{pence:02d}</span>'
        f"</a>"
    )


def _listing_html(tiles: list[str]) -> str:
    return f"<html><body>{''.join(tiles)}</body></html>"


def _product_html(gtin: str | None) -> str:
    ld = (
        '<script type="application/ld+json">{"@type":"Product","name":"Widget",'
        '"sku":"123","offers":[{"@type":"Offer","price":"12.99"}]}</script>'
    )
    widget = f'<div class="loadbeeTabContent" data-loadbee-gtin="{gtin}"></div>' if gtin else ""
    return f"<html><body>{ld}{widget}</body></html>"


def test_parse_listing_extracts_products():
    html = _listing_html([
        _tile("/uk/en-gb/toys/widget-a/p/1234567", "Widget A", 12, 50),
        _tile("/uk/en-gb/toys/widget-b/p/7654321", "Widget B", 3, 99),
    ])
    products = smyths._parse_listing(html)
    assert len(products) == 2
    assert products[0].title == "Widget A"
    assert products[0].price_pence == 1250
    assert products[0].url == "https://www.smythstoys.com/uk/en-gb/toys/widget-a/p/1234567"
    assert products[1].price_pence == 399


def test_parse_listing_no_matching_tiles_returns_empty():
    assert smyths._parse_listing("<html><body>no products here</body></html>") == []


def test_parse_listing_ignores_duplicate_hrefs():
    tile = _tile("/uk/en-gb/toys/widget-a/p/1234567", "Widget A", 12, 50)
    html = _listing_html([tile, tile])
    assert len(smyths._parse_listing(html)) == 1


def test_extract_smyths_ean_from_loadbee_attribute():
    html = _product_html("0810087219499")
    assert smyths._extract_smyths_ean(html) == "0810087219499"


def test_extract_smyths_ean_returns_none_when_absent():
    html = _product_html(None)
    assert smyths._extract_smyths_ean(html) is None


def test_crawl_fetches_product_page_only_for_new_or_changed_items(db_session, monkeypatch):
    listing_page_1 = _listing_html([_tile("/uk/en-gb/toys/widget/p/1111111", "Widget", 10, 0)])
    listing_page_2_empty = "<html><body>no more results</body></html>"
    product_page = _product_html("111122223333")

    calls = []

    def fake_fetch(url, api_key):
        calls.append(url)
        assert api_key == "test-key"
        if url == "https://www.smythstoys.com/uk/en-gb/sale":
            return 200, listing_page_1
        if url == "https://www.smythstoys.com/uk/en-gb/sale?page=2":
            return 200, listing_page_2_empty
        if url == "https://www.smythstoys.com/uk/en-gb/toys/widget/p/1111111":
            return 200, product_page
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(scraperapi, "fetch", fake_fetch)
    monkeypatch.setattr(smyths.time, "sleep", lambda s: None)

    adapter = smyths.SmythsAdapter(
        category_urls=["https://www.smythstoys.com/uk/en-gb/sale"],
        api_key="test-key", min_delay_s=0, max_delay_s=0,
    )

    deals = []
    count = adapter.crawl(db_session, on_deal=deals.append)
    assert count == 1
    assert len(deals) == 1
    assert deals[0].url == "https://www.smythstoys.com/uk/en-gb/toys/widget/p/1111111"
    assert deals[0].buy_price_pence == 1000
    assert deals[0].html == product_page
    assert calls.count("https://www.smythstoys.com/uk/en-gb/toys/widget/p/1111111") == 1

    # Second crawl, same price -> unchanged -> must not re-fetch the product page.
    calls.clear()
    deals_second = []
    count_second = adapter.crawl(db_session, on_deal=deals_second.append)
    assert count_second == 0
    assert deals_second == []
    assert "https://www.smythstoys.com/uk/en-gb/toys/widget/p/1111111" not in calls


def test_crawl_does_not_mark_seen_when_on_deal_raises(db_session, monkeypatch):
    """If processing a found deal fails, it must not be recorded in
    crawl_state -- otherwise a transient failure would permanently suppress
    a legitimate deal (see crawl_state.py's module docstring)."""
    listing_page_1 = _listing_html([_tile("/uk/en-gb/toys/widget/p/2222222", "Widget", 10, 0)])
    listing_page_2_empty = "<html><body>no more results</body></html>"
    product_page = _product_html("111122223333")

    def fake_fetch(url, api_key):
        if url == "https://www.smythstoys.com/uk/en-gb/sale":
            return 200, listing_page_1
        if url == "https://www.smythstoys.com/uk/en-gb/sale?page=2":
            return 200, listing_page_2_empty
        if url == "https://www.smythstoys.com/uk/en-gb/toys/widget/p/2222222":
            return 200, product_page
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(scraperapi, "fetch", fake_fetch)
    monkeypatch.setattr(smyths.time, "sleep", lambda s: None)

    adapter = smyths.SmythsAdapter(
        category_urls=["https://www.smythstoys.com/uk/en-gb/sale"],
        api_key="test-key", min_delay_s=0, max_delay_s=0,
    )

    def _boom(raw):
        raise RuntimeError("simulated processing failure")

    from app.sources import crawl_state
    try:
        adapter.crawl(db_session, on_deal=_boom)
    except RuntimeError:
        pass

    assert crawl_state.check(db_session, "smyths", "https://www.smythstoys.com/uk/en-gb/toys/widget/p/2222222", 1000) == "new"


def test_crawl_stops_category_on_fetch_failure(db_session, monkeypatch):
    monkeypatch.setattr(scraperapi, "fetch", lambda url, api_key: None)
    monkeypatch.setattr(smyths.time, "sleep", lambda s: None)

    adapter = smyths.SmythsAdapter(
        category_urls=["https://www.smythstoys.com/uk/en-gb/sale"],
        api_key="test-key", min_delay_s=0, max_delay_s=0,
    )
    assert adapter.crawl(db_session, on_deal=lambda raw: None) == 0
