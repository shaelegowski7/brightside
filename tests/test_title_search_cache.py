"""7-day negative-result cache for the model-number/title Keepa search
fallback — spec: "cache negative results too (don't re-search the same
failed title within 7 days)". Positive matches never expire."""
from datetime import datetime, timedelta, timezone

from app import models
from app.matching import title_search_cache


def test_miss_when_never_searched(db_session):
    assert title_search_cache.get_cached(db_session, "AF300UK") == (False, None)


def test_positive_result_is_cached_permanently(db_session):
    title_search_cache.record(db_session, "AF300UK", "B000WIDGT1")
    assert title_search_cache.get_cached(db_session, "AF300UK") == (True, "B000WIDGT1")


def test_negative_result_is_cached_within_ttl(db_session):
    title_search_cache.record(db_session, "AF300UK", None)
    assert title_search_cache.get_cached(db_session, "AF300UK") == (True, None)


def test_negative_result_expires_after_ttl(db_session):
    title_search_cache.record(db_session, "AF300UK", None)
    row = db_session.get(models.TitleSearchCache, "AF300UK")
    row.searched_at = datetime.now(timezone.utc) - timedelta(days=8)
    db_session.commit()

    assert title_search_cache.get_cached(db_session, "AF300UK") == (False, None)


def test_re_recording_updates_existing_row(db_session):
    title_search_cache.record(db_session, "AF300UK", None)
    title_search_cache.record(db_session, "AF300UK", "B000WIDGT1")
    assert title_search_cache.get_cached(db_session, "AF300UK") == (True, "B000WIDGT1")
