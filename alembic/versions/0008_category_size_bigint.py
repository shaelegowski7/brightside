"""category_size.cat_id: Integer -> BigInteger -- some real Keepa leaf
category IDs exceed Postgres's 4-byte INTEGER range (confirmed live
2026-07-24, catId 30117754031)

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-25

"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table, not a plain op.alter_column: SQLite has no ALTER
    # COLUMN ... TYPE at all (it'd raise a syntax error) -- batch mode
    # recreates the table under the hood there, while emitting a direct
    # ALTER on Postgres (the real target). Keeps this migration runnable
    # against both, matching every earlier migration's sqlite-compatible
    # op.create_table style.
    with op.batch_alter_table("category_size") as batch_op:
        batch_op.alter_column(
            "cat_id", existing_type=sa.Integer(), type_=sa.BigInteger(), existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("category_size") as batch_op:
        batch_op.alter_column(
            "cat_id", existing_type=sa.BigInteger(), type_=sa.Integer(), existing_nullable=False,
        )
