"""category_size table -- cached Keepa category productCount for the velocity gate's rank-percentile leg

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-23

"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "category_size",
        sa.Column("cat_id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("product_count", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("category_size")
