"""会话归属表：chat_sessions

Revision ID: b2d1sessown01
Revises: a1c0repidx01
Create Date: 2026-10-07

为什么是一张表而不是继续信任客户端自造的 session_id：
现在任何人换一个 sid 就能读到别人的对话历史 —— chat_messages.session_id 是唯一的
取数键，而它由前端生成、服务端照单全收。归属必须存在服务端，
所以 session_id 改由服务端签发，并把「谁能读这个会话」落在这张表上。

guest_secret_hash 存摘要不存原文：明文只在签发时通过 SSE 的 session 帧给一次，
之后每次请求带过来比对。万一库被拖走，拿到摘要也还原不出可用凭据。

手写而非 autogenerate：后者会顺带给 a1c0repidx01 里那 5 个 PG 专有索引
（GIN tsvector / pg_trgm / HNSW / 2 个 partial）生成 drop_index，env.py 没有
include_object 保护，跑一次就把检索索引删了。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2d1sessown01"
down_revision: Union[str, Sequence[str], None] = "a1c0repidx01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_sessions",
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("guest_secret_hash", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=16), nullable=False),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_chat_sessions_session_id"),
        "chat_sessions",
        ["session_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_chat_sessions_user_id"), "chat_sessions", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_chat_sessions_user_id"), table_name="chat_sessions")
    op.drop_index(op.f("ix_chat_sessions_session_id"), table_name="chat_sessions")
    op.drop_table("chat_sessions")
