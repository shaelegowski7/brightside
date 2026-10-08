from datetime import datetime, timedelta, timezone

import pytest

from app import keepa_cache, keepa_client, models


@pytest.fixture(autouse=True)
def _cache_reads_on(monkeypatch):
    monkeypatch.setattr(keepa_cache, "READS_ENABLED", True)


class _CountingClient:
    def __init__(self, products):
        self.tokens_left = 100
        self.products = products
        self.queried: list[list[str]] = []

    def query(self, codes, **kwargs):
        self.queried.append(list(codes))
        return [p for p in self.products if p["asin"] in codes or set(p.get("eanList") or []) & set(codes)]

    def seller_query(self, ids, **kwargs):
        return {}


def _product(asin="B0CACHE001", ean="5000000000001", price=2500):
    return {"asin": asin, "title": "Cached thing", "eanList": [ean],
            "stats": {"current": [], "avg90": [None] * 19, "buyBoxPrice": price}, "offers": []}


def _client(monkeypatch, *products):
    c = _CountingClient(list(products))
    monkeypatch.setattr(keepa_client, "_get_client", lambda: c)
    return c


def test_stage2_second_lookup_is_served_from_the_cache(db_session, monkeypatch):
    c = _client(monkeypatch, _product())
    first = keepa_client.stage2_full(db_session, ["B0CACHE001"])
    c.products[0]["stats"]["buyBoxPrice"] = 9999   # Keepa moved; the cache shouldn't ask
    second = keepa_client.stage2_full(db_session, ["B0CACHE001"])
    assert c.queried == [["B0CACHE001"]]
    assert first["B0CACHE001"].buybox_price_pence == second["B0CACHE001"].buybox_price_pence == 2500


def test_fresh_read_and_stale_data_go_back_to_keepa(db_session, monkeypatch):
    c = _client(monkeypatch, _product())
    keepa_client.stage2_full(db_session, ["B0CACHE001"])
    c.products[0]["stats"]["buyBoxPrice"] = 2200
    assert keepa_client.stage2_full(db_session, ["B0CACHE001"], max_age_days=0)["B0CACHE001"].buybox_price_pence == 2200

    row = db_session.get(models.KeepaCache, "B0CACHE001")
    row.stage2_fetched_at = datetime.now(timezone.utc) - timedelta(days=30)
    db_session.commit()
    c.products[0]["stats"]["buyBoxPrice"] = 2100
    assert keepa_client.stage2_full(db_session, ["B0CACHE001"])["B0CACHE001"].buybox_price_pence == 2100
    assert len(c.queried) == 3


def test_barcode_lookup_remembers_matches_and_misses(db_session, monkeypatch):
    c = _client(monkeypatch, _product())
    codes = ["5000000000001", "0000000000000"]
    first = keepa_client.stage1_by_code(db_session, codes)
    second = keepa_client.stage1_by_code(db_session, codes)
    assert c.queried == [codes]
    assert first == second
    assert set(first) == {"5000000000001"} and first["5000000000001"][0] == "B0CACHE001"
    assert db_session.get(models.KeepaCodeMap, "0000000000000").asin is None


def test_stage1_screen_by_asin_uses_the_cache(db_session, monkeypatch):
    c = _client(monkeypatch, _product())
    keepa_client.stage1_screen(db_session, ["B0CACHE001"], is_ean=False)
    keepa_client.stage1_screen(db_session, ["B0CACHE001"], is_ean=False)
    assert len(c.queried) == 1
