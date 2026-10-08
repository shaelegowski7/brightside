"""keepa_cache + keepa_code_map: keep every Keepa lookup already paid for

Rule changes (30% -> 10% ROI, £3 -> 80p, hazmat) kept forcing full rescans
because only verdicts were stored, never the Keepa data behind them.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-08

"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "keepa_cache",
        sa.Column("asin", sa.String(), primary_key=True),
        sa.Column("stage1", sa.JSON(), nullable=True),
        sa.Column("stage1_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stage2", sa.JSON(), nullable=True),
        sa.Column("stage2_fetched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "keepa_code_map",
        sa.Column("code", sa.String(), primary_key=True),
        sa.Column("asin", sa.String(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("keepa_code_map")
    op.drop_table("keepa_cache")
