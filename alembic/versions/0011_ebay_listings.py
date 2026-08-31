"""ebay_listings -- per-SKU record of what the eBay lister actually did,
see app/ebay_lister.py's module docstring

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-31

"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ebay_listings",
        sa.Column("sku", sa.String(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("isbn", sa.String(), nullable=True),
        sa.Column("condition", sa.String(), nullable=False),
        sa.Column("price_pence", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("category_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("offer_id", sa.String(), nullable=True),
        sa.Column("listing_id", sa.String(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_ebay_listings_isbn", "ebay_listings", ["isbn"])
    # status drives the resume path -- a re-run filters on it per SKU.
    op.create_index("ix_ebay_listings_status", "ebay_listings", ["status"])
    op.create_index("ix_ebay_listings_offer_id", "ebay_listings", ["offer_id"])
    op.create_index("ix_ebay_listings_listing_id", "ebay_listings", ["listing_id"])


def downgrade() -> None:
    op.drop_index("ix_ebay_listings_listing_id", table_name="ebay_listings")
    op.drop_index("ix_ebay_listings_offer_id", table_name="ebay_listings")
    op.drop_index("ix_ebay_listings_status", table_name="ebay_listings")
    op.drop_index("ix_ebay_listings_isbn", table_name="ebay_listings")
    op.drop_table("ebay_listings")
