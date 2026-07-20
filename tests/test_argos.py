"""Argos clearance scraper: __NEXT_DATA__ listing parsing (Argos is Next.js
SSR — no DOM/CSS scraping needed, see argos.py's module docstring), EAN
extraction from product-page JSON-LD description text (Argos doesn't use a
structured gtin field), and crawl_state-gated crawling — a product page is
only fetched when the listing price is new or has changed."""
import json

from app.sources import argos, scraperapi


def _listing_html(products: list[dict]) -> str:
    next_data = {"props": {"pageProps": {"productData": products}}}
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script></body></html>'


def _product_html(description: str) -> str:
    ld = {
        "@context": "https://schema.org",
        "@graph": [{"@type": "Product", "name": "Widget", "description": description}],
    }
    return f'<html><body><script type="application/ld+json">{json.dumps(ld)}</script></body></html>'


def test_parse_listing_extracts_products():
    html = _listing_html([
        {"id": "1234567", "attributes": {"name": "Widget A", "price": 12.5}},
        {"id": "7654321", "attributes": {"name": "Widget B", "price": 3.99}},
    ])
    products = argos._parse_listing(html)
    assert len(products) == 2
    assert products[0].title == "Widget A"
    assert products[0].price_pence == 1250
    assert products[0].url == "https://www.argos.co.uk/product/1234567"
    assert products[1].price_pence == 399


def test_parse_listing_missing_next_data_returns_empty():
    assert argos._parse_listing("<html><body>no data here</body></html>") == []


def test_parse_listing_malformed_next_data_returns_empty():
    html = '<html><body><script id="__NEXT_DATA__" type="application/json">{"not": "expected shape"}</script></body></html>'
    assert argos._parse_listing(html) == []


def test_extract_argos_ean_from_description_text():
    html = _product_html("Some blurb. EAN: 196388561070. More text.")
    assert argos._extract_argos_ean(html) == "196388561070"


def test_extract_argos_ean_returns_none_when_absent():
    html = _product_html("No identifying codes mentioned here.")
    assert argos._extract_argos_ean(html) is None


def test_crawl_fetches_product_page_only_for_new_or_changed_items(db_session, monkeypatch):
    listing_page_1 = _listing_html([{"id": "1111111", "attributes": {"name": "Widget", "price": 10.0}}])
    listing_page_2_empty = "<html><body>no more results</body></html>"
    product_page = _product_html("EAN: 111122223333.")

    calls = []

    def fake_fetch(url, api_key):
        calls.append(url)
        assert api_key == "test-key"
        if url == "https://www.argos.co.uk/clearance/technology/c:1/":
            return 200, listing_page_1
        if url == "https://www.argos.co.uk/clearance/technology/c:1/opt/page:2/":
            return 200, listing_page_2_empty
        if url == "https://www.argos.co.uk/product/1111111":
            return 200, product_page
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(scraperapi, "fetch", fake_fetch)
    monkeypatch.setattr(argos.time, "sleep", lambda s: None)

    adapter = argos.ArgosAdapter(
        category_urls=["https://www.argos.co.uk/clearance/technology/c:1/"],
        api_key="test-key", min_delay_s=0, max_delay_s=0,
    )

    deals = adapter.crawl(db_session)
    assert len(deals) == 1
    assert deals[0].url == "https://www.argos.co.uk/product/1111111"
    assert deals[0].buy_price_pence == 1000
    assert deals[0].html == product_page
    assert calls.count("https://www.argos.co.uk/product/1111111") == 1

    # Second crawl, same price -> unchanged -> must not re-fetch the product page.
    calls.clear()
    deals_second = adapter.crawl(db_session)
    assert deals_second == []
    assert "https://www.argos.co.uk/product/1111111" not in calls


def test_crawl_stops_category_on_fetch_failure(db_session, monkeypatch):
    monkeypatch.setattr(scraperapi, "fetch", lambda url, api_key: None)
    monkeypatch.setattr(argos.time, "sleep", lambda s: None)

    adapter = argos.ArgosAdapter(
        category_urls=["https://www.argos.co.uk/clearance/technology/c:1/"],
        api_key="test-key", min_delay_s=0, max_delay_s=0,
    )
    assert adapter.crawl(db_session) == []
