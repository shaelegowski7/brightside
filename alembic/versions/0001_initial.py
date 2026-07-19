"""initial schema: products, deals, scores, pings, token_log

Revision ID: 0001
Revises:
Create Date: 2026-07-19

"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ean", sa.String(), nullable=True),
        sa.Column("asin", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("matched_via", sa.String(), nullable=False),
        sa.Column("confidence", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_products_id", "products", ["id"])
    op.create_index("ix_products_ean", "products", ["ean"], unique=True)
    op.create_index("ix_products_asin", "products", ["asin"], unique=True)

    op.create_table(
        "deals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("retailer", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("image_url", sa.String(), nullable=True),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("retailer_url", sa.String(), nullable=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=True),
        sa.Column("buy_price", sa.Integer(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="new"),
    )
    op.create_index("ix_deals_id", "deals", ["id"])
    op.create_index("ix_deals_source", "deals", ["source"])
    op.create_index("ix_deals_url", "deals", ["url"], unique=True)
    op.create_index("ix_deals_product_id", "deals", ["product_id"])
    op.create_index("ix_deals_status", "deals", ["status"])

    op.create_table(
        "scores",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("deal_id", sa.Integer(), sa.ForeignKey("deals.id"), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sell_price", sa.Integer(), nullable=True),
        sa.Column("fees_json", sa.JSON(), nullable=True),
        sa.Column("net_profit", sa.Integer(), nullable=True),
        sa.Column("roi", sa.Float(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("est_monthly_sales", sa.Float(), nullable=True),
        sa.Column("offer_count", sa.Integer(), nullable=True),
        sa.Column("amazon_on_listing", sa.Boolean(), nullable=True),
        sa.Column("gated", sa.Boolean(), nullable=True),
        sa.Column("flags_json", sa.JSON(), nullable=True),
        sa.Column("verdict", sa.String(), nullable=False),
        sa.Column("verdict_reason", sa.String(), nullable=True),
    )
    op.create_index("ix_scores_id", "scores", ["id"])
    op.create_index("ix_scores_deal_id", "scores", ["deal_id"])

    op.create_table(
        "pings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asin", sa.String(), nullable=False),
        sa.Column("deal_id", sa.Integer(), sa.ForeignKey("deals.id"), nullable=False),
        sa.Column("score_id", sa.Integer(), sa.ForeignKey("scores.id"), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_pings_id", "pings", ["id"])
    op.create_index("ix_pings_asin", "pings", ["asin"])

    op.create_table(
        "token_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("tokens_before", sa.Integer(), nullable=True),
        sa.Column("tokens_after", sa.Integer(), nullable=True),
        sa.Column("tokens_consumed", sa.Integer(), nullable=True),
        sa.Column("note", sa.String(), nullable=True),
    )
    op.create_index("ix_token_log_id", "token_log", ["id"])


def downgrade() -> None:
    op.drop_table("token_log")
    op.drop_table("pings")
    op.drop_table("scores")
    op.drop_table("deals")
    op.drop_table("products")
