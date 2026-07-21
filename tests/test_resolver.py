from app import resolver
from app.resolver import thread_id_from_url
from app.sources import scraperapi


def test_thread_id_from_real_url():
    url = "https://www.hotukdeals.com/deals/jameson-original-triple-distilled-blended-irish-whiskey-70cl-4938754"
    assert thread_id_from_url(url) == "4938754"


def test_thread_id_from_url_with_trailing_slash():
    url = "https://www.hotukdeals.com/deals/some-slug-123456/"
    assert thread_id_from_url(url) == "123456"


def test_thread_id_returns_none_for_non_thread_url():
    assert thread_id_from_url("https://www.hotukdeals.com/hot") is None


class _FakeResponse:
    def __init__(self, status_code: int, url: str, text: str = ""):
        self.status_code = status_code
        self.url = url
        self.text = text


def test_resolve_returns_html_on_direct_success(monkeypatch):
    monkeypatch.setattr(resolver.requests, "get", lambda *a, **kw: _FakeResponse(200, "https://www.amazon.co.uk/dp/B000WIDGT1", "<html>ok</html>"))

    result = resolver.resolve("https://www.hotukdeals.com/deals/widget-1234567")

    assert result.blocked is False
    assert result.status_code == 200
    assert result.final_url == "https://www.amazon.co.uk/dp/B000WIDGT1"
    assert result.html == "<html>ok</html>"


def test_resolve_retries_blocked_retailer_via_proxy(monkeypatch):
    # HUKD's own redirect resolves fine; the retailer itself 403s on direct
    # fetch (confirmed live 2026-07-21 pattern: HUKD is never blocked, only
    # downstream retailers are).
    monkeypatch.setattr(resolver.requests, "get", lambda *a, **kw: _FakeResponse(403, "https://www.tesco.com/products/1"))

    proxy_calls = []

    def fake_proxy_fetch(url, api_key):
        proxy_calls.append((url, api_key))
        return 200, "<html>proxy content</html>"
    monkeypatch.setattr(scraperapi, "fetch", fake_proxy_fetch)

    result = resolver.resolve("https://www.hotukdeals.com/deals/oil-1234567", scraperapi_key="test-key")

    assert result.blocked is False
    assert result.status_code == 200
    assert result.final_url == "https://www.tesco.com/products/1"
    assert result.html == "<html>proxy content</html>"
    assert proxy_calls == [("https://www.tesco.com/products/1", "test-key")]


def test_resolve_stays_blocked_when_proxy_also_fails(monkeypatch):
    monkeypatch.setattr(resolver.requests, "get", lambda *a, **kw: _FakeResponse(403, "https://www.tesco.com/products/1"))
    monkeypatch.setattr(scraperapi, "fetch", lambda url, api_key: None)

    result = resolver.resolve("https://www.hotukdeals.com/deals/oil-1234567", scraperapi_key="test-key")

    assert result.blocked is True
    assert result.html is None
    assert result.status_code == 403


def test_resolve_skips_proxy_retry_without_a_key(monkeypatch):
    monkeypatch.setattr(resolver.requests, "get", lambda *a, **kw: _FakeResponse(403, "https://www.tesco.com/products/1"))

    def _boom(url, api_key):
        raise AssertionError("must not call the proxy without a configured key")
    monkeypatch.setattr(scraperapi, "fetch", _boom)

    result = resolver.resolve("https://www.hotukdeals.com/deals/oil-1234567")   # no scraperapi_key passed

    assert result.blocked is True
    assert result.html is None
