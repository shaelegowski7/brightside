"""stage2_full's field-parsing off a raw Keepa product payload -- previously
untested (every other test mocks stage2_full itself rather than exercising
its internals). Covers the amazon_on_listing fix: it must reflect whether
Amazon has a live new-condition offer, not just momentary buy-box
ownership (see keepa_client.py's module docstring for the false-negative
that prompted this, caught on B0BXX8X7DM 2026-07-28)."""
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
