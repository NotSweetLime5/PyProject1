"""P2 历史窗口测试：全部确定性，不调模型。

为什么不用评测来验这些：预算/保底/停止原因都是纯函数级的规则，
种几行消息直接调 `load_history` 就能验，而且必须每次跑（评测又贵又有概率性，
用它兜这类逻辑等于把确定性缺陷藏进噪声里）。
"""

import uuid
from contextlib import contextmanager

from conftest import run
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.models.custcare import ChatMessage, ChatSession
from app.services.chat_service import load_history


@contextmanager
def window(**overrides):
    """临时改窗口参数。测试不许依赖默认值 —— 默认是标定出来的运营值，会变。"""
    s = get_settings()
    saved = {k: getattr(s, k) for k in overrides}
    for k, v in overrides.items():
        setattr(s, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(s, k, v)


async def _seed(sid: str, turns: list[list[tuple[str, int]]], user_id=None) -> int:
    """turns = [[('user', 字数), ('assistant', 字数)], ...]，正序（最早在前）。"""
    n = 0
    async with AsyncSessionLocal() as db:
        for turn in turns:
            for role, chars in turn:
                # 按请求长度给足：("内容"*500) 只有 1000 字，[:3000] 会静默变成 1000 字，
                # 那会让所有"按字数"的断言悄悄测错东西（第一版就栽在这里）。
                body = ("内容" * (chars // 2 + 1))[:chars] if chars else "x"
                db.add(ChatMessage(session_id=sid, user_id=user_id, role=role, content=body))
                n += 1
        await db.commit()
    return n


async def _mk_session(sid: str, user_id=None) -> None:
    async with AsyncSessionLocal() as db:
        db.add(ChatSession(session_id=sid, user_id=user_id, guest_secret_hash=None,
                           created_by="guest" if user_id is None else "user"))
        await db.commit()


async def _purge(sid: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ChatMessage).where(ChatMessage.session_id == sid))
        await db.execute(delete(ChatSession).where(ChatSession.session_id == sid))
        await db.commit()


def _roles(msgs) -> list[str]:
    return ["user" if m.type == "human" else "ai" for m in msgs]


async def _mk_user() -> int:
    """建一个测试用户（tx_ 前缀，conftest 的会话级清理会收走）。"""
    from app.models.user import User

    name = f"tx_hw_{uuid.uuid4().hex[:6]}"
    # phone 上有唯一索引（第一次跑就撞了 duplicate key），两个测试用户不能共用一个号
    phone = "13" + str(uuid.uuid4().int % 10**9).zfill(9)
    async with AsyncSessionLocal() as db:
        u = User(username=name, password_hash="x", phone=phone, is_active=True)
        db.add(u)
        await db.commit()
        await db.refresh(u)
        return u.id


async def _drop_user(*ids: int) -> None:
    from app.models.user import User

    async with AsyncSessionLocal() as db:
        await db.execute(delete(User).where(User.id.in_(ids)))
        await db.commit()


def _db():
    """每次开一个短命 session：load_history 只读，不需要长生命周期。"""
    return AsyncSessionLocal()


def test_small_session_stops_at_session_start():
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid)
        await _seed(sid, [[("user", 30), ("assistant", 40)], [("user", 25), ("assistant", 35)]])
        async with _db() as db:
            w = await load_history(db, sid, None, char_budget=6000, max_messages=128)
        assert w.stop_reason == "session_start", w.stop_reason
        assert w.count == 4, w.count
        assert _roles(w.messages) == ["user", "ai", "user", "ai"], _roles(w.messages)
        assert w.chars == 130, w.chars
        await _purge(sid)
    run(main())


def test_char_budget_stops_on_whole_turn_boundary():
    """每轮 200 字，预算 450 → 收下 3 轮共 600 字后停。

    注意是 600 不是 400：判据是"开始下一轮之前有没有超预算"，所以允许最后一轮
    顶破预算。反过来要求"整轮装得下才收"会让轮次原子性咬到自己 ——
    实测那种写法把 6123 字的历史砍到 3063，一条稳定正确的用例直接退化。
    """
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid)
        await _seed(sid, [[("user", 100), ("assistant", 100)] for _ in range(6)])
        async with _db() as db:
            w = await load_history(db, sid, None, char_budget=450, max_messages=128)
        assert w.stop_reason == "char_budget", w.stop_reason
        assert w.chars == 600, f"必须整轮采纳且允许顶破，实得 {w.chars}"
        assert w.count == 6, w.count
        assert _roles(w.messages)[-1] == "ai", "最后一句必须是答语，不能是半轮提问"
        assert _roles(w.messages)[0] == "user", "轮边界不能落在 assistant 上"
        await _purge(sid)
    run(main())


