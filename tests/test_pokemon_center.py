"""Pokemon Center restock monitor: JSON-LD Product/Offer parsing off the
listing page (confirmed live 2026-07-21 — see pokemon_center.py's module
docstring for why this differs from Argos: no EAN anywhere, and listing-tile
data is occasionally low quality, hence re-deriving price/image from the
product page itself), and stock_state-gated crawling — a product page is
only fetched when an item just became available."""
import json

from app.sources import pokemon_center, scraperapi


def _product_ld(name: str, url: str, price: float, currency: str, in_stock: bool, images=None) -> dict:
    return {
        "@context": "https://schema.org/",
        "@type": "Product",
        "name": name,
        "image": images or [],
        "offers": {
            "@type": "Offer",
            "priceCurrency": currency,
            "price": price,
            "url": url,
            "availability": f"http://schema.org/{'InStock' if in_stock else 'OutOfStock'}",
        },
    }


def _listing_html(products: list[dict]) -> str:
    tags = "".join(f'<script type="application/ld+json">{json.dumps(p)}</script>' for p in products)
    return f"<html><body>{tags}</body></html>"


def test_parse_listing_extracts_products():
    html = _listing_html([
        _product_ld("Elite Trainer Box", "https://www.pokemoncenter.com/en-gb/product/1/etb", 56.99, "GBP", True),
        _product_ld("Booster Bundle", "https://www.pokemoncenter.com/en-gb/product/2/bb", 24.99, "GBP", False),
    ])
    products = pokemon_center._parse_listing(html)
    assert len(products) == 2
    assert products[0].title == "Elite Trainer Box"
    assert products[0].price_pence == 5699
    assert products[0].in_stock is True
    assert products[1].in_stock is False


def test_parse_listing_ignores_non_product_ld_json():
    html = '<script type="application/ld+json">{"@type": "BreadcrumbList"}</script>'
    assert pokemon_center._parse_listing(html) == []


def test_parse_listing_skips_entries_with_no_url():
    ld = _product_ld("No URL Item", "", 10.0, "GBP", True)
    ld["offers"]["url"] = ""
    ld["url"] = ""
    html = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
    assert pokemon_center._parse_listing(html) == []


def test_product_page_details_prefers_gbp_and_first_image():
    html = _listing_html([_product_ld(
        "Elite Trainer Box", "https://www.pokemoncenter.com/en-gb/product/1/etb",
        56.99, "GBP", True, images=["https://example.com/img1.jpg", "https://example.com/img2.jpg"],
    )])
    price_pence, image_url = pokemon_center._product_page_details(html)
    assert price_pence == 5699
    assert image_url == "https://example.com/img1.jpg"


def test_product_page_details_ignores_non_gbp_price():
    # Confirmed live: one listing tile mislabeled priceCurrency as USD with
    # blank sku/image — treat a non-GBP offer as "no reliable price" rather
    # than silently using the wrong currency's number.
    html = _listing_html([_product_ld(
        "Glitchy Item", "https://www.pokemoncenter.com/en-gb/product/9/glitch", 54.99, "USD", False,
    )])
    price_pence, image_url = pokemon_center._product_page_details(html)
    assert price_pence is None
    assert image_url is None


def test_crawl_fetches_product_page_only_on_drop(db_session, monkeypatch):
    listing_html = _listing_html([
        _product_ld("Elite Trainer Box", "https://www.pokemoncenter.com/en-gb/product/1/etb", 56.99, "GBP", True),
    ])
    product_html = _listing_html([_product_ld(
        "Elite Trainer Box", "https://www.pokemoncenter.com/en-gb/product/1/etb",
        56.99, "GBP", True, images=["https://example.com/etb.jpg"],
    )])

    calls = []

    def fake_fetch(url, api_key):
        calls.append(url)
        assert api_key == "test-key"
        if url == "https://www.pokemoncenter.com/en-gb/category/new-releases":
            return 200, listing_html
        if url == "https://www.pokemoncenter.com/en-gb/product/1/etb":
            return 200, product_html
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(scraperapi, "fetch", fake_fetch)
    monkeypatch.setattr(pokemon_center.time, "sleep", lambda s: None)

    adapter = pokemon_center.PokemonCenterAdapter(
        category_urls=["https://www.pokemoncenter.com/en-gb/category/new-releases"],
        api_key="test-key", min_delay_s=0, max_delay_s=0,
    )

    deals = []
    count = adapter.crawl(db_session, on_deal=deals.append)
    assert count == 1
    assert len(deals) == 1
    assert deals[0].url == "https://www.pokemoncenter.com/en-gb/product/1/etb"
    assert deals[0].buy_price_pence == 5699
    assert deals[0].image_url == "https://example.com/etb.jpg"
    assert calls.count("https://www.pokemoncenter.com/en-gb/product/1/etb") == 1

    # Second crawl, still in stock -> not a new drop -> no re-fetch of the product page.
    calls.clear()
    deals_second = []
    count_second = adapter.crawl(db_session, on_deal=deals_second.append)
    assert count_second == 0
    assert deals_second == []
    assert "https://www.pokemoncenter.com/en-gb/product/1/etb" not in calls


def test_crawl_does_not_mark_seen_when_on_deal_raises(db_session, monkeypatch):
    """If processing a drop fails, it must not be recorded in stock_state --
    otherwise a transient failure would permanently suppress a legitimate
    restock (see stock_state.py's module docstring)."""
    listing_html = _listing_html([
        _product_ld("Booster Bundle", "https://www.pokemoncenter.com/en-gb/product/9/bb", 24.99, "GBP", True),
    ])

    def fake_fetch(url, api_key):
        if url == "https://www.pokemoncenter.com/en-gb/category/new-releases":
            return 200, listing_html
        if url == "https://www.pokemoncenter.com/en-gb/product/9/bb":
            return 200, listing_html
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(scraperapi, "fetch", fake_fetch)
    monkeypatch.setattr(pokemon_center.time, "sleep", lambda s: None)

    adapter = pokemon_center.PokemonCenterAdapter(
        category_urls=["https://www.pokemoncenter.com/en-gb/category/new-releases"],
        api_key="test-key", min_delay_s=0, max_delay_s=0,
    )

    def _boom(raw):
        raise RuntimeError("simulated processing failure")

    from app.sources import stock_state
    try:
        adapter.crawl(db_session, on_deal=_boom)
    except RuntimeError:
        pass

    assert stock_state.check(db_session, "pokemon_center", "https://www.pokemoncenter.com/en-gb/product/9/bb", True) is True
