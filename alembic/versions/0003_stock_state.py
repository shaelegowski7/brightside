"""stock_state table for restock/new-release monitors

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-21

"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stock_state",
        sa.Column("retailer", sa.String(), primary_key=True),
        sa.Column("url_hash", sa.String(), primary_key=True),
        sa.Column("in_stock", sa.Boolean(), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("stock_state")
