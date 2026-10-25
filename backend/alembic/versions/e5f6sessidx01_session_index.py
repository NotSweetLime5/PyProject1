"""会话索引补全 + 把"要不要摘要"从"索引全不全"里拆出来

Revision ID: e5f6sessidx01
Revises: d4e5sesssumm01
Create Date: 2026-10-08

d4e5sesssumm01 刻意不回填尾部指针，代价是 `list_sessions` 那个 `limit*6` 分组前截断的
启发式一直删不掉（会话列表一旦改读 chat_sessions.last_at，没回填的历史就全塌成"没有最近活动"）。
这一条把索引补全。**但不能补完指针就顺手让调度器去摘要 800 多个死会话** ——
所以这里同时加一个独立的资格位。

## 为什么加一列，而不是用别的手段

调度的候选判据是 `last_message_id > COALESCE(水位, 0)`。要让老会话回填后不进候选，
能走的路只有四条，前三种都是假的：

1. **不回填** → 就是上一版的回避，会话索引永远不完整，`list_sessions` 的缺陷一直遗留。
2. **回填 last_at 成一个很早的时间** → 把"资格"编码进一个业务时间字段里。
   排序、UI、以后任何按 last_at 做的判断都会读到一个人造的旧时间；而且它压根挡不住调度
   （判据不看 last_at），要挡还得再改判据 —— 绕一圈还是加字段，只是把语义弄脏了。
3. **给老会话伪造一个水位线**（比如指向它第一条消息） → **绝对不行**：水位线的语义是
   "这段已经被摘要覆盖了"，伪造它等于宣布一段从没被摘要过的历史可以不再被读，
   而读侧真的会按它停住（`load_history` 用 `id > 水位` 过滤）。那是真的把旧历史从上下文里删了。
4. **独立的资格位**（本条）→ 索引归索引，摘要资格归摘要资格，两件事各自表达。
   资格由**在线写入**授予：`save_message` 更新尾部指针时一并置 true；
   调度判据多一个 `AND summary_eligible`。回填出来的指针不带资格，
   于是一个死会话无论多长都不会被摘要，而它只要再收到一条消息就立刻回到原有流程里。

## 幂等与"不许覆盖在线写入"

回填只填 `last_message_id IS NULL` 的行。这半个条件同时给了这两件事：
重跑第二次匹配 0 行；而 Alembic 跑过之后任何新写入都已经有值，
不会被这条 UPDATE 拿历史最大值退回去。

时间取 `max(created_at)`，不是 `now()` —— last_at 的语义是"这个会话最后说话的时刻"，
用回填时刻冒充就会把所有老会话挤到列表最前，而且撒得很整齐，看不出是假的。

## 不破坏在途状态

已经在摘要循环里的会话（有水位 = 至少成功摘要过一次）回填后必须**保持**有资格，
否则"升级一次，正在重试的摘要全停了"这种状态丢失最难查。
所以 ALTER 之后紧跟一条 carve-out：`summary_through_message_id IS NOT NULL → eligible = true`。
本库现在匹配 0 行（0 个会话有摘要），但真实部署上这一条是必须的。

## 只填了 1253 行里的 1253 个"有消息的"会话行

孤儿 sid（chat_messages 里有、chat_sessions 里没有行的）在这里是 115 个，
且**它们一条有主消息都没有**（实测 0 条）—— 全是当年没存凭据的游客会话。
本迁移不给它们造行：造出来的行谁都不能读，而且 P1 定过没有弱兼容。
它们也就不会进会话列表（列表按 chat_sessions.user_id 过滤）。

## downgrade

只删 `summary_eligible` 这一列，**故意不回滚尾部指针**：那两个值是既有消息的纯派生
（max(id) / max(created_at)），留着不会误导任何东西，而下一次写入本来就会重算它们。
反过来"精确回滚只删回填的那批"需要一个"这行是回填来的"的标记位 ——
为了一次回滚再往会话表上贴一个标记位，代价大于收益。

回滚会丢掉资格位（它是写入历史的派生，不是消息内容的一部分），
所以 upgrade → downgrade → upgrade 之后：
**有水位线的会话由 KEEP_IN_LOOP 自动回到循环里**（正在滚动摘要的会话不会被这次回滚卡死），
其余会话要等下一次真实写入重新获得资格。这个代价可以接受，因为回滚本来就要连着回滚应用版本
—— 旧版本代码里没有这一列，也没有读它的调度器。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6sessidx01"
down_revision: Union[str, Sequence[str], None] = "d4e5sesssumm01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 已经在摘要循环里的会话保持资格（升级不能顺手停掉在重试的摘要）
KEEP_IN_LOOP = """
UPDATE chat_sessions
   SET summary_eligible = true
 WHERE summary_through_message_id IS NOT NULL
"""

# 只填没有指针的行：这半个条件同时给了幂等和"不覆盖在线写入"
FILL_POINTERS = """
UPDATE chat_sessions cs
   SET last_message_id = m.max_id,
       last_at         = m.max_at
  FROM (
        SELECT session_id, max(id) AS max_id, max(created_at) AS max_at
          FROM chat_messages
         WHERE deleted_at IS NULL
         GROUP BY session_id
       ) m
 WHERE m.session_id = cs.session_id
   AND cs.last_message_id IS NULL
"""


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("summary_eligible", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    # 两边都默认 false，资格**只由一次真实消息写入授予**（save_message 里和尾部指针一起置上）。
    # 这样"新会话正常进摘要流程"和"回填的老会话不进"用的是同一条规则，
    # 而不是靠"新会话默认 true、老会话默认 false"那种只在 ALTER 那一刻成立的分裂默认值。
    # （constant default 的 ADD COLUMN 在 PG 11+ 不重写整表，1369 行毫秒级。）
    op.execute(KEEP_IN_LOOP)
    op.execute(FILL_POINTERS)


def downgrade() -> None:
    op.drop_column("chat_sessions", "summary_eligible")
