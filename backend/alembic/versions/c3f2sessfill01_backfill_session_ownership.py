"""回填会话归属：给 P1 之前已有的聊天记录补 chat_sessions 行

Revision ID: c3f2sessfill01
Revises: b2d1sessown01
Create Date: 2026-10-07

为什么必须有这条：b2d1sessown01 只建表，不动数据。而归属校验强制开启后，
读会话的唯一依据就是 chat_sessions —— 老消息没有对应行，一律 unknown_sid → 410。
对一个刚建的项目无所谓，对任何已经跑过一段时间、真有人聊过的部署，
那一刀就把所有历史会话全判成"已失效"。补这一条，翻开关的成本就不含数据丢失。

归属规则（写成 SQL 而不是 Python，因为它只看两个列，而且必须和消息同一个事务量级）：
  - 只补 user_id 非空的消息。游客的老会话补不了 —— 当年根本没存凭据，
    现在谁也证明不了那是谁的，硬造一行只会制造一个谁都不能读还占位的记录。
  - 一个 session_id 一行，归属取**最早那条有主消息**的作者：谁开的会话算谁的。
    DISTINCT ON (session_id) ... ORDER BY id 就是这个意思。
  - created_by 写 'backfill' 而不是 'user'：这一列记的是"当初谁建的会话"，
    回填时并不知道，标成已知值就是造假。判据路径不读这一列（_verify 只看
    user_id 和 guest_secret_hash），所以多个取值不会改变任何行为。
  - guest_secret_hash 留 NULL：归属已经在 user_id 上，不需要也不该有凭据。

ON CONFLICT DO NOTHING 让它可重复执行：中途失败重跑不会撞唯一索引。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "c3f2sessfill01"
down_revision: Union[str, Sequence[str], None] = "b2d1sessown01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


BACKFILL = """
INSERT INTO chat_sessions (session_id, user_id, guest_secret_hash, created_by)
SELECT DISTINCT ON (session_id) session_id, user_id, NULL, 'backfill'
FROM chat_messages
WHERE user_id IS NOT NULL AND deleted_at IS NULL
ORDER BY session_id, id ASC
ON CONFLICT (session_id) DO NOTHING
"""


def upgrade() -> None:
    op.execute(BACKFILL)


def downgrade() -> None:
    # 只撤这一条造出来的行。按 sid 反查会误删签发出来的真会话，所以只认 created_by。
    op.execute("DELETE FROM chat_sessions WHERE created_by = 'backfill'")
