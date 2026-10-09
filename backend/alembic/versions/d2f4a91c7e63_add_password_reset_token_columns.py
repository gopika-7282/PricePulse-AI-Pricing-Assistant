"""Ensure users have fields for hashed, expiring password reset tokens.

Revision ID: d2f4a91c7e63
Revises: a38c7d92f104
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d2f4a91c7e63"
down_revision: Union[str, Sequence[str], None] = "a38c7d92f104"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "reset_token" not in columns:
        op.add_column("users", sa.Column("reset_token", sa.String(length=255), nullable=True))
    if "reset_token_expiry" not in columns:
        op.add_column("users", sa.Column("reset_token_expiry", sa.DateTime(timezone=True), nullable=True))
    indexes = {index["name"] for index in inspector.get_indexes("users")}
    if "ix_users_reset_token" not in indexes:
        op.create_index("ix_users_reset_token", "users", ["reset_token"], unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("users")}
    if "ix_users_reset_token" in indexes:
        op.drop_index("ix_users_reset_token", table_name="users")
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "reset_token_expiry" in columns:
        op.drop_column("users", "reset_token_expiry")
    if "reset_token" in columns:
        op.drop_column("users", "reset_token")
