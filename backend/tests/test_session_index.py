"""P3A 收尾测试：会话索引补全 + 摘要资格解耦 + list_sessions 分页。

这里测的三件事都属于"错了不会报错"的那类，所以必须有确定性用例：

- 回填把 1300 多个死会话的尾部指针立起来了。如果资格解耦没生效，调度器就会把它们
  全判成摘要候选 —— 症状是"某天 token 账单暴涨"，运行期一点异常都没有。
- 回填的值取错（比如 last_at 用了回填时刻）不会失败，只会让会话列表的顺序悄悄变脏，
  而且撒得很整齐，看不出是假的。
- list_sessions 的旧缺陷是"分组前截断"：平时数据小看不出来，一个长会话就能让别的会话
  整条消失。所以这里拿一个 90 条消息的会话正面证明旧写法会丢、新写法不丢。

回填的 SQL 不是重打一遍，而是**从迁移文件里 import 出来跑**：重打一份测试就会和实现漂移，
那时测试绿着而迁移是错的，比没有测试更糟。
"""

import importlib.util
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conftest import make_client, run
from sqlalchemy import delete, select, text

from app.core.database import AsyncSessionLocal, engine
from app.core.redis import redis_client
from app.models.custcare import ChatMessage, ChatSession
from app.models.user import User
from app.services import chat_summary as chat_summary_mod
from app.services.chat_service import list_sessions, load_history, save_message

_VERSION = (Path(__file__).resolve().parents[1]
            / "alembic" / "versions" / "e5f6sessidx01_session_index.py")
# 从迁移文件本身取 SQL：重打一份的话，测试和实现会各自漂移，
# 那时测试绿着而真正要上线的语句是错的 —— 比没有测试更糟。
# 这个 version 文件在模块级只定义常量和函数，exec_module 不会有副作用。
_spec = importlib.util.spec_from_file_location("mig_e5f6sessidx01", str(_VERSION))
_loader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_loader)
FILL_POINTERS = _loader.FILL_POINTERS
KEEP_IN_LOOP = _loader.KEEP_IN_LOOP

# 调度候选判据：新口径带资格位，旧口径不带。两个都留在测试里，是为了把
# "0 个候选"这件事归因到资格位上，而不是归因到某个凑巧挡住它的条件。
# 计数**只算本用例自己那几个 sid**：这个库是 dev/test 共用的，
# 按全局计数断言就会依赖别的测试留了什么数据，失败原因和被测逻辑无关。
CAND_NEW = """
select count(*) from chat_sessions
 where deleted_at is null and summary_eligible
   and last_message_id is not null
   and last_message_id > coalesce(summary_through_message_id, 0)
   and session_id = any(:ids)
"""
CAND_OLD = """
select count(*) from chat_sessions
 where deleted_at is null
   and last_message_id is not null
   and last_message_id > coalesce(summary_through_message_id, 0)
   and session_id = any(:ids)
"""

BASE_T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _phone() -> str:
    return "13" + str(uuid.uuid4().int % 10 ** 9).zfill(9)


async def _mk_user(name: str) -> int:
    async with AsyncSessionLocal() as db:
        u = User(username=f"{name}_{uuid.uuid4().hex[:6]}", password_hash="x",
                 phone=_phone(), is_active=True)
        db.add(u)
        await db.commit()
        await db.refresh(u)
        return u.id


async def _drop_users(*ids: int) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(User).where(User.id.in_(list(ids))))
        await db.commit()


async def _mk_session(sid: str, user_id=None, **cols) -> None:
    async with AsyncSessionLocal() as db:
        db.add(ChatSession(session_id=sid, user_id=user_id, guest_secret_hash=None,
                           created_by="user" if user_id else "guest", **cols))
        await db.commit()


async def _seed_msgs(sid: str, n: int, chars: int, *, user_id=None,
                     start: datetime | None = None) -> list[int]:
    """直插消息（不走 save_message）—— 模拟"迁移之前就已存在"的历史。"""
    ids = []
    async with AsyncSessionLocal() as db:
        for i in range(n):
            role = "user" if i % 2 == 0 else "assistant"
            ts = (start or BASE_T) + timedelta(seconds=i)
            m = ChatMessage(session_id=sid, user_id=user_id, role=role,
                            content="内" * chars, created_at=ts)
            db.add(m)
            await db.flush()
            ids.append(m.id)
        await db.commit()
    return ids


