"""app/matching/cache.py: cache_product's check-then-insert behavior. Two
different EANs can legitimately resolve to the same ASIN (regional barcode
variants, multipacks) -- confirmed live 2026-07-24 as an uncaught
IntegrityError on products.asin's unique constraint, silently dropping that
deal's processing."""
from app import models
from app.matching import cache


def test_cache_product_new_ean_and_asin_creates_row(db_session):
    product = cache.cache_product(
        db_session, ean="5901234123457", asin="B000TEST01", title="Widget",
        matched_via="jsonld", confidence="high",
    )
    assert product.ean == "5901234123457"
    assert product.asin == "B000TEST01"


def test_cache_product_second_ean_for_same_asin_returns_existing_row(db_session):
    """The exact production scenario: EAN1 already cached against ASIN X;
    a different EAN2 resolves (via Keepa) to that same ASIN X. Must return
    the existing row, not attempt a second INSERT that violates products.
    asin's unique constraint."""
    first = cache.cache_product(
        db_session, ean="5901234123457", asin="B000TEST02", title="Widget",
        matched_via="jsonld", confidence="high",
    )

    second = cache.cache_product(
        db_session, ean="9999999999999", asin="B000TEST02", title="Widget (different retailer page)",
        matched_via="jsonld", confidence="high",
    )

    assert second.id == first.id
    assert second.ean == "5901234123457"   # the existing row's ean wins, not overwritten
    assert db_session.query(models.Product).filter(models.Product.asin == "B000TEST02").count() == 1


def test_cache_product_negative_cache_allows_multiple_null_asin_rows(db_session):
    """asin=None is the negative-match cache path (jsonld_no_match) -- must
    not be subject to the same-ASIN dedup check (there's no ASIN to dedupe
    on), and Postgres's unique constraint already allows multiple NULLs."""
    first = cache.cache_product(db_session, ean="1111111111111", asin=None, title=None, matched_via="jsonld_no_match", confidence="none")
    second = cache.cache_product(db_session, ean="2222222222222", asin=None, title=None, matched_via="jsonld_no_match", confidence="none")

    assert first.id != second.id
    assert first.asin is None and second.asin is None


def test_cache_product_race_condition_falls_back_to_existing_row(db_session, monkeypatch):
    """Simulates the rare true race: get_cached_by_asin misses (nothing
    cached yet at check time), but the row exists by the time we try to
    insert (another concurrent scheduler job won the race) -- the resulting
    IntegrityError must be caught and resolved to the existing row, not
    propagate up and drop the deal."""
    cache.cache_product(db_session, ean="3333333333333", asin="B000RACE01", title="Widget", matched_via="jsonld", confidence="high")

    # First call (the pre-check) misses, as if this session hadn't seen the
    # concurrent insert yet; the real IntegrityError from the doomed INSERT
    # then forces a second lookup, which must see it.
    real_get_cached_by_asin = cache.get_cached_by_asin
    call_count = {"n": 0}

    def flaky_get_cached_by_asin(db, asin):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None
        return real_get_cached_by_asin(db, asin)

    monkeypatch.setattr(cache, "get_cached_by_asin", flaky_get_cached_by_asin)

    result = cache.cache_product(db_session, ean="4444444444444", asin="B000RACE01", title="Widget", matched_via="jsonld", confidence="high")

    assert result.ean == "3333333333333"
    assert db_session.query(models.Product).filter(models.Product.asin == "B000RACE01").count() == 1
