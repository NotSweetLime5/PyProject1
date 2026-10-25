"""摘要检查进度与用户长期偏好。

Revision ID: f6a7memory01
Revises: e5f6sessidx01
"""
from alembic import op
import sqlalchemy as sa

revision = "f6a7memory01"
down_revision = "e5f6sessidx01"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chat_sessions", sa.Column("summary_checked_through_message_id", sa.BigInteger()))
    op.add_column("chat_sessions", sa.Column("summary_checked_at", sa.DateTime(timezone=True)))
    op.create_index("ix_chat_summary_check", "chat_sessions", ["summary_checked_at", "id"],
                    postgresql_where=sa.text("summary_eligible AND deleted_at IS NULL"))
    op.create_index("ix_chat_message_session_id", "chat_messages", ["session_id", "id"],
                    postgresql_where=sa.text("deleted_at IS NULL"))
    op.create_table(
        "user_memories",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.String(32), nullable=False),
        sa.Column("value", sa.String(120), nullable=False),
        sa.Column("source_session_id", sa.String(64)),
        sa.Column("source_message_id", sa.BigInteger()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("user_id", "key", name="uq_user_memory_key"),
        sa.CheckConstraint("key IN ('brand_preference', 'usual_use', 'answer_style')", name="ck_memory_key"),
    )
    op.create_index("ix_user_memories_user_id", "user_memories", ["user_id"])


def downgrade():
    op.drop_table("user_memories")
    op.drop_index("ix_chat_message_session_id", table_name="chat_messages")
    op.drop_index("ix_chat_summary_check", table_name="chat_sessions")
    op.drop_column("chat_sessions", "summary_checked_at")
    op.drop_column("chat_sessions", "summary_checked_through_message_id")
