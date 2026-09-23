"""supplier_scan_results: store the velocity figure, not just its verdict

velocity_floor is the largest single reject reason across every scan run so
far (1,159 of 2,578 scored products). The number behind it existed only as
prose inside verdict_reason, so "which of these actually sell" meant
re-querying Keepa for data already paid for.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-09

"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("supplier_scan_results",
                  sa.Column("est_monthly_sales", sa.Float(), nullable=True))
    op.add_column("supplier_scan_results",
                  sa.Column("est_monthly_sales_source", sa.String(), nullable=True))
    # "Everything above the velocity floor, any supplier" is the query this
    # table now exists to answer.
    op.create_index("ix_ssr_monthly_sales", "supplier_scan_results", ["est_monthly_sales"])


def downgrade() -> None:
    op.drop_index("ix_ssr_monthly_sales", table_name="supplier_scan_results")
    op.drop_column("supplier_scan_results", "est_monthly_sales_source")
    op.drop_column("supplier_scan_results", "est_monthly_sales")
