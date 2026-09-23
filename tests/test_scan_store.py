"""Mapping from scan checkpoint records to supplier_scan_results rows.

Covers the two things that would quietly lose data: a stage-1-only product
being dropped instead of stored, and a re-scan creating a second row rather
than updating the first.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import scan_store
from app.database import Base
from app.models import SupplierScanResult


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[SupplierScanResult.__table__])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


S1 = [
    {"ean": "111", "asin": "B01", "title": "Matched but screened out",
     "sales_rank": 500, "est_sell_price_pence": 1200},
    {"ean": "222", "asin": None, "title": None},          # no Amazon match at all
    {"ean": "333", "asin": "B03", "title": "Went on to stage 2"},
]
S2 = [
    {"ean": "333", "asin": "B03", "brand": "B", "name": "n", "title": "Went on to stage 2",
     "buy_price_pence": 500, "units_per_sale": 1, "bundle_cost_pence": 500,
     "sell_price_pence": 2000, "net_profit_pence": 700, "roi": 1.4,
     "sales_rank": 900, "fba_offer_count": 2, "verdict": "PASS",
     "verdict_reason": None, "flags": [], "note": "x"},
]


def test_stores_every_stage1_row_including_unmatched(db):
    scan_store.persist_scan(db, "acme", S1, S2)
    rows = {r.ean: r for r in db.query(SupplierScanResult).all()}
    assert set(rows) == {"111", "222", "333"}
    # "never seen on Amazon" is a fact worth keeping, not a row to drop.
    assert rows["222"].asin is None and rows["222"].stage == 1
    assert rows["111"].stage == 1 and rows["111"].sell_price_pence == 1200


def test_stage2_record_wins_over_stage1_for_same_ean(db):
    scan_store.persist_scan(db, "acme", S1, S2)
    r = db.query(SupplierScanResult).filter_by(ean="333").one()
    assert r.stage == 2
    assert r.verdict == "PASS" and r.net_profit_pence == 700


def test_rescan_updates_in_place_and_keeps_first_seen(db):
    scan_store.persist_scan(db, "acme", S1, S2)
    before = db.query(SupplierScanResult).filter_by(ean="333").one().first_seen

    cheaper = [dict(S2[0], buy_price_pence=300, net_profit_pence=900, verdict="PASS")]
    scan_store.persist_scan(db, "acme", S1, cheaper)

    assert db.query(SupplierScanResult).count() == 3        # not 6
    r = db.query(SupplierScanResult).filter_by(ean="333").one()
    assert r.buy_price_pence == 300 and r.net_profit_pence == 900
    assert r.first_seen == before                            # history preserved


def test_same_ean_from_two_suppliers_is_two_rows(db):
    scan_store.persist_scan(db, "acme", S1, S2)
    scan_store.persist_scan(db, "other", S1, S2)
    assert db.query(SupplierScanResult).filter_by(ean="333").count() == 2


def test_long_verdict_reason_is_truncated_not_rejected(db):
    long = [dict(S2[0], verdict_reason="x" * 900)]
    scan_store.persist_scan(db, "acme", [], long)
    assert len(db.query(SupplierScanResult).one().verdict_reason) == 500


def test_stores_velocity_figure_and_its_source(db):
    """The number behind velocity_floor, not just the verdict. Source is
    stored with it because keepa_confirmed and rank_drop_proxy are trusted
    very differently by the gate."""
    rec = [dict(S2[0], est_monthly_sales=250.0, est_monthly_sales_source="keepa_confirmed")]
    scan_store.persist_scan(db, "acme", [], rec)
    r = db.query(SupplierScanResult).one()
    assert r.est_monthly_sales == 250.0
    assert r.est_monthly_sales_source == "keepa_confirmed"


def test_missing_velocity_is_null_not_zero(db):
    """No velocity data and 'sells nothing' are different facts — storing an
    absent figure as 0 would make unknowns look like confirmed duds."""
    scan_store.persist_scan(db, "acme", [], S2)
    r = db.query(SupplierScanResult).one()
    assert r.est_monthly_sales is None
    assert r.est_monthly_sales_source is None
