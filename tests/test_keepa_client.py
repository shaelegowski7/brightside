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
    def __init__(self, products: list[dict], sellers: dict | None = None):
        self.tokens_left = 100
        self._products = products
        self._sellers = sellers or {}

    def query(self, *args, **kwargs):
        return self._products

    def seller_query(self, ids, **kwargs):
        return {i: self._sellers[i] for i in ids if i in self._sellers}


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


def test_amazon_on_listing_ignores_a_dead_amazon_offer(db_session, monkeypatch):
    """B000T8Z6QQ: Amazon's offer was still in the array two months after it
    sold out, but not in liveOffersOrder."""
    product = _product(offers=[
        {"sellerId": "AMAZON", "isAmazon": True, "isFBA": True, "condition": 1},
        {"sellerId": "THIRD_PARTY", "isAmazon": False, "condition": 1},
    ])
    product["liveOffersOrder"] = [1]
    monkeypatch.setattr(keepa_client, "_get_client", lambda: _FakeKeepaClient([product]))
    assert keepa_client.stage2_full(db_session, ["B0TEST0001"])["B0TEST0001"].amazon_on_listing is False

    product["liveOffersOrder"] = [0, 1]
    assert keepa_client.stage2_full(db_session, ["B0TEST0001"])["B0TEST0001"].amazon_on_listing is True


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


def _count_new_csv(counts: list[int]) -> list:
    csv: list = [None] * 12
    csv[keepa_client._IDX_COUNT_NEW] = [v for i, c in enumerate(counts) for v in (1000 + i, c)]
    return csv


def test_seller_history_single_seller_listing(db_session, monkeypatch):
    product = _product(offers=[{"sellerId": "BRANDOWNER", "condition": 1}])
    product["buyBoxSellerIdHistory"] = ["1000", "BRANDOWNER", "2000", "-1", "3000", "BRANDOWNER"]   # all strings, as Keepa sends it
    product["csv"] = _count_new_csv([1, 0, 1])
    monkeypatch.setattr(keepa_client, "_get_client", lambda: _FakeKeepaClient([product]))

    r = keepa_client.stage2_full(db_session, ["B0TEST0001"])["B0TEST0001"]

    assert (r.distinct_sellers_ever, r.max_new_offers_ever) == (1, 1)


def test_seller_history_unions_offers_and_buybox_history(db_session, monkeypatch):
    product = _product(offers=[{"sellerId": "A", "condition": 1}])
    product["buyBoxSellerIdHistory"] = ["1000", "B", "2000", "C"]
    product["csv"] = _count_new_csv([1, 3, 2])
    monkeypatch.setattr(keepa_client, "_get_client", lambda: _FakeKeepaClient([product]))

    r = keepa_client.stage2_full(db_session, ["B0TEST0001"])["B0TEST0001"]

    assert (r.distinct_sellers_ever, r.max_new_offers_ever) == (3, 3)


def test_seller_history_unknown_when_payload_has_none(db_session, monkeypatch):
    monkeypatch.setattr(keepa_client, "_get_client", lambda: _FakeKeepaClient([_product()]))

    r = keepa_client.stage2_full(db_session, ["B0TEST0001"])["B0TEST0001"]

    assert (r.distinct_sellers_ever, r.max_new_offers_ever) == (None, None)


def test_rank_drops_no_longer_estimate_sales(db_session, monkeypatch):
    product = _product()
    product["stats"]["salesRankDrops30"] = 80
    monkeypatch.setattr(keepa_client, "_get_client", lambda: _FakeKeepaClient([product]))

    r = keepa_client.stage2_full(db_session, ["B0TEST0001"])["B0TEST0001"]

    assert (r.est_monthly_sales, r.est_monthly_sales_source) == (None, None)


def test_monthly_sold_badge_is_the_sales_figure(db_session, monkeypatch):
    product = _product()
    product["monthlySold"] = 200
    product["stats"]["salesRankDrops30"] = 30
    monkeypatch.setattr(keepa_client, "_get_client", lambda: _FakeKeepaClient([product]))

    r = keepa_client.stage2_full(db_session, ["B0TEST0001"])["B0TEST0001"]

    assert (r.est_monthly_sales, r.est_monthly_sales_source) == (200.0, "keepa_confirmed")


def _stage2_of(db_session, monkeypatch, product, sellers=None):
    keepa_client._seller_name_cache.clear()
    monkeypatch.setattr(keepa_client, "_get_client", lambda: _FakeKeepaClient([product], sellers))
    return keepa_client.stage2_full(db_session, [product["asin"]])[product["asin"]]


def test_variation_child_keeps_its_own_badge(db_session, monkeypatch):
    product = _product()
    product.update(monthlySold=400, parentAsin="B0PARENT01")
    r = _stage2_of(db_session, monkeypatch, product)
    assert (r.est_monthly_sales, r.est_monthly_sales_source, r.is_variation) == (400.0, "keepa_confirmed", True)


def test_amazon_instock_share_from_out_of_stock_pct(db_session, monkeypatch):
    product = _product()
    product["stats"]["outOfStockPercentage90"] = [16, 0]   # LEGO 72036, live 2026-10-01
    assert _stage2_of(db_session, monkeypatch, product).amazon_instock_pct_90 == pytest.approx(0.84)


