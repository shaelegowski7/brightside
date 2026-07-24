"""CategorySize.cat_id round-trips a value exceeding Postgres's 4-byte
INTEGER range (confirmed live 2026-07-24: real Keepa leaf category IDs like
30117754031 hit NumericValueOutOfRange when the column was still Integer).

Caveat: SQLite (the test DB) doesn't enforce column-width limits the way
Postgres does, so this test can't actually fail if the model regresses back
to sa.Integer -- that specific bug is Postgres-only. It's still worth
having as a value-level regression check and as documentation of why the
column is BigInteger."""
from app import models


def test_category_size_stores_id_exceeding_int32_range(db_session):
    big_cat_id = 30117754031   # ~14x int32's max (2,147,483,647) -- a real Keepa catId
    db_session.add(models.CategorySize(cat_id=big_cat_id, name="2 in 1 Laptops", product_count=7110))
    db_session.commit()

    row = db_session.get(models.CategorySize, big_cat_id)
    assert row.cat_id == big_cat_id
    assert row.product_count == 7110
