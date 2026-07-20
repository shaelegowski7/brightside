"""crawl_state table for retailer scraper diffing

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-20

"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crawl_state",
        sa.Column("retailer", sa.String(), primary_key=True),
        sa.Column("url_hash", sa.String(), primary_key=True),
        sa.Column("last_price", sa.Integer(), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("crawl_state")