def test_fba_count_30_days_ago_and_leaf_rank_90_day_average(db_session, monkeypatch):
    now, day = keepa_client._keepa_now(), keepa_client._DAY_MINUTES
    product = _product()
    csv: list = [None] * 35
    csv[keepa_client._IDX_COUNT_NEW_FBA] = [now - 60 * day, 2, now - 10 * day, 5]
    product["csv"] = csv
    # rank 1000 for the first 60 of the last 90 days, then 4000 for 30: avg 2000
    product["categoryTree"] = [{"catId": 1, "name": "Toys & Games"}, {"catId": 2, "name": "Building Sets"}]
    product["salesRanks"] = {"2": [now - 200 * day, 1000, now - 30 * day, 4000]}
    r = _stage2_of(db_session, monkeypatch, product)
    assert r.fba_offer_count_30d_ago == 2
    assert r.leaf_rank_avg90 == 2000


def test_brand_matching_the_buybox_seller(db_session, monkeypatch):
    product = _product()
    product["brand"] = "Nespresso"
    product["stats"]["buyBoxSellerId"] = "A364RD88VFRM3J"
    r = _stage2_of(db_session, monkeypatch, product, {"A364RD88VFRM3J": {"sellerName": "Nespresso UK Ltd"}})
    assert r.brand_matches_main_seller is True
    product["brand"] = "Emtrix"
    r = _stage2_of(db_session, monkeypatch, product, {"A364RD88VFRM3J": {"sellerName": "Footcare UK"}})
    assert r.brand_matches_main_seller is False


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


class _FlatFees:
    """Fees big enough that nothing sub-£20 could ever clear them, which is
    the real-world shape (referral + fulfilment + unrecoverable VAT ~= £7.50
    on a £9 sale)."""
    def get_fees(self, category, sell_price_pence, dims=None, **kwargs):
        from app.decision.engine import FeeInput
        return FeeInput(
            referral_fee_pence=int(sell_price_pence * 0.15),
            fba_fulfilment_fee_pence=250,
            monthly_storage_fee_pence=5,
            estimated=True,
        )

    def classify_size_tier(self, dims):
        return "standard"


class _HeavyFees(_FlatFees):
    """A bottle of 90 tablets, not a lipstick. Real Valupak VP037 fees came
    to about £7 of an £8.96 sale — the shape that makes absolute profit fail
    while ROI still looks healthy."""
    def get_fees(self, category, sell_price_pence, dims=None, **kwargs):
        from app.decision.engine import FeeInput
        return FeeInput(
            referral_fee_pence=int(sell_price_pence * 0.15),
            fba_fulfilment_fee_pence=450,
            monthly_storage_fee_pence=20,
            estimated=True,
        )


def _stage1_cfg(**overrides):
    from tests.test_decision_engine import default_config
    return default_config(**overrides)


def _stage1(price_pence):
    return keepa_client.Stage1Result(
        asin="B000TEST01", title="t", category="Toys & Games",
        sales_rank=20000, est_sell_price_pence=price_pence, rank_history_days=200,
    )


def test_stage1_screen_rejects_on_absolute_profit_not_just_roi():
    """The real filter. A 1p buy price makes the optimistic ROI enormous, so
    ROI cannot be what rejects a £8.96 item whose fees eat almost all of it —
    only the net-profit check catches it, and it must catch it at stage 1 so
    the stage-2 lookup is never spent. (Valupak VP037, 2026-09-08.)"""
    passes, reason = keepa_client.stage1_screen_passes(
        _stage1(896), buy_price_pence=1,
        cfg=_stage1_cfg(), fees=_HeavyFees(),
    )
    assert passes is False
    assert "net profit" in reason


def test_stage1_screen_keeps_cheap_goods_that_still_clear_three_pounds():
    """The case a flat £20 sell-price floor would have wrongly killed:
    clearance cosmetics bought at £1.25 and selling at £14 clear £3 net
    comfortably, so they must survive to stage 2."""
    passes, reason = keepa_client.stage1_screen_passes(
        _stage1(1400), buy_price_pence=125,
        cfg=_stage1_cfg(), fees=_FlatFees(),
    )
    assert passes is True and reason is None


def test_stage1_screen_optional_price_floor_still_available():
    """Off by default, but honoured when explicitly set."""
    passes, reason = keepa_client.stage1_screen_passes(
        _stage1(896), buy_price_pence=1,
        cfg=_stage1_cfg(min_sell_price_pence=2000), fees=_FlatFees(),
    )
    assert passes is False
    assert "below 2000p floor" in reason


def test_stage1_screen_allows_healthy_item():
    passes, reason = keepa_client.stage1_screen_passes(
        _stage1(2000), buy_price_pence=500,
        cfg=_stage1_cfg(min_sell_price_pence=2000), fees=_FlatFees(),
    )
    assert passes is True and reason is None


def test_stage1_screen_price_floor_does_not_fire_without_price_history():
    """No price to screen on means stage 2 makes the real call — the floor
    must not turn 'unknown' into 'reject'."""
    passes, _ = keepa_client.stage1_screen_passes(
        _stage1(None), buy_price_pence=1,
        cfg=_stage1_cfg(min_sell_price_pence=2000), fees=_FlatFees(),
    )
    assert passes is True