def test_budget_does_not_strand_half_the_window():
    """区分新旧写法的关键形状：单轮略小于预算。

    四轮分别 3060 / 3050 / 3000 / 3000 字，预算 6000。
    旧写法（"下一轮整个装得下才收"）：收 3060 后，3050 装不进剩下的 2940 → 停，
    历史只剩 3060，**预算用掉一半**。新写法：3060 + 3050 = 6110 收满两轮再停。
    这正是 P2 验收实测出来的真回归（cal-bulk-far 3/3 → 0/3）。
    """
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid)
        # 正序写（最早在前）：最新的两轮必须"略小于预算"，才区分得出两种写法
        sizes = [(1500, 1500), (1500, 1500), (1525, 1525), (1530, 1530)]
        await _seed(sid, [[("user", u), ("assistant", a)] for u, a in sizes])
        async with _db() as db:
            w = await load_history(db, sid, None, char_budget=6000, max_messages=128)
        assert w.count == 4, f"两轮 4 条都该收下，实得 {w.count} 条 / {w.chars} 字"
        assert w.chars == 6110, w.chars
        assert w.stop_reason == "char_budget", w.stop_reason
        await _purge(sid)
    run(main())


def test_first_turn_kept_whole_even_when_over_budget():
    """软预算：最近一轮 900 字 > 预算 500，仍整轮保留，然后停。

    这条是"不许把历史压成 0 条"的保证：一条超长回答不能让用户看不到任何上下文。
    """
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid)
        await _seed(sid, [
            [("user", 60), ("assistant", 60)],
            [("user", 400), ("assistant", 500)],  # 最近一轮 900 字
        ])
        async with _db() as db:
            w = await load_history(db, sid, None, char_budget=500, max_messages=128)
        assert w.first_turn_over_budget is True, w
        assert w.stop_reason == "char_budget", w.stop_reason
        assert w.count == 2, w.count
        assert w.chars == 900, w.chars
        assert _roles(w.messages) == ["user", "ai"], _roles(w.messages)
        await _purge(sid)
    run(main())


def test_max_messages_is_a_fuse_that_can_fire_first():
    """极端超短消息会话：预算 100 万、条数上限 6 → 由 max_messages 先挡住。

    用户明确纠正过我的表述：128 不是"永远不会先触发"，而是"先触发也允许"。
    这条测试就是把那个允许钉住。
    """
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid)
        await _seed(sid, [[("user", 4), ("assistant", 4)] for _ in range(20)])
        async with _db() as db:
            w = await load_history(db, sid, None, char_budget=1_000_000, max_messages=6)
        assert w.stop_reason == "max_messages", w.stop_reason
        assert w.count == 6, w.count
        assert w.chars == 24, w.chars
        await _purge(sid)
    run(main())


def test_fuse_never_splits_the_guaranteed_turn():
    """条数保险丝也不能把保底轮砍半：一轮 3 条 > 上限 2 → 仍然整轮收下。"""
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid)
        # 一轮里塞 3 条：user → ai → ai（连续两条答语是中断重放能出现的形状）
        await _seed(sid, [[("user", 50), ("assistant", 50)],
                          [("user", 30), ("assistant", 30), ("assistant", 30)]])
        async with _db() as db:
            w = await load_history(db, sid, None, char_budget=1_000_000, max_messages=2)
        assert w.count == 3, f"保底轮必须整轮，实得 {w.count}"
        assert _roles(w.messages) == ["user", "ai", "ai"], _roles(w.messages)
        await _purge(sid)
    run(main())