async def _sid_row(sid: str) -> dict:
    async with AsyncSessionLocal() as db:
        r = (await db.execute(
            select(ChatSession).where(ChatSession.session_id == sid)
        )).scalar_one()
        return {
            "last_message_id": r.last_message_id, "last_at": r.last_at,
            "eligible": r.summary_eligible, "summary": r.summary,
            "wm": r.summary_through_message_id, "revision": r.summary_revision,
            "user_id": r.user_id,
        }


async def _purge(*sids: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ChatMessage).where(ChatMessage.session_id.in_(list(sids))))
        await db.execute(delete(ChatSession).where(ChatSession.session_id.in_(list(sids))))
        await db.commit()


async def _count(sql: str, ids: list[str]) -> int:
    async with engine.connect() as c:
        return (await c.execute(text(sql), {"ids": ids})).scalar()


@contextmanager
def no_llm(text_out="假摘要"):
    """把模块级 summarizer 换掉：测试绝不能碰真 API，
    而"这一轮有没有真的调用过模型"本身就是要断言的东西。"""
    calls = []

    async def fake(prev, transcript):
        calls.append((prev, transcript))
        return text_out
    orig = chat_summary_mod._llm_summarize
    chat_summary_mod._llm_summarize = fake
    try:
        yield calls
    finally:
        chat_summary_mod._llm_summarize = orig


# ---------------- A. 回填本身 ----------------

def test_backfill_values_come_from_messages_and_are_idempotent():
    """last_message_id = max(id)、last_at = max(created_at)；重跑影响 0 行；不碰没消息的行。"""
    async def main():
        sid = uuid.uuid4().hex
        empty = uuid.uuid4().hex
        await _mk_session(sid, user_id=None)
        await _mk_session(empty, user_id=None)
        ids = await _seed_msgs(sid, 4, 30, start=BASE_T)
        async with engine.begin() as c:
            n1 = (await c.execute(text(FILL_POINTERS))).rowcount
        got = await _sid_row(sid)
        assert got["last_message_id"] == ids[-1], got
        # 关键：时间取的是消息自己的时间戳（2026-01-01），不是回填时刻
        assert got["last_at"].year == 2026 and got["last_at"].month == 1, got["last_at"]
        assert got["last_at"] == (BASE_T + timedelta(seconds=3)), got["last_at"]
        assert (await _sid_row(empty))["last_message_id"] is None, "没消息的会话不该被造出指针"
        async with engine.begin() as c:
            n2 = (await c.execute(text(FILL_POINTERS))).rowcount
        assert n2 == 0, n2
        await _purge(sid, empty)
    run(main())


def test_backfill_does_not_clobber_newer_online_write():
    """回填只填 NULL。已经由在线写入立好的指针（可能比历史 max(id) 更新）不能退回去。"""
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid, user_id=None)
        ids = await _seed_msgs(sid, 3, 20)
        newer = max(ids) + 10_000  # 模拟"回填之前又写了几条"的值
        async with engine.begin() as c:
            await c.execute(
                text("update chat_sessions set last_message_id = :v, last_at = now() "
                     "where session_id = :s"), {"v": newer, "s": sid}
            )
            await c.execute(text(FILL_POINTERS))
        assert (await _sid_row(sid))["last_message_id"] == newer
        await _purge(sid)
    run(main())


