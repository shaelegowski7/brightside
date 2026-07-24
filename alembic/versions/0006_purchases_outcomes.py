"""purchases, outcomes -- manual purchase/sale logging (spec phase 3), feeds
the realised-vs-predicted ROI review workflow

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-24

"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "purchases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("score_id", sa.Integer(), sa.ForeignKey("scores.id"), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("actual_buy_price", sa.Integer(), nullable=False),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_purchases_id", "purchases", ["id"])
    op.create_index("ix_purchases_score_id", "purchases", ["score_id"])

    op.create_table(
        "outcomes",
        sa.Column("purchase_id", sa.Integer(), sa.ForeignKey("purchases.id"), primary_key=True),
        sa.Column("sold_price", sa.Integer(), nullable=False),
        sa.Column("sold_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actual_fees", sa.Integer(), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("outcomes")
    op.drop_table("purchases")
