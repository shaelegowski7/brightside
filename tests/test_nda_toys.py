"""NDA Toys wholesale deals scraper: listing-tile parsing (inc-VAT unit
price, stock status, rel="next" pagination), barcode extraction from a
product page's <td>Barcode</td> row, crawl_state-gated crawling -- a
product page (the expensive ultra_premium fetch) is only fetched when the
listing price is new or has changed -- and the persisted resume cursor
across category_urls (models.NdaToysCrawlProgress)."""
from app import models
from app.sources import nda_toys, scraperapi


def _tile(product_id: int, slug: str, title: str, pounds: int, pence: int, in_stock: bool = True) -> str:
    availability = "In Stock" if in_stock else "Out of Stock"
    return (
        '<div class="col"><div class="card h-100 nda-shadow abdets">'
        f'<a href="https://www.nda-toys.com/product/{product_id}/{slug}">'
        '<div class="cat-image-container">'
        f'<img src="https://www.nda-toys.com/images/{slug}.jpg" class="cat-image" loading="lazy">'
        "</div></a>"
        '<div class="card-body">'
        f'<a href="https://www.nda-toys.com/product/{product_id}/{slug}">'
        f'<h3 class="card-title mt-3 fs-6">{title}</h3></a>'
        '<div class="row">'
        f'<div class="col-12">Unit Price <span class="fs-4 fw-bold">£{pounds - 1}.99</span> '
        f'Inc VAT: £{pounds}.{pence:02d}</div>'
        '<div class="col-12">Availability: <span class="fs-4 fw-bold text-success">'
        f'<span class="fs-4 fw-bold text-success availB1">{availability}</span></span></div>'
        "</div></div></div></div>"
    )


def _listing_html(tiles: list[str], next_page: str | None = None) -> str:
    head = f'<link rel="next" href="{next_page}"/>' if next_page else ""
    return f"<html><head>{head}</head><body>{''.join(tiles)}</body></html>"


def _product_html(barcode: str | None) -> str:
    row = f"<tr><td>Barcode</td><td>{barcode}</td></tr>" if barcode else ""
    return f'<html><body><table class="table">{row}</table></body></html>'


def test_parse_listing_extracts_products():
    html = _listing_html([
        _tile(111, "widget-a", "Widget A", 6, 42),
        _tile(222, "widget-b", "Widget B", 3, 99),
    ])
    products = nda_toys._parse_listing(html)
    assert len(products) == 2
    assert products[0].title == "Widget A"
    assert products[0].buy_price_pence == 642
    assert products[0].url == "https://www.nda-toys.com/product/111/widget-a"
    assert products[0].in_stock is True
    assert products[1].buy_price_pence == 399


def test_parse_listing_no_matching_tiles_returns_empty():
    assert nda_toys._parse_listing("<html><body>no products here</body></html>") == []


def test_parse_listing_ignores_duplicate_hrefs():
    tile = _tile(111, "widget-a", "Widget A", 6, 42)
    html = _listing_html([tile, tile])
    assert len(nda_toys._parse_listing(html)) == 1


def test_parse_listing_marks_out_of_stock_items():
    html = _listing_html([_tile(111, "widget-a", "Widget A", 6, 42, in_stock=False)])
    products = nda_toys._parse_listing(html)
    assert len(products) == 1
    assert products[0].in_stock is False


def test_next_page_url_found():
    html = _listing_html([], next_page="/wholesale-toy-deals?page=2")
    assert nda_toys._next_page_url(html) == "https://www.nda-toys.com/wholesale-toy-deals?page=2"


def test_next_page_url_absent_on_last_page():
    html = _listing_html([])
    assert nda_toys._next_page_url(html) is None


def test_extract_nda_toys_ean_from_barcode_row():
    html = _product_html("5010996269539")
    assert nda_toys._extract_nda_toys_ean(html) == "5010996269539"


def test_extract_nda_toys_ean_returns_none_when_absent():
    assert nda_toys._extract_nda_toys_ean(_product_html(None)) is None