def test_backfill_never_touches_summary_or_watermark():
    """不回填 summary，也不伪造水位线：伪造水位等于宣布"这段已被摘要覆盖"，
    而读侧真的会按它停住 —— 那是把旧历史从上下文里真删掉。"""
    async def main():
        sid = uuid.uuid4().hex
        summary = "已经摘要过的内容"
        await _mk_session(sid, user_id=None)
        ids = await _seed_msgs(sid, 4, 25)
        async with engine.begin() as c:
            await c.execute(
                text("update chat_sessions set summary = :sm, summary_through_message_id = :wm, "
                     "summary_revision = 3 where session_id = :s"),
                {"sm": summary, "wm": ids[1], "s": sid},
            )
            await c.execute(text(FILL_POINTERS))
        got = await _sid_row(sid)
        assert got["summary"] == summary and got["wm"] == ids[1] and got["revision"] == 3, got
        assert got["last_message_id"] == ids[-1], got
        await _purge(sid)
    run(main())


def test_watermarked_sessions_keep_their_place_in_the_loop():
    """KEEP_IN_LOOP：已经有水位的会话回填后必须仍有资格。

    否则"升级一次，正在滚动摘要的会话全停了"——而且它看起来一切正常，
    只是那些会话再也不更新了。本库当前匹配 0 行，但真实部署上这条必须有。
    """
    async def main():
        with_wm, without = uuid.uuid4().hex, uuid.uuid4().hex
        await _mk_session(with_wm, user_id=None)
        await _mk_session(without, user_id=None)
        ids = await _seed_msgs(with_wm, 4, 20)
        await _seed_msgs(without, 4, 20)
        async with engine.begin() as c:
            await c.execute(
                text("update chat_sessions set summary_through_message_id = :wm where session_id = :s"),
                {"wm": ids[0], "s": with_wm},
            )
            await c.execute(text(KEEP_IN_LOOP))
            await c.execute(text(FILL_POINTERS))
        assert (await _sid_row(with_wm))["eligible"] is True
        assert (await _sid_row(without))["eligible"] is False
        await _purge(with_wm, without)
    run(main())


# ---------------- B. 索引完整 ≠ 允许摘要 ----------------

def test_backfilled_history_does_not_queue_summaries():
    """核心那条：把 12 个"远超读窗口"的历史会话回填完，调度器一个都不许摘要。

    种子必须**真的被挤出窗口**，否则那个 0 是字数造成的、不是资格位造成的：
    软预算允许最后一轮顶破，所以 6 行 / 7200 字会被整轮全收下（什么都不会折），
    我第一版就是这么把这条测试写成空绿的。8 行 / 9600 字才会留下一轮被挤出去。
    末尾另起一段"只放开资格位就必须真摘要出来"，把这件事正面钉死。
    同时断言"一次模型都没调"，因为真正的验收是账单，不是返回值。
    """
    async def main():
        sids = [uuid.uuid4().hex for _ in range(12)]
        for sid in sids:
            await _mk_session(sid, user_id=None)
            await _seed_msgs(sid, 8, 1200)     # 9600 字：见下面注释，6 行那种种子测不出东西
        async with engine.begin() as c:
            await c.execute(text(FILL_POINTERS))
        assert await _count(CAND_OLD, sids) == 12, "旧口径本应把这批全判成候选，否则这条测试没有区分度"
        assert await _count(CAND_NEW, sids) == 0, "资格位没生效"
        with no_llm() as calls:
            # 逐条走 summarize_session（资格位在这一层也拦）而不是只跑全局 job：
            # 这个库是 dev/test 共用的，只断言"我的这几个 sid"的结果才不受别人的残留影响。
            for sid in sids:
                assert await chat_summary_mod.summarize_session(sid) == 0, sid
            await chat_summary_mod.run_chat_summary()   # 调度路径也得跑一遍，别让它抛错
        assert calls == [], calls
        for sid in sids:
            assert (await _sid_row(sid))["summary"] is None, sid

        # 判别性证明：同一批数据只放开资格位，就必须真的摘要出来。
        # 少了这一步，"12 个都没摘要"完全可能只是种子不够长造成的假绿。
        with no_llm("放开资格后应当真摘要") as calls2:
            async with engine.begin() as c:
                await c.execute(
                    text("update chat_sessions set summary_eligible = true where session_id = :s"),
                    {"s": sids[0]},
                )
            assert await chat_summary_mod.summarize_session(sids[0]) == 1
        assert len(calls2) == 1, calls2
        assert (await _sid_row(sids[0]))["summary"] == "放开资格后应当真摘要"
        await _purge(*sids)
    run(main())


