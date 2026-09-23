"""gating reason_code + approval_url -- distinguish APPROVAL_REQUIRED
(invoice and you're in) from NOT_ELIGIBLE (no path), see
app/spapi_client.py's _parse_gating

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-06

"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("spapi_gating_cache", sa.Column("reason_code", sa.String(), nullable=True))
    op.add_column("spapi_gating_cache", sa.Column("approval_url", sa.String(), nullable=True))
    # Every existing row predates reason-code capture, so its reason_code is
    # unknown rather than "ungated". Expire them so the next lookup refetches
    # instead of serving a bool with no reason attached for up to 7 days.
    op.execute("UPDATE spapi_gating_cache SET fetched_at = '1970-01-01 00:00:00+00'")


def downgrade() -> None:
    op.drop_column("spapi_gating_cache", "approval_url")
    op.drop_column("spapi_gating_cache", "reason_code")