def test_crawl_fetches_product_page_only_for_new_or_changed_items(db_session, monkeypatch):
    listing_page_1 = _listing_html(
        [_tile(1111111, "widget", "Widget", 10, 0)],
        next_page="/wholesale-toy-deals?page=2",
    )
    listing_page_2 = _listing_html([])
    product_page = _product_html("111122223333")

    calls = []

    def fake_fetch(url, api_key, ultra_premium=False):
        calls.append(url)
        assert api_key == "test-key"
        assert ultra_premium is True  # every fetch here must use the expensive tier
        if url == "https://www.nda-toys.com/wholesale-toy-deals":
            return 200, listing_page_1
        if url == "https://www.nda-toys.com/wholesale-toy-deals?page=2":
            return 200, listing_page_2
        if url == "https://www.nda-toys.com/product/1111111/widget":
            return 200, product_page
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(scraperapi, "fetch", fake_fetch)
    monkeypatch.setattr(nda_toys.time, "sleep", lambda s: None)

    adapter = nda_toys.NdaToysAdapter(
        category_urls=["https://www.nda-toys.com/wholesale-toy-deals"],
        api_key="test-key", min_delay_s=0, max_delay_s=0,
    )

    deals = []
    count = adapter.crawl(db_session, on_deal=deals.append)
    assert count == 1
    assert len(deals) == 1
    assert deals[0].url == "https://www.nda-toys.com/product/1111111/widget"
    assert deals[0].buy_price_pence == 1000
    assert deals[0].html == product_page
    assert calls.count("https://www.nda-toys.com/product/1111111/widget") == 1

    # Second crawl, same price -> unchanged -> must not re-fetch the product page.
    calls.clear()
    deals_second = []
    count_second = adapter.crawl(db_session, on_deal=deals_second.append)
    assert count_second == 0
    assert deals_second == []
    assert "https://www.nda-toys.com/product/1111111/widget" not in calls


def test_crawl_skips_out_of_stock_items(db_session, monkeypatch):
    listing_page = _listing_html([_tile(3333333, "widget", "Widget", 10, 0, in_stock=False)])

    def fake_fetch(url, api_key, ultra_premium=False):
        if url == "https://www.nda-toys.com/wholesale-toy-deals":
            return 200, listing_page
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(scraperapi, "fetch", fake_fetch)
    monkeypatch.setattr(nda_toys.time, "sleep", lambda s: None)

    adapter = nda_toys.NdaToysAdapter(
        category_urls=["https://www.nda-toys.com/wholesale-toy-deals"],
        api_key="test-key", min_delay_s=0, max_delay_s=0,
    )
    assert adapter.crawl(db_session, on_deal=lambda raw: None) == 0


def test_crawl_does_not_mark_seen_when_on_deal_raises(db_session, monkeypatch):
    """Same guarantee as every other source: a transient processing failure
    must not permanently suppress a legitimate deal (see crawl_state.py's
    module docstring)."""
    listing_page = _listing_html([_tile(4444444, "widget", "Widget", 10, 0)])
    product_page = _product_html("111122223333")

    def fake_fetch(url, api_key, ultra_premium=False):
        if url == "https://www.nda-toys.com/wholesale-toy-deals":
            return 200, listing_page
        if url == "https://www.nda-toys.com/product/4444444/widget":
            return 200, product_page
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(scraperapi, "fetch", fake_fetch)
    monkeypatch.setattr(nda_toys.time, "sleep", lambda s: None)

    adapter = nda_toys.NdaToysAdapter(
        category_urls=["https://www.nda-toys.com/wholesale-toy-deals"],
        api_key="test-key", min_delay_s=0, max_delay_s=0,
    )

    def _boom(raw):
        raise RuntimeError("simulated processing failure")

    from app.sources import crawl_state
    try:
        adapter.crawl(db_session, on_deal=_boom)
    except RuntimeError:
        pass

    assert crawl_state.check(
        db_session, "nda_toys", "https://www.nda-toys.com/product/4444444/widget", 1000
    ) == "new"


def test_crawl_stops_listing_on_fetch_failure(db_session, monkeypatch):
    monkeypatch.setattr(scraperapi, "fetch", lambda url, api_key, ultra_premium=False: None)
    monkeypatch.setattr(nda_toys.time, "sleep", lambda s: None)

    adapter = nda_toys.NdaToysAdapter(
        category_urls=["https://www.nda-toys.com/wholesale-toy-deals"],
        api_key="test-key", min_delay_s=0, max_delay_s=0,
    )
    assert adapter.crawl(db_session, on_deal=lambda raw: None) == 0


# --- resume cursor (models.NdaToysCrawlProgress) ---


