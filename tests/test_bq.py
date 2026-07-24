"""B&Q clearance scraper: schema.org ItemList listing parsing (title, price,
url AND a real EAN straight from the `sku` field — no second per-product
fetch needed at all, see bq.py's module docstring), synthetic-JSON-LD
construction for the pipeline's existing jsonld.extract_ean() path, and
crawl_state-gated crawling across paginated listing pages."""
import json

from app.sources import bq, direct_fetch


def _item(name: str, url: str, price: float, sku: str) -> dict:
    return {"@type": "Product", "name": name, "url": url, "sku": sku, "offers": {"price": price}}


def _listing_html(items: list[dict]) -> str:
    item_list = {"@context": "https://schema.org", "@type": "ItemList", "numberOfItems": len(items), "itemListElement": items}
    return f'<html><body><script type="application/ld+json">{json.dumps(item_list)}</script></body></html>'


def test_parse_listing_extracts_products_with_ean():
    html = _listing_html([
        _item("Widget A", "https://www.diy.com/departments/widget-a/111_BQ.prd", 12.50, "5015111211646"),
        _item("Widget B", "https://www.diy.com/departments/widget-b/222_BQ.prd", 3.99, "03415344"),
    ])
    products = bq._parse_listing(html)
    assert len(products) == 2
    assert products[0].title == "Widget A"
    assert products[0].price_pence == 1250
    assert products[0].url == "https://www.diy.com/departments/widget-a/111_BQ.prd"
    assert products[0].ean == "5015111211646"
    assert products[1].ean == "03415344"   # 8-digit EAN-8 is also valid


def test_parse_listing_non_gtin_sku_leaves_ean_none():
    html = _listing_html([_item("Widget", "https://www.diy.com/x/1_BQ.prd", 5.0, "AB12")])
    products = bq._parse_listing(html)
    assert products[0].ean is None


def test_parse_listing_no_item_list_returns_empty():
    assert bq._parse_listing("<html><body>no data here</body></html>") == []


def test_parse_listing_malformed_json_returns_empty():
    html = '<html><body><script type="application/ld+json">{not valid json</script></body></html>'
    assert bq._parse_listing(html) == []


def test_synthetic_html_embeds_gtin_for_generic_extractor():
    product = bq._ListingProduct(title="Widget", price_pence=1250, url="https://www.diy.com/x/1_BQ.prd", ean="5015111211646")
    html = bq._synthetic_html(product)
    from app.matching import jsonld
    assert jsonld.extract_ean("https://www.diy.com/x/1_BQ.prd", html) == "5015111211646"


def test_synthetic_html_empty_when_no_ean():
    product = bq._ListingProduct(title="Widget", price_pence=1250, url="https://www.diy.com/x/1_BQ.prd", ean=None)
    assert bq._synthetic_html(product) == ""


def test_crawl_paginates_until_empty_page(db_session, monkeypatch):
    listing_page_1 = _listing_html([_item("Widget", "https://www.diy.com/x/1_BQ.prd", 10.0, "1111111111111")])
    listing_page_2_empty = "<html><body>no more results</body></html>"

    calls = []

    def fake_fetch(url):
        calls.append(url)
        if url == "https://www.diy.com/clearance.cat":
            return 200, listing_page_1
        if url == "https://www.diy.com/clearance.cat?page=2":
            return 200, listing_page_2_empty
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(direct_fetch, "fetch", fake_fetch)
    monkeypatch.setattr(bq.time, "sleep", lambda s: None)

    adapter = bq.BQAdapter(category_urls=["https://www.diy.com/clearance.cat"], min_delay_s=0, max_delay_s=0)

    deals = []
    count = adapter.crawl(db_session, on_deal=deals.append)
    assert count == 1
    assert len(deals) == 1
    assert deals[0].url == "https://www.diy.com/x/1_BQ.prd"
    assert deals[0].buy_price_pence == 1000
    from app.matching import jsonld
    assert jsonld.extract_ean(deals[0].url, deals[0].html) == "1111111111111"

    # Second crawl, same price -> unchanged -> no on_deal call.
    deals_second = []
    count_second = adapter.crawl(db_session, on_deal=deals_second.append)
    assert count_second == 0
    assert deals_second == []


def test_crawl_does_not_mark_seen_when_on_deal_raises(db_session, monkeypatch):
    """Mirrors argos.py's/smyths.py's equivalent test -- a transient
    processing failure must not permanently suppress a legitimate deal
    (see crawl_state.py's module docstring)."""
    listing_page_1 = _listing_html([_item("Widget", "https://www.diy.com/x/2_BQ.prd", 10.0, "2222222222222")])
    listing_page_2_empty = "<html><body>no more results</body></html>"

    def fake_fetch(url):
        if url == "https://www.diy.com/clearance.cat":
            return 200, listing_page_1
        if url == "https://www.diy.com/clearance.cat?page=2":
            return 200, listing_page_2_empty
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(direct_fetch, "fetch", fake_fetch)
    monkeypatch.setattr(bq.time, "sleep", lambda s: None)

    adapter = bq.BQAdapter(category_urls=["https://www.diy.com/clearance.cat"], min_delay_s=0, max_delay_s=0)

    def _boom(raw):
        raise RuntimeError("simulated processing failure")

    from app.sources import crawl_state
    try:
        adapter.crawl(db_session, on_deal=_boom)
    except RuntimeError:
        pass

    assert crawl_state.check(db_session, "bq", "https://www.diy.com/x/2_BQ.prd", 1000) == "new"


def test_crawl_stops_category_on_fetch_failure(db_session, monkeypatch):
    monkeypatch.setattr(direct_fetch, "fetch", lambda url: None)
    monkeypatch.setattr(bq.time, "sleep", lambda s: None)

    adapter = bq.BQAdapter(category_urls=["https://www.diy.com/clearance.cat"], min_delay_s=0, max_delay_s=0)
    assert adapter.crawl(db_session, on_deal=lambda raw: None) == 0
