"""stock_state diffing: a "drop" is in-stock-now-and-wasn't-last-poll (or
first ever sighting already in stock) — used by restock/new-release
monitors (Phase 3, e.g. Pokemon Center) to avoid re-emitting on every poll
while an item stays in stock."""
from app import models
from app.sources import stock_state

_URL = "https://www.pokemoncenter.com/en-gb/product/10-10447-108/etb"


def test_first_sighting_in_stock_is_a_drop(db_session):
    assert stock_state.diff_and_record(db_session, "pokemon_center", _URL, True) is True

    row = db_session.get(models.StockState, ("pokemon_center", stock_state.url_hash(_URL)))
    assert row.in_stock is True


def test_first_sighting_out_of_stock_is_not_a_drop(db_session):
    assert stock_state.diff_and_record(db_session, "pokemon_center", _URL, False) is False


def test_still_in_stock_is_not_a_repeat_drop(db_session):
    assert stock_state.diff_and_record(db_session, "pokemon_center", _URL, True) is True
    assert stock_state.diff_and_record(db_session, "pokemon_center", _URL, True) is False


def test_transition_to_in_stock_is_a_drop(db_session):
    assert stock_state.diff_and_record(db_session, "pokemon_center", _URL, False) is False
    assert stock_state.diff_and_record(db_session, "pokemon_center", _URL, True) is True


def test_going_out_of_stock_then_back_is_a_drop_again(db_session):
    assert stock_state.diff_and_record(db_session, "pokemon_center", _URL, True) is True
    assert stock_state.diff_and_record(db_session, "pokemon_center", _URL, False) is False
    assert stock_state.diff_and_record(db_session, "pokemon_center", _URL, True) is True


def test_different_retailers_are_independent(db_session):
    assert stock_state.diff_and_record(db_session, "pokemon_center", _URL, True) is True
    assert stock_state.diff_and_record(db_session, "some_other_retailer", _URL, True) is True


def test_check_does_not_write(db_session):
    """check() alone must not mark the item seen -- callers that need
    interruption-safe deferred recording rely on this (see
    pokemon_center.py)."""
    assert stock_state.check(db_session, "pokemon_center", _URL, True) is True
    assert stock_state.check(db_session, "pokemon_center", _URL, True) is True   # still a "drop" -- check() never wrote

    row = db_session.get(models.StockState, ("pokemon_center", stock_state.url_hash(_URL)))
    assert row is None


def test_record_after_check_matches_diff_and_record(db_session):
    assert stock_state.check(db_session, "pokemon_center", _URL, True) is True
    stock_state.record(db_session, "pokemon_center", _URL, True)

    assert stock_state.check(db_session, "pokemon_center", _URL, True) is False   # no longer a drop, already seen in stock
