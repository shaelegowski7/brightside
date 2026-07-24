"""Screwfix clearance scraper: __NEXT_DATA__ search-results listing parsing
via the generic "find the dict with a products list" walk (the exact page-
component key is a per-render hash, not a stable path — see screwfix.py's
module docstring), and crawl_state-gated crawling. No EAN mechanism on this
site at all, so unlike Argos/Smyths there is no second per-product fetch or
EAN-extraction test here — every deal from this source relies on the
pipeline's own title-search fallback."""
import json

from app.sources import direct_fetch, screwfix


def _product(sku: str, title: str, path: str, price_pounds: float) -> dict:
    return {
        "skuId": sku,
        "longDescription": title,
        "detailPageUrl": path,
        "priceInformation": {"currentPriceIncVat": {"currency": "GBP", "amount": price_pounds}},
    }


def _listing_html(products: list[dict], total: int | None = None) -> str:
    next_data = {
        "props": {
            "pageProps": {
                "page": {
                    "page": {
                        "some-hash-key": {
                            "models": {
                                "enrichedData": {
                                    "products": products,
                                    "totalProducts": total if total is not None else len(products),
                                    "pagination": {"offset": 0, "limit": 20},
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script></body></html>'


def test_parse_listing_extracts_products():
    html = _listing_html([
        _product("111A", "Widget A", "/p/widget-a/111a", 12.50),
        _product("222B", "Widget B", "/p/widget-b/222b", 3.99),
    ])
    products = screwfix._parse_listing(html)
    assert len(products) == 2
    assert products[0].title == "Widget A"
    assert products[0].price_pence == 1250
    assert products[0].url == "https://www.screwfix.com/p/widget-a/111a"
    assert products[1].price_pence == 399


def test_parse_listing_missing_next_data_returns_empty():
    assert screwfix._parse_listing("<html><body>no data here</body></html>") == []


def test_parse_listing_no_products_key_returns_empty():
    next_data = {"props": {"pageProps": {"page": {"page": {"x": {"models": {"enrichedData": {"totalProducts": 0}}}}}}}}
    html = f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script></body></html>'
    assert screwfix._parse_listing(html) == []


def test_with_page_size_appends_correctly_with_and_without_existing_query():
    assert screwfix._with_page_size("https://www.screwfix.com/search?search=clearancepromo") == \
        "https://www.screwfix.com/search?search=clearancepromo&page_size=100"
    assert screwfix._with_page_size("https://www.screwfix.com/search") == \
        "https://www.screwfix.com/search?page_size=100"


def test_crawl_appends_page_size_100_and_fetches_once_per_category(db_session, monkeypatch):
    """Real ceiling confirmed live via Playwright network capture (see module
    docstring) -- crawl() fetches exactly one page per configured
    category_url, always at page_size=100, no further pagination attempt."""
    listing_page = _listing_html([_product("111A", "Widget", "/p/widget/111a", 10.0)], total=1557)

    calls = []

    def fake_fetch(url):
        calls.append(url)
        return 200, listing_page

    monkeypatch.setattr(direct_fetch, "fetch", fake_fetch)
    monkeypatch.setattr(screwfix.time, "sleep", lambda s: None)

    adapter = screwfix.ScrewfixAdapter(
        category_urls=["https://www.screwfix.com/search?search=clearancepromo"], min_delay_s=0, max_delay_s=0,
    )

    deals = []
    count = adapter.crawl(db_session, on_deal=deals.append)
    assert count == 1
    assert len(deals) == 1
    assert deals[0].url == "https://www.screwfix.com/p/widget/111a"
    assert deals[0].buy_price_pence == 1000
    assert deals[0].html == ""   # no EAN mechanism -- pipeline falls to title-search
    assert calls == ["https://www.screwfix.com/search?search=clearancepromo&page_size=100"]

    # Second crawl, same price -> unchanged -> no on_deal call.
    deals_second = []
    count_second = adapter.crawl(db_session, on_deal=deals_second.append)
    assert count_second == 0
    assert deals_second == []


def test_crawl_does_not_mark_seen_when_on_deal_raises(db_session, monkeypatch):
    listing_page = _listing_html([_product("222B", "Widget", "/p/widget/222b", 10.0)])
    monkeypatch.setattr(direct_fetch, "fetch", lambda url: (200, listing_page))
    monkeypatch.setattr(screwfix.time, "sleep", lambda s: None)

    adapter = screwfix.ScrewfixAdapter(
        category_urls=["https://www.screwfix.com/search?search=clearancepromo"], min_delay_s=0, max_delay_s=0,
    )

    def _boom(raw):
        raise RuntimeError("simulated processing failure")

    from app.sources import crawl_state
    try:
        adapter.crawl(db_session, on_deal=_boom)
    except RuntimeError:
        pass

    assert crawl_state.check(db_session, "screwfix", "https://www.screwfix.com/p/widget/222b", 1000) == "new"


def test_crawl_skips_category_on_fetch_failure(db_session, monkeypatch):
    monkeypatch.setattr(direct_fetch, "fetch", lambda url: None)
    monkeypatch.setattr(screwfix.time, "sleep", lambda s: None)

    adapter = screwfix.ScrewfixAdapter(
        category_urls=["https://www.screwfix.com/search?search=clearancepromo"], min_delay_s=0, max_delay_s=0,
    )
    assert adapter.crawl(db_session, on_deal=lambda raw: None) == 0
