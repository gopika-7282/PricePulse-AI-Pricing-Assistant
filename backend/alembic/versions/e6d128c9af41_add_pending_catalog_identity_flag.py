"""Track products whose staging catalog row still needs LangGraph resolution."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e6d128c9af41"
down_revision: Union[str, Sequence[str], None] = "e5e7a52c1120"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("retailer_products")}
    if "catalog_identity_pending" not in columns:
        op.add_column(
            "retailer_products",
            sa.Column("catalog_identity_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("retailer_products")}
    if "catalog_identity_pending" in columns:
        op.drop_column("retailer_products", "catalog_identity_pending")