def test_crawl_skips_brands_already_marked_complete(db_session, monkeypatch):
    """Index 0 is pre-marked complete -- a fresh crawl must skip straight
    to index 1 without fetching brand 0's listing at all."""
    brand0 = "https://www.nda-toys.com/1/brand-zero-wholesale"
    brand1 = "https://www.nda-toys.com/2/brand-one-wholesale"
    listing1 = _listing_html([_tile(5555555, "widget", "Widget", 10, 0)])
    product1 = _product_html("111122223333")

    db_session.add(models.NdaToysCrawlProgress(id=1, completed_through_index=0))
    db_session.commit()

    calls = []

    def fake_fetch(url, api_key, ultra_premium=False):
        calls.append(url)
        if url == brand1:
            return 200, listing1
        if url == "https://www.nda-toys.com/product/5555555/widget":
            return 200, product1
        raise AssertionError(f"unexpected fetch (brand 0 should have been skipped): {url}")

    monkeypatch.setattr(scraperapi, "fetch", fake_fetch)
    monkeypatch.setattr(nda_toys.time, "sleep", lambda s: None)

    adapter = nda_toys.NdaToysAdapter(
        category_urls=[brand0, brand1], api_key="test-key", min_delay_s=0, max_delay_s=0,
    )
    count = adapter.crawl(db_session, on_deal=lambda raw: None)
    assert count == 1
    assert brand0 not in calls


def test_crawl_advances_progress_only_on_genuine_completion(db_session, monkeypatch):
    brand0 = "https://www.nda-toys.com/1/brand-zero-wholesale"
    brand1 = "https://www.nda-toys.com/2/brand-one-wholesale"
    listing0 = _listing_html([_tile(6666666, "widget", "Widget", 10, 0)])
    product0 = _product_html("111122223333")

    def fake_fetch(url, api_key, ultra_premium=False):
        if url == brand0:
            return 200, listing0
        if url == "https://www.nda-toys.com/product/6666666/widget":
            return 200, product0
        if url == brand1:
            return None  # brand 1's own listing fetch fails
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(scraperapi, "fetch", fake_fetch)
    monkeypatch.setattr(nda_toys.time, "sleep", lambda s: None)

    adapter = nda_toys.NdaToysAdapter(
        category_urls=[brand0, brand1], api_key="test-key", min_delay_s=0, max_delay_s=0,
    )
    adapter.crawl(db_session, on_deal=lambda raw: None)

    progress = db_session.get(models.NdaToysCrawlProgress, 1)
    # brand0 (index 0) completed genuinely -> cursor advances to 0.
    # brand1 (index 1) failed mid-fetch -> cursor must NOT advance to 1,
    # so it's retried in full on the next crawl rather than skipped.
    assert progress.completed_through_index == 0


def test_crawl_resumes_from_persisted_index_across_calls(db_session, monkeypatch):
    brand0 = "https://www.nda-toys.com/1/brand-zero-wholesale"
    brand1 = "https://www.nda-toys.com/2/brand-one-wholesale"
    listing0 = _listing_html([_tile(7777777, "widget-a", "Widget A", 10, 0)])
    listing1 = _listing_html([_tile(8888888, "widget-b", "Widget B", 20, 0)])
    product0 = _product_html("111100001111")
    product1 = _product_html("222200002222")

    def fake_fetch(url, api_key, ultra_premium=False):
        return {
            brand0: (200, listing0),
            "https://www.nda-toys.com/product/7777777/widget-a": (200, product0),
            brand1: (200, listing1),
            "https://www.nda-toys.com/product/8888888/widget-b": (200, product1),
        }[url]

    monkeypatch.setattr(scraperapi, "fetch", fake_fetch)
    monkeypatch.setattr(nda_toys.time, "sleep", lambda s: None)

    adapter = nda_toys.NdaToysAdapter(
        category_urls=[brand0, brand1], api_key="test-key", min_delay_s=0, max_delay_s=0,
    )

    first_deals = []
    adapter.crawl(db_session, on_deal=first_deals.append)
    assert [d.url for d in first_deals] == [
        "https://www.nda-toys.com/product/7777777/widget-a",
        "https://www.nda-toys.com/product/8888888/widget-b",
    ]

    # A brand-new adapter instance (simulating a fresh process after a
    # restart) must resume from the persisted cursor, not redo brand 0.
    calls = []
    monkeypatch.setattr(scraperapi, "fetch", lambda url, api_key, ultra_premium=False: (calls.append(url), fake_fetch(url, api_key, ultra_premium))[1])
    second_adapter = nda_toys.NdaToysAdapter(
        category_urls=[brand0, brand1], api_key="test-key", min_delay_s=0, max_delay_s=0,
    )
    second_deals = []
    second_adapter.crawl(db_session, on_deal=second_deals.append)
    assert brand0 not in calls
    assert second_deals == []  # brand1's item is unchanged since the first crawl -> skipped too
