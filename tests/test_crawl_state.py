"""crawl_state diffing: new item / price drop / unchanged classification,
used by retailer clearance scrapers (Phase 2) to decide whether to emit an
event for an item on this crawl."""
from app import models
from app.sources import crawl_state

_URL = "https://www.argos.co.uk/product/1234567"


def test_first_sighting_is_new(db_session):
    result = crawl_state.diff_and_record(db_session, "argos", _URL, 1999)
    assert result == "new"

    row = db_session.get(models.CrawlState, ("argos", crawl_state.url_hash(_URL)))
    assert row.last_price == 1999


def test_same_price_is_unchanged(db_session):
    crawl_state.diff_and_record(db_session, "argos", _URL, 1999)
    result = crawl_state.diff_and_record(db_session, "argos", _URL, 1999)
    assert result == "unchanged"


def test_lower_price_is_price_drop(db_session):
    crawl_state.diff_and_record(db_session, "argos", _URL, 1999)
    result = crawl_state.diff_and_record(db_session, "argos", _URL, 1499)
    assert result == "price_drop"

    row = db_session.get(models.CrawlState, ("argos", crawl_state.url_hash(_URL)))
    assert row.last_price == 1499


def test_higher_price_is_unchanged_but_still_recorded(db_session):
    crawl_state.diff_and_record(db_session, "argos", _URL, 1999)
    result = crawl_state.diff_and_record(db_session, "argos", _URL, 2499)
    assert result == "unchanged"

    # A later drop is measured against the new (higher) last-seen price, not
    # the original one.
    result = crawl_state.diff_and_record(db_session, "argos", _URL, 2199)
    assert result == "price_drop"


def test_different_retailers_are_independent(db_session):
    assert crawl_state.diff_and_record(db_session, "argos", _URL, 1999) == "new"
    assert crawl_state.diff_and_record(db_session, "currys", _URL, 1999) == "new"


def test_url_hash_differs_per_url():
    assert crawl_state.url_hash(_URL) != crawl_state.url_hash(_URL + "?x=1")


def test_check_does_not_write(db_session):
    """check() alone must not mark the item seen -- callers that need
    interruption-safe deferred recording rely on this (see argos.py)."""
    assert crawl_state.check(db_session, "argos", _URL, 1999) == "new"
    assert crawl_state.check(db_session, "argos", _URL, 1999) == "new"   # still "new" -- check() never wrote

    row = db_session.get(models.CrawlState, ("argos", crawl_state.url_hash(_URL)))
    assert row is None


def test_record_after_check_matches_diff_and_record(db_session):
    diff = crawl_state.check(db_session, "argos", _URL, 1999)
    crawl_state.record(db_session, "argos", _URL, 1999)
    assert diff == "new"

    diff2 = crawl_state.check(db_session, "argos", _URL, 1499)
    assert diff2 == "price_drop"
    crawl_state.record(db_session, "argos", _URL, 1499)

    assert crawl_state.check(db_session, "argos", _URL, 1499) == "unchanged"
