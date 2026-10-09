"""Keep retailer-specific editable product details on each retailer entry."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c4f7e15ad902"
down_revision: Union[str, Sequence[str], None] = "b7a203ef41c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("retailer_products")}
    for name, column_type in (
        ("name_override", sa.String()),
        ("category_override", sa.String()),
        ("brand_override", sa.String()),
        ("product_details_override", sa.Text()),
    ):
        if name not in existing:
            op.add_column("retailer_products", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("retailer_products")}
    for name in ("product_details_override", "brand_override", "category_override", "name_override"):
        if name in existing:
            op.drop_column("retailer_products", name)
