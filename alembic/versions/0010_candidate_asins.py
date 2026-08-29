"""candidate_asins -- reverse candidate search results, see
app/candidate_finder.py's module docstring

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-29

"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "candidate_asins",
        sa.Column("asin", sa.String(), primary_key=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("buybox_price", sa.Integer(), nullable=False),
        sa.Column("target_buy_price", sa.Integer(), nullable=False),
        sa.Column("sales_rank", sa.Integer(), nullable=True),
        sa.Column("fba_offer_count", sa.Integer(), nullable=True),
        sa.Column("est_monthly_sales", sa.Float(), nullable=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("candidate_asins")
