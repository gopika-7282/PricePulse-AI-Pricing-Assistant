"""Backfill historical NULL competitor counts before enforcing the contract."""
from alembic import op
from sqlalchemy import text

revision = "20261111_0002"
down_revision = "20261111_0001"
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    bind.execute(text("UPDATE price_analysis SET competitor_count = 0 WHERE competitor_count IS NULL"))
    op.alter_column("price_analysis", "competitor_count", nullable=False)

def downgrade():
    # Relaxing nullability is data-safe; no values are deleted or rewritten.
    op.alter_column("price_analysis", "competitor_count", nullable=True)