def test_dangling_user_turn_still_kept():
    """会话末尾只有一句提问、没有答语（落库中断的形状）：不能因此返回空历史。"""
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid)
        await _seed(sid, [[("user", 40), ("assistant", 40)], [("user", 33)]])
        async with _db() as db:
            w = await load_history(db, sid, None, char_budget=1_000_000, max_messages=128)
        assert w.count == 3, w.count
        assert _roles(w.messages) == ["user", "ai", "user"], _roles(w.messages)
        assert w.stop_reason == "session_start", w.stop_reason
        await _purge(sid)
    run(main())


def test_keyset_pagination_matches_a_single_full_read():
    """块大小远小于会话长度时，结果必须和"一次读全"完全一致：无重复、无漏、正序。

    这是 keyset 最容易错的地方（cursor 边界写反会漏一条或多一条）。
    """
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid)
        turns = [[("user", 30 + i), ("assistant", 40 + i)] for i in range(25)]
        await _seed(sid, turns)
        async with _db() as db:
            # 参照答案：直接用旧的全量读法取 50 条
            ref = list(await db.scalars(
                select(ChatMessage).where(ChatMessage.session_id == sid)
                .order_by(ChatMessage.id.asc())))
            with window(chat_history_fetch_block=7):
                w = await load_history(db, sid, None, char_budget=10_000_000, max_messages=128)
        assert w.count == len(ref) == 50, (w.count, len(ref))
        assert [m.content for m in w.messages] == [r.content for r in ref], "正序或去重有问题"
        assert w.blocks >= 4, f"块大小 7、50 条至少该读 8 块，实得 {w.blocks}"
        assert w.scanned == 50, w.scanned
        await _purge(sid)
    run(main())


def test_keyset_stops_reading_once_budget_is_met():
    """预算到了就不再往前翻：扫描条数应远小于会话长度。

    块大小显式设小（8），否则默认 64 一块就把整个断言空间吃光了 ——
    第一块读满 64 条时预算早就满足，"扫了 64 条"本身是正确的。
    """
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid)
        await _seed(sid, [[("user", 200), ("assistant", 200)] for _ in range(40)])
        async with _db() as db:
            with window(chat_history_fetch_block=8):
                w = await load_history(db, sid, None, char_budget=800, max_messages=128)
        assert w.stop_reason == "char_budget", w.stop_reason
        assert w.count <= 8, w.count
        assert w.scanned <= 16, f"扫了 {w.scanned} 条 / 共 80 条，keyset 没起作用"
        assert w.blocks <= 2, w.blocks
        await _purge(sid)
    run(main())


def test_default_budget_is_the_calibrated_value_not_a_context_window_guess():
    """默认值来自标定。谁把它改回"按条数"或按 1M 放大，这条要响。"""
    from app.core.config import Settings

    f = Settings.model_fields
    assert f["chat_history_char_budget"].default == 6000
    assert f["chat_history_max_messages"].default == 128
    assert "chat_history_turns" not in f, "按条数定义窗口的旧配置应该已经删掉"


def test_user_id_filter_still_applies_for_logged_in_legacy_rows():
    """P1 的兼容路径不能被 P2 弄丢：登录用户的老数据仍按 user_id 过滤。

    用真实用户 id，不编一个数字：chat_messages.user_id 有外键，
    编造的 id 会在插入时就炸，测试根本测不到过滤逻辑。
    """
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid)
        uid = await _mk_user()
        await _seed(sid, [[("user", 50), ("assistant", 50)]], user_id=None)
        await _seed(sid, [[("user", 60), ("assistant", 60)]], user_id=uid)
        async with _db() as db:
            w = await load_history(db, sid, uid, char_budget=1_000_000, max_messages=128)
        assert w.count == 2, w.count
        assert w.chars == 120, w.chars
        # 换一个用户来读：那句无主的（游客时期写的）不该进上下文
        uid2 = await _mk_user()
        async with _db() as db:
            w2 = await load_history(db, sid, uid2, char_budget=1_000_000, max_messages=128)
        assert w2.count == 0, w2.count
        await _purge(sid)
        await _drop_user(uid, uid2)
    run(main())
