"""Add tenant-scoped persistent chat conversations and messages."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a38c7d92f104"
down_revision: Union[str, Sequence[str], None] = "f4a9c2d18b60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_conversations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("retailer_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("retailer_products.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_chat_conversations_retailer_id", "chat_conversations", ["retailer_id"])
    op.create_index("ix_chat_conversations_product_id", "chat_conversations", ["product_id"])
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_chat_messages_conversation_id", "chat_messages", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_messages_conversation_id", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_chat_conversations_product_id", table_name="chat_conversations")
    op.drop_index("ix_chat_conversations_retailer_id", table_name="chat_conversations")
    op.drop_table("chat_conversations")
