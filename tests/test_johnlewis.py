"""John Lewis outlet scraper: __NEXT_DATA__ listing parsing at the stable
productListingData.products path (unlike Screwfix, this page-prop key
doesn't move -- see johnlewis.py's module docstring), and crawl_state-gated
crawling -- a product page is only fetched when the listing price is new or
has changed. No retailer-specific EAN extractor is needed or tested here:
John Lewis's product pages use the standard `gtin13` JSON-LD key, so the
existing generic jsonld.extract_ean() path (already covered by tests/
test_matching.py) picks it up unchanged."""
import json

from app.sources import direct_fetch, johnlewis


def _product(product_id: str, title: str, path: str, price_min: str) -> dict:
    return {
        "productId": product_id,
        "title": title,
        "url": path,
        "variantPriceRange": {"value": {"min": price_min, "max": price_min}},
    }


def _listing_html(products: list[dict]) -> str:
    next_data = {"props": {"pageProps": {"productListingData": {"products": products, "results": len(products), "pagesAvailable": 1}}}}
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script></body></html>'


def _product_html(gtin: str | None) -> str:
    offers = {"@type": "Offer", "price": 12.99, "priceCurrency": "GBP"}
    if gtin:
        offers["gtin13"] = gtin
    ld = {"@context": "https://schema.org", "@type": "Product", "name": "Widget", "offers": offers}
    return f'<html><body><script type="application/ld+json">{json.dumps(ld)}</script></body></html>'


def test_parse_listing_extracts_products():
    html = _listing_html([
        _product("111", "Widget A", "/widget-a/p111", "12.50"),
        _product("222", "Widget B", "/widget-b/p222", "3.99"),
    ])
    products = johnlewis._parse_listing(html)
    assert len(products) == 2
    assert products[0].title == "Widget A"
    assert products[0].price_pence == 1250
    assert products[0].url == "https://www.johnlewis.com/widget-a/p111"
    assert products[1].price_pence == 399


def test_parse_listing_missing_next_data_returns_empty():
    assert johnlewis._parse_listing("<html><body>no data here</body></html>") == []


def test_parse_listing_malformed_next_data_returns_empty():
    html = '<html><body><script id="__NEXT_DATA__" type="application/json">{"not": "expected shape"}</script></body></html>'
    assert johnlewis._parse_listing(html) == []


def test_crawl_fetches_product_page_only_for_new_or_changed_items(db_session, monkeypatch):
    listing_page = _listing_html([_product("111", "Widget", "/widget/p111", "10.00")])
    product_page = _product_html("5063682534824")

    calls = []

    def fake_fetch(url):
        calls.append(url)
        if url == "https://www.johnlewis.com/browse/outlet/_/N-puqr":
            return 200, listing_page
        if url == "https://www.johnlewis.com/widget/p111":
            return 200, product_page
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(direct_fetch, "fetch", fake_fetch)
    monkeypatch.setattr(johnlewis.time, "sleep", lambda s: None)

    adapter = johnlewis.JohnLewisAdapter(
        category_urls=["https://www.johnlewis.com/browse/outlet/_/N-puqr"], min_delay_s=0, max_delay_s=0,
    )

    deals = []
    count = adapter.crawl(db_session, on_deal=deals.append)
    assert count == 1
    assert len(deals) == 1
    assert deals[0].url == "https://www.johnlewis.com/widget/p111"
    assert deals[0].buy_price_pence == 1000
    assert deals[0].html == product_page
    assert calls.count("https://www.johnlewis.com/widget/p111") == 1

    from app.matching import jsonld
    assert jsonld.extract_ean(deals[0].url, deals[0].html) == "5063682534824"

    # Second crawl, same price -> unchanged -> must not re-fetch the product page.
    calls.clear()
    deals_second = []
    count_second = adapter.crawl(db_session, on_deal=deals_second.append)
    assert count_second == 0
    assert deals_second == []
    assert "https://www.johnlewis.com/widget/p111" not in calls


def test_crawl_does_not_mark_seen_when_on_deal_raises(db_session, monkeypatch):
    """Mirrors argos.py's/smyths.py's equivalent test -- a transient
    processing failure must not permanently suppress a legitimate deal
    (see crawl_state.py's module docstring)."""
    listing_page = _listing_html([_product("222", "Widget", "/widget/p222", "10.00")])
    product_page = _product_html("5063682534824")

    def fake_fetch(url):
        if url == "https://www.johnlewis.com/browse/outlet/_/N-puqr":
            return 200, listing_page
        if url == "https://www.johnlewis.com/widget/p222":
            return 200, product_page
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(direct_fetch, "fetch", fake_fetch)
    monkeypatch.setattr(johnlewis.time, "sleep", lambda s: None)

    adapter = johnlewis.JohnLewisAdapter(
        category_urls=["https://www.johnlewis.com/browse/outlet/_/N-puqr"], min_delay_s=0, max_delay_s=0,
    )

    def _boom(raw):
        raise RuntimeError("simulated processing failure")

    from app.sources import crawl_state
    try:
        adapter.crawl(db_session, on_deal=_boom)
    except RuntimeError:
        pass

    assert crawl_state.check(db_session, "johnlewis", "https://www.johnlewis.com/widget/p222", 1000) == "new"


def test_crawl_skips_category_on_listing_fetch_failure(db_session, monkeypatch):
    monkeypatch.setattr(direct_fetch, "fetch", lambda url: None)
    monkeypatch.setattr(johnlewis.time, "sleep", lambda s: None)

    adapter = johnlewis.JohnLewisAdapter(
        category_urls=["https://www.johnlewis.com/browse/outlet/_/N-puqr"], min_delay_s=0, max_delay_s=0,
    )
    assert adapter.crawl(db_session, on_deal=lambda raw: None) == 0


def test_crawl_does_not_record_seen_when_product_page_fetch_fails(db_session, monkeypatch):
    listing_page = _listing_html([_product("333", "Widget", "/widget/p333", "10.00")])

    def fake_fetch(url):
        if url == "https://www.johnlewis.com/browse/outlet/_/N-puqr":
            return 200, listing_page
        return None   # product page fetch fails

    monkeypatch.setattr(direct_fetch, "fetch", fake_fetch)
    monkeypatch.setattr(johnlewis.time, "sleep", lambda s: None)

    adapter = johnlewis.JohnLewisAdapter(
        category_urls=["https://www.johnlewis.com/browse/outlet/_/N-puqr"], min_delay_s=0, max_delay_s=0,
    )

    deals = []
    count = adapter.crawl(db_session, on_deal=deals.append)
    assert count == 0
    assert deals == []

    from app.sources import crawl_state
    assert crawl_state.check(db_session, "johnlewis", "https://www.johnlewis.com/widget/p333", 1000) == "new"
