"""Persist per-marketplace outcomes for each catalog scrape."""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "d93e4c2a71b9"
down_revision: Union[str, Sequence[str], None] = "c8a4015ec723"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("product_catalog", sa.Column("marketplace_statuses", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))


def downgrade() -> None:
    op.drop_column("product_catalog", "marketplace_statuses")
