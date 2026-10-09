"""Merge the existing password-reset and commercial-quantity migration lines."""
from typing import Sequence, Union

revision: str = "b8c14f6a209d"
down_revision: Union[str, Sequence[str], None] = ("a7c9e2f14b30", "d2f4a91c7e63")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
