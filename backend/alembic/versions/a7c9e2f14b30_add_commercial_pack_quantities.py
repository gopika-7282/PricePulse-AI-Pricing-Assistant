"""Add commercial pack quantities separately from inventory stock."""
from alembic import op
import sqlalchemy as sa

revision = "a7c9e2f14b30"
down_revision = "f4a9c2d18b60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, columns in {
        "retailer_products": [("quantity_value", sa.Float()), ("quantity_unit", sa.String(16))],
        "competitor_products": [("quantity_value", sa.Float()), ("quantity_unit", sa.String(16)),
                                ("pack_count", sa.Integer()), ("total_quantity", sa.Float()),
                                ("total_quantity_unit", sa.String(16))],
    }.items():
        existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}
        for name, type_ in columns:
            if name not in existing:
                op.add_column(table, sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for table, names in {"competitor_products": ["total_quantity_unit", "total_quantity", "pack_count", "quantity_unit", "quantity_value"],
                         "retailer_products": ["quantity_unit", "quantity_value"]}.items():
        existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}
        for name in names:
            if name in existing:
                op.drop_column(table, name)
