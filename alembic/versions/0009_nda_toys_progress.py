"""nda_toys_crawl_progress -- resume cursor for NdaToysAdapter, see
app/models.py's NdaToysCrawlProgress docstring

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-28

"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nda_toys_crawl_progress",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("completed_through_index", sa.Integer(), nullable=False, server_default="-1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("nda_toys_crawl_progress")
