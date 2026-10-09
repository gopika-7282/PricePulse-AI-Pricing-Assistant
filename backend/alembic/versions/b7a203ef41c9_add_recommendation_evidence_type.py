"""Persist the source classification for every price recommendation."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b7a203ef41c9"
down_revision: Union[str, Sequence[str], None] = "d93e4c2a71b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recommendations",
        sa.Column("evidence_type", sa.String(), nullable=False, server_default="NO_EVIDENCE"),
    )


def downgrade() -> None:
    op.drop_column("recommendations", "evidence_type")
