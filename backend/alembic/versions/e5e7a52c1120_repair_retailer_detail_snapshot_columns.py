"""Repair retailer-specific detail columns if the earlier migration was recorded incompletely."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e5e7a52c1120"
down_revision: Union[str, Sequence[str], None] = "c4f7e15ad902"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "retailer_products" not in inspector.get_table_names():
        raise RuntimeError("retailer_products table is missing; refusing to apply snapshot-column repair")
    existing = {column["name"] for column in inspector.get_columns("retailer_products")}
    columns = (
        ("name_override", sa.String()),
        ("category_override", sa.String()),
        ("brand_override", sa.String()),
        ("product_details_override", sa.Text()),
    )
    for name, column_type in columns:
        if name not in existing:
            op.add_column("retailer_products", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    # These columns are part of the schema established by c4f7e15ad902.
    # The repair migration only reconciles a partial application, so downgrade
    # returns to that schema rather than dropping data-bearing columns.
    pass
