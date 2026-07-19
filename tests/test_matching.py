from app.matching.amazon_url import extract_asin
from app.matching.jsonld import extract_ean


def test_extract_asin_from_real_confirmed_redirect_url():
    # Captured live 2026-07-19 from a hotukdeals -> amazon.co.uk redirect.
    url = "https://www.amazon.co.uk/dp/B000WIQLG2?smid=A3P5ROKL5A1OLE&tag=pepperugc03-21&ascsubtag=ppr-uk-3008030905"
    assert extract_asin(url) == "B000WIQLG2"


def test_extract_asin_non_amazon_host_returns_none():
    assert extract_asin("https://www.very.co.uk/some-product/123.prd") is None


def test_extract_asin_gp_product_path():
    assert extract_asin("https://www.amazon.co.uk/gp/product/B01N5IB20Q/") == "B01N5IB20Q"


def test_extract_ean_from_ld_json_product():
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Product","name":"Widget",
     "gtin13":"5060378730712",
     "offers":{"@type":"Offer","price":"19.99","priceCurrency":"GBP"}}
    </script>
    </head><body></body></html>
    """
    assert extract_ean("https://www.example-retailer.co.uk/p/widget", html) == "5060378730712"


def test_extract_ean_from_ld_json_graph_array():
    html = """
    <script type="application/ld+json">
    {"@context":"https://schema.org","@graph":[
        {"@type":"BreadcrumbList"},
        {"@type":"Product","name":"Widget","gtin":"01234565"}
    ]}
    </script>
    """
    assert extract_ean("https://www.example-retailer.co.uk/p/widget", html) == "01234565"


def test_extract_ean_from_microdata_fallback():
    html = """
    <div itemscope itemtype="https://schema.org/Product">
      <span itemprop="gtin13" content="5012345678900"></span>
    </div>
    """
    assert extract_ean("https://www.example-retailer.co.uk/p/widget", html) == "5012345678900"


def test_extract_ean_returns_none_when_absent():
    html = "<html><body><p>No structured data here.</p></body></html>"
    assert extract_ean("https://www.example-retailer.co.uk/p/widget", html) is None