def test_reactivated_session_enters_the_summary_flow():
    """回填过的老会话只要再说一句话，就该立刻回到正常的摘要流程（资格由写入授予）。"""
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid, user_id=None)
        await _seed_msgs(sid, 8, 1200)   # 9600 字，确保真有一轮被挤出窗口（理由见上一条用例）
        async with engine.begin() as c:
            await c.execute(text(FILL_POINTERS))
        assert (await _sid_row(sid))["eligible"] is False
        with no_llm("重新活跃后的摘要") as calls:
            assert await chat_summary_mod.summarize_session(sid) == 0      # 还没资格
            async with AsyncSessionLocal() as db:                          # 用户回来说了一句
                await save_message(db, session_id=sid, user_id=None, role="user", content="接着上次的问题")
                await db.commit()
            assert (await _sid_row(sid))["eligible"] is True
            assert await _count(CAND_NEW, [sid]) == 1, "重新活跃后没进候选"
            assert await chat_summary_mod.summarize_session(sid) == 1
        assert len(calls) == 1, calls
        got = await _sid_row(sid)
        assert got["summary"] == "重新活跃后的摘要" and got["wm"] and got["revision"] == 1, got
        await _purge(sid)
    run(main())


def test_backfilled_history_is_still_readable_without_a_summary():
    """要求 2 最后一条：不许因为"没摘要"让旧历史从上下文里消失。

    回填之后水位线仍然是 NULL → 读路径的 `id > 水位` 过滤等于没有过滤，
    旧会话该看见多少还是看见多少（受字符预算限制，和 P2 完全一致）。
    """
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid, user_id=None)
        await _seed_msgs(sid, 5, 300)          # 1500 字，整段都在预算里
        async with engine.begin() as c:
            await c.execute(text(FILL_POINTERS))
        async with AsyncSessionLocal() as db:
            w = await load_history(db, sid, None, char_budget=6000, max_messages=128)
        assert (await _sid_row(sid))["wm"] is None
        assert w.count == 5 and w.chars == 1500, (w.count, w.chars)
        assert w.summary == "", w.summary
        assert w.stop_reason == "session_start", w.stop_reason
        await _purge(sid)
    run(main())


# ---------------- C. list_sessions ----------------

def _old_heuristic(spec_limit: int = 20):
    """旧写法的参照实现（分组前截断）。留着是为了正面量出它丢了多少会话。"""
    async def run_old(db, user_id: int) -> list[str]:
        rows = (await db.execute(
            select(ChatMessage.session_id)
            .where(ChatMessage.user_id == user_id, ChatMessage.deleted_at.is_(None))
            .order_by(ChatMessage.id.desc())
            .limit(spec_limit * 6)
        )).all()
        seen = []
        for (sid,) in rows:
            if sid not in seen:
                seen.append(sid)
        return seen[:spec_limit]
    return run_old


def test_list_sessions_no_longer_loses_sessions_behind_a_long_one():
    """25 个会话、最新那个有 90 条消息：旧写法吃满 120 行预算，把更早的会话整条挤掉。"""
    async def main():
        uid = await _mk_user("txi_ls")
        sids = [uuid.uuid4().hex for _ in range(25)]
        for i, sid in enumerate(sids):
            await _mk_session(sid, user_id=uid)
            n = 90 if i == len(sids) - 1 else 2
            await _seed_msgs(sid, n, 10, user_id=uid,
                             start=BASE_T + timedelta(minutes=i))
        # 把尾部指针立起来（等价于迁移回填），并让 last_at 严格按 i 递增
        async with engine.begin() as c:
            await c.execute(text(FILL_POINTERS))
            for i, sid in enumerate(sids):
                await c.execute(
                    text("update chat_sessions set last_at = :t where session_id = :s"),
                    {"t": BASE_T + timedelta(minutes=i), "s": sid},
                )
        async with AsyncSessionLocal() as db:
            old = await _old_heuristic()(db, uid)
            page1 = await list_sessions(db, uid, limit=20)
            page2 = await list_sessions(db, uid, limit=20, offset=20)
        ids1 = [r["session_id"] for r in page1]
        ids2 = [r["session_id"] for r in page2]
        assert len(old) < 25, f"旧写法本该丢会话，却没丢说明数据形状不对：{len(old)}"
        assert len(ids1) == 20 and len(ids2) == 5, (len(ids1), len(ids2))
        assert not (set(ids1) & set(ids2)), "两页有交集 → 分页会重复看到同一个会话"
        assert set(ids1) | set(ids2) == set(sids), "翻页翻不到某个会话"
        # 排序：新 → 旧（第一页第一条是那个 90 条消息的最新会话）
        assert ids1[0] == sids[-1], (ids1[0], sids[-1])
        assert page1[0]["preview"] and page1[0]["last_role"] in ("user", "assistant"), page1[0]
        await _purge(*sids)
        await _drop_users(uid)
    run(main())


