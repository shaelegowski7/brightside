"""supplier_scan_results -- make catalogue scans queryable

Six suppliers' scans (15,416 stage-1 lookups, 2,578 full scorings) existed
only as untracked .jsonl files in the repo root. See app/models.py's
SupplierScanResult docstring.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-09

"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "supplier_scan_results",
        sa.Column("supplier", sa.String(), nullable=False),
        sa.Column("ean", sa.String(), nullable=False),
        sa.Column("asin", sa.String(), nullable=True),
        sa.Column("brand", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("stage", sa.Integer(), nullable=False),
        sa.Column("buy_price_pence", sa.Integer(), nullable=True),
        sa.Column("units_per_sale", sa.Integer(), nullable=True),
        sa.Column("bundle_cost_pence", sa.Integer(), nullable=True),
        sa.Column("sell_price_pence", sa.Integer(), nullable=True),
        sa.Column("net_profit_pence", sa.Integer(), nullable=True),
        sa.Column("roi", sa.Float(), nullable=True),
        sa.Column("sales_rank", sa.Integer(), nullable=True),
        sa.Column("fba_offer_count", sa.Integer(), nullable=True),
        sa.Column("verdict", sa.String(), nullable=True),
        sa.Column("verdict_reason", sa.String(), nullable=True),
        sa.Column("flags", sa.JSON(), nullable=True),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("supplier", "ean"),
    )
    op.create_index("ix_supplier_scan_results_asin", "supplier_scan_results", ["asin"])
    # The two questions actually asked of this table: "what passed / came
    # close, anywhere" and "show me this supplier's results".
    op.create_index("ix_ssr_verdict", "supplier_scan_results", ["verdict"])
    op.create_index("ix_ssr_supplier_stage", "supplier_scan_results", ["supplier", "stage"])


def downgrade() -> None:
    op.drop_index("ix_ssr_supplier_stage", table_name="supplier_scan_results")
    op.drop_index("ix_ssr_verdict", table_name="supplier_scan_results")
    op.drop_index("ix_supplier_scan_results_asin", table_name="supplier_scan_results")
    op.drop_table("supplier_scan_results")
