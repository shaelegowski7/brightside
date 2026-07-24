"""spapi_fee_cache, spapi_gating_cache -- dormant until spapi_client.
is_configured(), see app/spapi_client.py's module docstring

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-24

"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "spapi_fee_cache",
        sa.Column("asin", sa.String(), primary_key=True),
        sa.Column("price_band_pence", sa.Integer(), primary_key=True),
        sa.Column("referral_fee_pence", sa.Integer(), nullable=False),
        sa.Column("fba_fulfilment_fee_pence", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "spapi_gating_cache",
        sa.Column("asin", sa.String(), primary_key=True),
        sa.Column("gated", sa.Boolean(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("spapi_gating_cache")
    op.drop_table("spapi_fee_cache")