def test_list_sessions_is_deterministic_when_last_at_ties():
    """同一时间戳的多个会话必须靠 id 兜底成全序，否则翻页会重复 + 漏项。"""
    async def main():
        uid = await _mk_user("txi_tie")
        sids = [uuid.uuid4().hex for _ in range(6)]
        for sid in sids:
            await _mk_session(sid, user_id=uid)
            await _seed_msgs(sid, 2, 10, user_id=uid)
        async with engine.begin() as c:
            await c.execute(text(FILL_POINTERS))
            await c.execute(  # 故意全刷成同一个时间戳
                text("update chat_sessions set last_at = :t where user_id = :u"),
                {"t": BASE_T, "u": uid},
            )
        async with AsyncSessionLocal() as db:
            a = [r["session_id"] for r in await list_sessions(db, uid, limit=2)]
            b = [r["session_id"] for r in await list_sessions(db, uid, limit=2)]
            pages = []
            for off in (0, 2, 4):
                pages += [r["session_id"] for r in await list_sessions(db, uid, limit=2, offset=off)]
        assert a == b, "同一页取两次结果不同 → 排序不是全序"
        assert len(set(pages)) == 6 and len(pages) == 6, pages
        await _purge(*sids)
        await _drop_users(uid)
    run(main())


def test_list_sessions_ownership_and_edge_rows():
    """归属边界不许退化：别人的会话、游客会话、软删会话、没聊过的会话都不能出现。"""
    async def main():
        uid_a, uid_b = await _mk_user("txi_a"), await _mk_user("txi_b")
        mine, yours, guest, dead, never = (uuid.uuid4().hex for _ in range(5))
        await _mk_session(mine, user_id=uid_a)
        await _mk_session(yours, user_id=uid_b)
        await _mk_session(guest, user_id=None)
        await _mk_session(dead, user_id=uid_a)
        await _mk_session(never, user_id=uid_a)      # 签发了但一句话没说
        for sid in (mine, yours, guest, dead):
            await _seed_msgs(sid, 2, 12, user_id=uid_a if sid in (mine, dead) else uid_b)
        async with engine.begin() as c:
            await c.execute(text(FILL_POINTERS))
            await c.execute(
                text("update chat_sessions set deleted_at = now() where session_id = :s"), {"s": dead}
            )
        async with AsyncSessionLocal() as db:
            rows = await list_sessions(db, uid_a, limit=50)
        got = {r["session_id"] for r in rows}
        assert got == {mine}, got            # 只有自己的、活着真聊过的那一个
        assert never not in got and guest not in got and yours not in got and dead not in got
        # 越权面：B 只能看见 B 的
        async with AsyncSessionLocal() as db:
            rows_b = {r["session_id"] for r in await list_sessions(db, uid_b, limit=50)}
        assert rows_b == {yours}, rows_b
        await _purge(mine, yours, guest, dead, never)
        await _drop_users(uid_a, uid_b)
    run(main())


