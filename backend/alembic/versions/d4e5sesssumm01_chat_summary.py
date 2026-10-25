"""会话滚动摘要的列：chat_sessions 加 summary + 水位 + 尾部指针

Revision ID: d4e5sesssumm01
Revises: c3f2sessfill01
Create Date: 2026-10-07

P3A 只做一件事：让"很久以前说过什么"不再靠把整段历史塞进 prompt 来记得。
读侧已经有字符预算（chat_history_char_budget=6000）从最新往回取整轮，
超出的部分今天的处理是**直接不看**。这组列给它一个替代品：
一段覆盖到某个 message 水位线的摘要。

各列的分工（为什么不是"一个 summary 字段"就够）：

- `summary`：摘要正文。只服务**当前这一个会话**，不是长期记忆 ——
  跨会话的稳定事实属于 P3B 的 user_memories，把自由文本 summary 当记忆库
  是这两套机制最容易被混起来的地方。
- `summary_through_message_id`：水位线。语义是"id <= 它的所有消息都已经被这段
  summary 覆盖"。它是唯一的正确性锚点：读侧按它决定从哪里接上原文，
  写侧只有摘要**成功落库**才允许推进它。宁可摘要重复一段，也不能让水位线
  跑到还没被覆盖的消息前面 —— 那等于把中间那段永久丢掉。
- `last_message_id` / `last_at`：会话尾部指针，新消息写入时同事务更新。
  调度判据 `last_message_id > COALESCE(summary_through_message_id, 0)` 靠它，
  否则每轮都要对 chat_messages 做一次 EXISTS/聚合才能知道"有没有新东西"。
- `summary_revision`：这个会话成功摘要过几次（每次 +1，用于观察收敛与抖动）。
- `summary_schema_version`：摘要**格式/提示词**的版本。和上面那个必须拆开：
  改 prompt 时靠 revision 无法表达"旧摘要作废、按新格式重做"，
  而混成一列的话，一次 prompt 调整就会把全部会话的计数灌成噪音。

不做数据回填（和 c3f2sessfill01 相反，那是有意的）：
`last_message_id` 一律留 NULL，靠新消息写出来。理由有两条 ——
①回填就等于让调度器在同一轮里把 800 多个历史会话全判成"该摘要"，
而它们当中绝大多数再也不会有人读；②本库那些历史消息基本是评测残留
（1721 条里 1417 条属于 zhangsan_demo，就是 eval runner 的 DEMO_USER），
给它们花真金白银的 token 做摘要是纯浪费。一个会话等到**真的有人回来聊**
才有摘要的价值，那时尾部指针自然会立起来。

不加快照索引：会话量级下调度扫描是几百行的顺序扫，而 chat_messages 每个会话
平均 2.1 条。等 EXPLAIN 说它慢再加索引，不要凭想象加。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5sesssumm01"
down_revision: Union[str, Sequence[str], None] = "c3f2sessfill01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column(
        "chat_sessions", sa.Column("summary_through_message_id", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "chat_sessions", sa.Column("last_message_id", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "chat_sessions",
        sa.Column("last_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "chat_sessions",
        sa.Column(
            "summary_revision", sa.Integer(), server_default="0", nullable=False
        ),
    )
    op.add_column(
        "chat_sessions",
        sa.Column(
            "summary_schema_version", sa.Integer(), server_default="1", nullable=False
        ),
    )


def downgrade() -> None:
    # 顺序反过来删，且这是本阶段唯一的回滚路径：产品代码在 summary 列不存在时
    # 不该被部署（读侧 SELECT * 会炸），所以回滚必须连着回滚应用版本。
    op.drop_column("chat_sessions", "summary_schema_version")
    op.drop_column("chat_sessions", "summary_revision")
    op.drop_column("chat_sessions", "last_at")
    op.drop_column("chat_sessions", "last_message_id")
    op.drop_column("chat_sessions", "summary_through_message_id")
    op.drop_column("chat_sessions", "summary")
