"""独立、用户隔离的记忆教训库。

Revision ID: h8c9guard01
Revises: g7b8model01
"""
from alembic import op
import sqlalchemy as sa

revision = "h8c9guard01"
down_revision = "g7b8model01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "memory_lessons",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rule_code", sa.String(40), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="candidate"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_session_id", sa.String(64), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("user_id", "rule_code", name="uq_memory_lesson_user_rule"),
        sa.CheckConstraint("rule_code IN ('preference_instruction', 'policy_evidence_gap')", name="ck_lesson_rule"),
        sa.CheckConstraint("status IN ('candidate', 'active', 'dismissed')", name="ck_lesson_status"),
        sa.CheckConstraint("revision >= 1 AND occurrences >= 1", name="ck_lesson_counters"),
    )
    op.create_index("ix_memory_lessons_user_id", "memory_lessons", ["user_id"])


def downgrade():
    op.drop_table("memory_lessons")
