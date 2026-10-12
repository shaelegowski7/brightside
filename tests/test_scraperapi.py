import requests

from app.sources import scraperapi

KEY = "0123456789abcdef0123456789abcdef"


class _Resp:
    def raise_for_status(self):
        raise requests.HTTPError(
            f"500 Server Error: Internal Server Error for url: "
            f"https://api.scraperapi.com/structured/google/search?api_key={KEY}&query=x"
        )


def test_google_search_failure_log_never_contains_the_api_key(monkeypatch, capsys):
    """requests' HTTPError message carries the full URL, api_key included,
    and the logs these prints go to are kept in the Obsidian vault."""
    monkeypatch.setattr(scraperapi.requests, "get", lambda *a, **k: _Resp())
    assert scraperapi.google_search("x", KEY) == []
    out = capsys.readouterr().out
    assert "failed" in out and KEY not in out


def test_fetch_failure_log_never_contains_the_api_key(monkeypatch, capsys):
    def boom(*a, **k):
        raise requests.ConnectionError(f"Max retries exceeded with url: /?api_key={KEY}&url=x")

    monkeypatch.setattr(scraperapi.requests, "get", boom)
    assert scraperapi.fetch("https://example.com", KEY) is None
    out = capsys.readouterr().out
    assert "failed" in out and KEY not in out