def test_list_sessions_survives_a_soft_deleted_tail_message():
    """尾部消息被软删时：会话还是要列出来，预览留空，而不是整条消失或 500。"""
    async def main():
        uid = await _mk_user("txi_soft")
        sid = uuid.uuid4().hex
        await _mk_session(sid, user_id=uid)
        ids = await _seed_msgs(sid, 3, 15, user_id=uid)
        async with engine.begin() as c:
            await c.execute(text(FILL_POINTERS))
            await c.execute(
                text("update chat_messages set deleted_at = now() where id = :i"), {"i": ids[-1]}
            )
        async with AsyncSessionLocal() as db:
            rows = await list_sessions(db, uid, limit=10)
        assert len(rows) == 1, rows
        assert rows[0]["session_id"] == sid and rows[0]["preview"] == "", rows[0]
        assert rows[0]["last_at"], rows[0]
        await _purge(sid)
        await _drop_users(uid)
    run(main())


def test_sessions_endpoint_contract():
    """路由层的对外契约：字段集合不变、limit/offset 真生效、参数非法按项目规矩回 400。

    服务层测试证明不了 `SessionOut` 还在序列化这四个字段、也证明不了 Query 校验
    没把一个坏请求变成 500 —— 前端只看得见这两件事。
    """
    async def main():
        name = f"txi_api_{uuid.uuid4().hex[:6]}"
        pwd = "Passw0rd!x"
        async with make_client() as c:
            r = await c.post("/api/v1/auth/register",
                             json={"username": name, "password": pwd, "phone": _phone()})
            assert r.status_code == 200, r.text
            # 注册/登录都吃限流额度：不清掉就会把 701 读成"接口坏了"（test_auth 里同样的坑）
            async for k in redis_client.scan_iter("rl:*"):
                await redis_client.delete(k)
            login = (await c.post("/api/v1/auth/login", json={"username": name, "password": pwd})).json()
            # token 在 data.access_token 里（data 直接当字符串用会发出 "Bearer {'access_token'...}"，
            # 症状是 401 登录凭证无效 —— 我第一版就是这么误判成接口坏了）
            tok = login["data"]["access_token"]
            h = {"Authorization": f"Bearer {tok}"}

            async with engine.connect() as ex:
                uid = (await ex.execute(
                    text("select id from users where username = :n"), {"n": name}
                )).scalar()
            sids = [uuid.uuid4().hex for _ in range(5)]
            for sid in sids:
                await _mk_session(sid, user_id=uid)
                await _seed_msgs(sid, 2, 12, user_id=uid)
            async with engine.begin() as ex:
                await ex.execute(text(FILL_POINTERS))

            body = (await c.get("/api/v1/chat/sessions", headers=h)).json()
            data = body["data"]
            assert set(body) == {"code", "msg", "data"}, body
            assert len(data) == 5, data
            assert all(set(d) == {"session_id", "last_at", "preview", "last_role"} for d in data), data
            # 同一批种子里 last_at 全等于同一个事务时间 → 排序必然落到 id DESC 兜底
            assert [d["session_id"] for d in data] == list(reversed(sids)), data

            p1 = (await c.get("/api/v1/chat/sessions?limit=2", headers=h)).json()["data"]
            p2 = (await c.get("/api/v1/chat/sessions?limit=2&offset=2", headers=h)).json()["data"]
            p3 = (await c.get("/api/v1/chat/sessions?limit=2&offset=4", headers=h)).json()["data"]
            assert [len(x) for x in (p1, p2, p3)] == [2, 2, 1], (len(p1), len(p2), len(p3))
            got = [d["session_id"] for x in (p1, p2, p3) for d in x]
            assert len(set(got)) == 5 and got == [d["session_id"] for d in data], got

            # 参数越界：本项目 HTTP 状态码 = 业务码，校验错就是 400，不是 422 也不是 500
            assert (await c.get("/api/v1/chat/sessions?limit=0", headers=h)).status_code == 400
            assert (await c.get("/api/v1/chat/sessions?offset=-1", headers=h)).status_code == 400
            # 没带 token 仍然是 401（这条是 P1 定的边界，不该被这次改动碰松）
            assert (await c.get("/api/v1/chat/sessions")).status_code == 401

            await _purge(*sids)
            async with engine.begin() as ex:
                await ex.execute(delete(User).where(User.id == uid))
    run(main())
