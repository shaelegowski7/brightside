"""title_search_cache table for the model-number/title Keepa search fallback

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-21

"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "title_search_cache",
        sa.Column("search_term", sa.String(), primary_key=True),
        sa.Column("asin", sa.String(), nullable=True),
        sa.Column("searched_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("title_search_cache")
