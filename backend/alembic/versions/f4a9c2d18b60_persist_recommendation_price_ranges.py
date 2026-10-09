"""Persist backend-calculated recommendation price ranges."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f4a9c2d18b60"
down_revision: Union[str, Sequence[str], None] = "e7f3b2a41c89"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("recommendations")}
    for name in ("recommended_price_min", "recommended_price_max"):
        if name not in existing:
            op.add_column("recommendations", sa.Column(name, sa.Float(), nullable=True))


def downgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("recommendations")}
    for name in ("recommended_price_max", "recommended_price_min"):
        if name in existing:
            op.drop_column("recommendations", name)
