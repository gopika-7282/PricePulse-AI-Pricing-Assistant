"""Distinguish provisional catalog rows from edited products awaiting reanalysis."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e7f3b2a41c89"
down_revision: Union[str, Sequence[str], None] = "e6d128c9af41"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("retailer_products")}
    if "catalog_identity_staging" not in columns:
        op.add_column(
            "retailer_products",
            sa.Column("catalog_identity_staging", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("retailer_products")}
    if "catalog_identity_staging" in columns:
        op.drop_column("retailer_products", "catalog_identity_staging")
