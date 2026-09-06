"""stage2_full's field-parsing off a raw Keepa product payload -- previously
untested (every other test mocks stage2_full itself rather than exercising
its internals). Covers the amazon_on_listing fix: it must reflect whether
Amazon has a live new-condition offer, not just momentary buy-box
ownership (see keepa_client.py's module docstring for the false-negative
that prompted this, caught on B0BXX8X7DM 2026-07-28)."""
import pytest
import requests
from urllib3.exceptions import ProtocolError

from app import keepa_client


class _FakeKeepaClient:
    def __init__(self, products: list[dict]):
        self.tokens_left = 100
        self._products = products

    def query(self, *args, **kwargs):
        return self._products


def _product(asin: str = "B0TEST0001", offers: list[dict] | None = None, buy_box_is_amazon: bool = False) -> dict:
    return {
        "asin": asin,
        "title": "Test Product",
        "stats": {
            "current": [],
            "avg90": [],
            "buyBoxIsAmazon": buy_box_is_amazon,
            "buyBoxPrice": 1000,
        },
        "offers": offers or [],
    }


def test_amazon_on_listing_true_when_amazon_offer_present_but_not_buybox_winner(db_session, monkeypatch):
    """The exact scenario that slipped through: buyBoxIsAmazon False (a 3rd
    party FBM seller currently holds it) but Amazon still has a live new
    offer -- a real competitive risk the old buy-box-only check missed."""
    product = _product(offers=[
        {"sellerId": "THIRD_PARTY", "isAmazon": False, "condition": 1},
        {"sellerId": "AMAZON", "isAmazon": True, "isFBA": True, "condition": 1},
    ], buy_box_is_amazon=False)
    monkeypatch.setattr(keepa_client, "_get_client", lambda: _FakeKeepaClient([product]))

    results = keepa_client.stage2_full(db_session, ["B0TEST0001"])

    assert results["B0TEST0001"].amazon_on_listing is True


def test_amazon_on_listing_false_when_no_amazon_offer(db_session, monkeypatch):
    product = _product(offers=[
        {"sellerId": "THIRD_PARTY", "isAmazon": False, "condition": 1},
    ])
    monkeypatch.setattr(keepa_client, "_get_client", lambda: _FakeKeepaClient([product]))

    results = keepa_client.stage2_full(db_session, ["B0TEST0001"])

    assert results["B0TEST0001"].amazon_on_listing is False


def test_amazon_on_listing_ignores_amazon_used_offer(db_session, monkeypatch):
    """A used-condition Amazon offer isn't the same competitive threat as a
    new one -- only condition==1 (New) should count."""
    product = _product(offers=[
        {"sellerId": "AMAZON", "isAmazon": True, "condition": 2},
    ])
    monkeypatch.setattr(keepa_client, "_get_client", lambda: _FakeKeepaClient([product]))

    results = keepa_client.stage2_full(db_session, ["B0TEST0001"])

    assert results["B0TEST0001"].amazon_on_listing is False


def test_amazon_on_listing_true_even_when_buybox_is_amazon_flag_stale(db_session, monkeypatch):
    """Sanity check the fix doesn't accidentally still key off buyBoxIsAmazon
    -- flip it True with no matching offer and confirm it's ignored."""
    product = _product(offers=[
        {"sellerId": "THIRD_PARTY", "isAmazon": False, "condition": 1},
    ], buy_box_is_amazon=True)
    monkeypatch.setattr(keepa_client, "_get_client", lambda: _FakeKeepaClient([product]))

    results = keepa_client.stage2_full(db_session, ["B0TEST0001"])

    assert results["B0TEST0001"].amazon_on_listing is False


class _FlakyClient:
    """Raises `exc` on the first `fail_times` calls, then succeeds."""

    def __init__(self, exc: Exception, fail_times: int):
        self.tokens_left = 100
        self._exc = exc
        self._left = fail_times
        self.calls = 0

    def query(self, *args, **kwargs):
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            raise self._exc
        return ["ok"]


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.ConnectionError("connection aborted"),
        # The three that killed real multi-hour scans while the retry
        # clause still caught only ConnectionError -- a socket dropped
        # during a long Keepa token-wait surfaces as any of these.
        requests.exceptions.ChunkedEncodingError("connection broken"),
        requests.exceptions.ReadTimeout("read timed out"),
        ProtocolError("Remote end closed connection without response"),
    ],
    ids=["connection", "chunked_encoding", "read_timeout", "protocol"],
)
def test_query_with_retry_recovers_from_dropped_socket(exc, monkeypatch):
    monkeypatch.setattr(keepa_client.time, "sleep", lambda _s: None)
    client = _FlakyClient(exc, fail_times=1)
    assert keepa_client._query_with_retry(client) == ["ok"]
    assert client.calls == 2


def test_query_with_retry_reraises_after_last_attempt(monkeypatch):
    monkeypatch.setattr(keepa_client.time, "sleep", lambda _s: None)
    client = _FlakyClient(requests.exceptions.ReadTimeout("gone"), fail_times=99)
    with pytest.raises(requests.exceptions.ReadTimeout):
        keepa_client._query_with_retry(client)
    assert client.calls == keepa_client._QUERY_RETRIES


def test_query_with_retry_does_not_swallow_programming_errors(monkeypatch):
    """A bad request is a bug, not a flaky socket -- it must surface at once."""
    monkeypatch.setattr(keepa_client.time, "sleep", lambda _s: None)
    client = _FlakyClient(ValueError("bad argument"), fail_times=99)
    with pytest.raises(ValueError):
        keepa_client._query_with_retry(client)
    assert client.calls == 1
