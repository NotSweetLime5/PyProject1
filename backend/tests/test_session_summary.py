"""P3A 会话滚动摘要测试：全部确定性，不调模型。

分四层，每层守的东西不一样：

1. 尾部指针（last_message_id/last_at）—— 摘要调度的唯一判据依赖它。它错了整个 job
   要么空转要么漏会话，而这两种都不报错。
2. plan_fold 的切分规则 —— 它必须和 load_history 的采纳规则**逐字一致**，
   否则"保留区"和"读侧真读到的范围"会错开，中间那段同时躲开摘要和窗口。
   这条一致性有专门用例正面比对两边的字数合计。
3. 水位线（summary_through_message_id）—— 只有摘要成功落库才允许推进。
   推进了却没落库 = 那段历史永久没人再读，是不可逆丢失，
   所以"模型返回空"和"水位被别人抢先推进"两种失败都必须有用例钉住。
4. 读侧注入 —— 摘要那条消息的形状，以及"没摘要时行为和 P2 完全一样"。
"""

import json
import os
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from conftest import run
from langchain_core.messages import SystemMessage
from sqlalchemy import delete, select, update

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.models.custcare import ChatMessage, ChatSession
from app.services.chat_service import load_history, save_message, stream_answer
from app.services.chat_summary import plan_fold, summarize_session


@contextmanager
def knobs(**overrides):
    """临时改配置。测试不许依赖默认值 —— 默认是标定出来的运营值，会变。"""
    s = get_settings()
    saved = {k: getattr(s, k) for k in overrides}
    for k, v in overrides.items():
        setattr(s, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(s, k, v)


def _mem(spec):
    """plan_fold 是纯函数，用内存行就够，不必碰数据库。spec=[(role, 字数)]，id 从 1 递增。"""
    return [SimpleNamespace(id=i, role=role, content="内" * chars)
            for i, (role, chars) in enumerate(spec, start=1)]


def _six_turns(per=100):
    """6 个完整轮：user/per 字 + assistant/per 字，共 12 行、1200 字（per=100 时）。"""
    out = []
    for _ in range(6):
        out.append(("user", per))
        out.append(("assistant", per))
    return out


async def _mk_session(sid: str) -> None:
    async with AsyncSessionLocal() as db:
        db.add(ChatSession(session_id=sid, user_id=None, guest_secret_hash=None,
                           created_by="guest"))
        await db.commit()


async def _purge(sid: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ChatMessage).where(ChatMessage.session_id == sid))
        await db.execute(delete(ChatSession).where(ChatSession.session_id == sid))
        await db.commit()


async def _session_row(sid: str) -> dict | None:
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(ChatSession).where(ChatSession.session_id == sid)
        )).scalar_one_or_none()
        if row is None:
            return None
        return {
            "last_message_id": row.last_message_id,
            "last_at": row.last_at,
            "summary": row.summary,
            "wm": row.summary_through_message_id,
            "revision": row.summary_revision,
            "schema": row.summary_schema_version,
        }


async def _seed(sid: str, spec) -> list[int]:
    """spec = [(role, 字数 或 正文)]。走 save_message —— 顺带把尾部指针立起来，
    这才是真实写入路径（直插 ChatMessage 的用例测不到那部分）。"""
    ids = []
    async with AsyncSessionLocal() as db:
        for role, body in spec:
            m = await save_message(
                db, session_id=sid, user_id=None, role=role,
                content="内" * body if isinstance(body, int) else body,
            )
            ids.append(m.id)
        await db.commit()
    return ids


async def _db_rows(sid: str):
    async with AsyncSessionLocal() as db:
        return list(await db.scalars(
            select(ChatMessage)
            .where(ChatMessage.session_id == sid)
            .order_by(ChatMessage.id)
        ))


# ---------------- 1. 尾部指针 ----------------

def test_save_message_advances_tail_pointer():
    """每条新消息都要把尾部指针推到它自己 —— 单调、同事务、不需要第二次 commit。"""
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid)
        async with AsyncSessionLocal() as db:
            m1 = await save_message(db, session_id=sid, user_id=None, role="user", content="第一句")
            await db.commit()
            got = await _session_row(sid)
            assert got["last_message_id"] == m1.id, got
            assert got["last_at"] is not None, got

            m2 = await save_message(db, session_id=sid, user_id=None, role="assistant", content="第二句")
            await db.commit()
            got2 = await _session_row(sid)
            assert got2["last_message_id"] == m2.id > m1.id, (m1.id, m2.id, got2)
        await _purge(sid)
    run(main())


def test_tail_pointer_rolls_back_with_the_message():
    """回滚时指针不能自己往前走 —— 否则调度会去摘要一段根本不存在的消息。

    这条测的是"同事务"这个说法本身：save_message 只 flush 不 commit，
    指针的 UPDATE 必须挂在同一个事务上，而不是另开一个连接偷偷提交。
    """
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid)
        async with AsyncSessionLocal() as db:
            await save_message(db, session_id=sid, user_id=None, role="user", content="会消失的一句")
            await db.rollback()
        got = await _session_row(sid)
        assert got["last_message_id"] is None, got
        async with AsyncSessionLocal() as db:
            n = (await db.execute(
                select(ChatMessage).where(ChatMessage.session_id == sid)
            )).all()
        assert n == [], n
        await _purge(sid)
    run(main())


def test_save_message_without_session_row_does_not_explode():
    """没有 chat_sessions 行的会话（老数据/直插测试）写入照常成功，指针影响 0 行。

    归属校验强制开启后正常路径总有行，但 save_message 不该假设这件事：
    它一旦被"行不存在"拖崩，故障点会落在流式回答的中途。
    """
    async def main():
        sid = uuid.uuid4().hex
        async with AsyncSessionLocal() as db:
            msg = await save_message(db, session_id=sid, user_id=None, role="user", content="无主会话")
            await db.commit()
            assert msg.id, msg
        assert await _session_row(sid) is None
        await _purge(sid)
    run(main())


# ---------------- 2. plan_fold 的切分 ----------------

def test_plan_fold_skips_when_window_still_fits():
    """整段还装得进读窗口就不该摘要 —— 没有东西被挤出去时花一次 LLM 调用纯属亏钱。"""
    rows = _mem([("user", 100), ("assistant", 100), ("user", 100), ("assistant", 100)])
    assert plan_fold(rows, char_budget=600, max_messages=128,
                     min_fold_chars=100, fold_max_chars=4000) is None


def test_plan_fold_cuts_on_turn_boundary():
    """切点必须落在整轮上：把提问折进摘要、把答语留在原文里，读到的就是一个悬空话题。"""
    rows = _mem(_six_turns(100))
    plan = plan_fold(rows, char_budget=600, max_messages=128,
                     min_fold_chars=100, fold_max_chars=5000)
    assert plan is not None
    # 6 轮 × 200 字，预算 600 → 保留最新 3 轮，折最旧 3 轮（id 1..6）
    assert [m.id for m in plan.rows] == [1, 2, 3, 4, 5, 6], [m.id for m in plan.rows]
    assert plan.cut_message_id == 6
    # 切点之后第一条是 user —— 保留区的第一轮是完整的
    assert rows[6].role == "user", rows[6].role


def test_plan_fold_hysteresis_needs_enough_evicted():
    """刚挤出一轮就摘要 = 每轮对话多付一次调用。不足 min_fold_chars 就再等等。"""
    rows = _mem([("user", 100), ("assistant", 100)] * 4)   # 800 字，预算 600 → 只挤出 200
    assert plan_fold(rows, char_budget=600, max_messages=128,
                     min_fold_chars=300, fold_max_chars=5000) is None
    # 同一个形状，把回滞调小就该动了 —— 证明上一条是回滞挡的，不是别的规则误伤
    plan = plan_fold(rows, char_budget=600, max_messages=128,
                     min_fold_chars=100, fold_max_chars=5000)
    assert plan is not None and plan.chars == 200, plan


def test_plan_fold_never_folds_the_guaranteed_newest_turn():
    """最近一轮无条件整轮保留（和读侧同一条保底规则）：哪怕它自己就超预算也不能折 ——
    那样窗口里就只剩一段摘要，最新的对话反而看不见。"""
    # 只有一轮，且它自己就超预算：没有"更旧的"东西可折，必须返回 None 而不是把这轮折掉
    only = _mem([("user", 100), ("assistant", 1000)])
    assert plan_fold(only, char_budget=600, max_messages=128,
                     min_fold_chars=1, fold_max_chars=5000) is None

    # 保底轮之外更旧的那些照常折 —— 折的边界绝不能爬到保底轮里去（这里保底轮是 id 3、4）
    rows = _mem([("user", 100), ("assistant", 100), ("user", 100), ("assistant", 1000)])
    plan = plan_fold(rows, char_budget=600, max_messages=128,
                     min_fold_chars=1, fold_max_chars=5000)
    assert plan is not None and plan.cut_message_id == 2, plan
    assert [m.id for m in plan.rows] == [1, 2], plan.rows


def test_plan_fold_one_pass_is_capped():
    """巨型会话一次只折 fold_max_chars，剩下的下一轮继续折（单调前进，摘要调用也不会被撑爆）。"""
    rows = _mem(_six_turns(100))
    plan = plan_fold(rows, char_budget=600, max_messages=128,
                     min_fold_chars=100, fold_max_chars=400)
    assert plan.chars == 400 and plan.cut_message_id == 4, plan
    # 第二轮从新水位接着折，必须还能前进（否则会卡在同一个水位上空转）
    rest = [m for m in rows if m.id > plan.cut_message_id]
    again = plan_fold(rest, char_budget=600, max_messages=128,
                      min_fold_chars=100, fold_max_chars=400)
    assert again is not None and again.cut_message_id > plan.cut_message_id, again


def test_fold_region_plus_reader_region_is_exactly_everything():
    """no-limbo 不变式：摘要折掉的 + 读侧看见的 == 全部原文，一条不差。

    这是本模块唯一真正重要的等式。它不成立的那一刻，就有一段历史既不在 summary 里、
    也不在模型看到的消息里 —— 而水位线已经过去了，以后再也轮不到它。
    这个状态不报错，只表现成"客服突然忘了前面说过的话"。

    第二组（被条数保险丝挡住的那类）是这条用例的关键：
    **只抄字符规则、不抄条数规则，它一定会在这里挂。**
    """
    async def main():
        cases = (
            ("char_budget 挡住的", _six_turns(100), 600, 128),
            ("max_messages 挡住的", [("user", 10), ("assistant", 10)] * 8, 6000, 4),
            # 最新一轮是 0 字（中断重放会留下 content="" 的行）+ 条数保险丝正好在它后面咬住。
            # 这组是专门用来打"用字数当'已经收过一轮'的标志"那种写法的：
            # 那种写法在这里会多收一轮，等式当场不成立（实测过：改回 kept 就挂）。
            ("零字轮 + 保险丝", [("user", 100), ("assistant", 100), ("user", 0), ("assistant", 0)],
             200, 2),
        )
        for name, spec, budget, cap in cases:
            sid = uuid.uuid4().hex
            await _mk_session(sid)
            await _seed(sid, spec)
            rows = await _db_rows(sid)
            total = sum(len(m.content) for m in rows)
            async with AsyncSessionLocal() as db:
                w = await load_history(db, sid, None, char_budget=budget, max_messages=cap)
            plan = plan_fold(rows, char_budget=budget, max_messages=cap,
                             min_fold_chars=1, fold_max_chars=10 ** 9)
            folded = plan.chars if plan else 0
            assert w.chars + folded == total, (name, w.chars, folded, total)
            if plan:
                kept_ids = {m.id for m in rows} - {r.id for r in plan.rows}
                assert plan.cut_message_id == max(r.id for r in plan.rows) < min(kept_ids), name
            await _purge(sid)
    run(main())


# ---------------- 3. 水位线：只有成功才推进 ----------------

def _fake(text):
    """记录调用参数的假摘要器（零 API 调用）。"""
    calls = []

    async def fn(prev, transcript):
        calls.append((prev, transcript))
        return text
    fn.calls = calls
    return fn


def test_summarize_success_writes_summary_and_watermark():
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid)
        ids = await _seed(sid, _six_turns(100))
        fn = _fake("用户先问预算内的轻薄本，客服推荐了 XPS 14；后来转到退换货。")
        with knobs(chat_history_char_budget=600, chat_summary_min_fold_chars=100,
                   chat_summary_fold_max_chars=5000, chat_summary_target_chars=80):
            done = await summarize_session(sid, summarizer=fn)
        assert done == 1, done
        got = await _session_row(sid)
        assert got["wm"] == ids[5], (got["wm"], ids[5])   # 折掉最旧 3 轮 → 水位到第 6 条
        assert got["revision"] == 1, got
        assert got["schema"] == 2, got
        assert got["summary"].startswith("用户先问预算内的轻薄本"), got
        # 送进模型的是被折那几轮的原文，按 id 升序、带角色标注
        prev, transcript = fn.calls[0]
        assert not prev, repr(prev)
        source_rows = [json.loads(line) for line in transcript.splitlines()]
        assert len(source_rows) == 6, transcript
        assert [r["source_message_id"] for r in source_rows] == ids[:6]
        assert [r["role"] for r in source_rows] == ["user", "assistant"] * 3
        assert all(r["content"] == "内" * 100 for r in source_rows)
        await _purge(sid)
    run(main())


def test_empty_model_output_does_not_advance_watermark():
    """模型返回空 → 一个字都不写、水位一步都不推。

    这是最贵的一种错：推进水位等于宣布"这段历史我已经交给摘要了"，而摘要其实是空的。
    那段对话从此两边都不沾，且不会再有第二次机会。
    """
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid)
        await _seed(sid, _six_turns(100))
        with knobs(chat_history_char_budget=600, chat_summary_min_fold_chars=100,
                   chat_summary_fold_max_chars=5000, chat_summary_target_chars=80):
            assert await summarize_session(sid, summarizer=_fake("")) == 0
            assert await summarize_session(sid, summarizer=_fake("   \n  ")) == 0
        got = await _session_row(sid)
        assert got["wm"] is None and got["summary"] is None, got
        assert got["revision"] == 0, got
        await _purge(sid)
    run(main())


def test_watermark_moved_by_other_writer_discards_this_pass():
    """compare-and-set：水位在我规划之后被别人推进过，本轮成果只能丢掉。

    写成无条件覆盖的话，会把别人更新的水位**退回去** —— 那比丢一次摘要严重得多：
    区间之间会裂开一段谁都不负责的空白。
    """
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid)
        await _seed(sid, _six_turns(100))

        async def sneaky(prev, transcript):
            async with AsyncSessionLocal() as other:
                await other.execute(
                    update(ChatSession)
                    .where(ChatSession.session_id == sid)
                    .values(summary_through_message_id=999_999, summary="别人先写好的摘要")
                )
                await other.commit()
            return "我这轮白做了"

        with knobs(chat_history_char_budget=600, chat_summary_min_fold_chars=100,
                   chat_summary_fold_max_chars=5000, chat_summary_target_chars=80):
            assert await summarize_session(sid, summarizer=sneaky) == 0
        got = await _session_row(sid)
        assert got["wm"] == 999_999, got
        assert got["summary"] == "别人先写好的摘要", got
        assert got["revision"] == 0, got
        await _purge(sid)
    run(main())


def test_summary_is_hard_truncated():
    """落库长度必须 ≤ target。不截就等于把刚省下的预算从另一边花回去，
    而且"最坏多出 target 字"这个上界不再成立。"""
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid)
        await _seed(sid, _six_turns(100))
        with knobs(chat_history_char_budget=600, chat_summary_min_fold_chars=100,
                   chat_summary_fold_max_chars=5000, chat_summary_target_chars=80):
            assert await summarize_session(sid, summarizer=_fake("长" * 5000)) == 1
        got = await _session_row(sid)
        assert len(got["summary"]) == 80, (len(got["summary"]), got)
        await _purge(sid)
    run(main())


def test_second_pass_rolls_previous_summary_in():
    """滚动摘要：第二次的输入必须带上第一次的摘要正文，水位继续往前，revision 变 2。"""
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid)
        ids = await _seed(sid, _six_turns(100))
        with knobs(chat_history_char_budget=600, chat_summary_min_fold_chars=100,
                   chat_summary_fold_max_chars=400, chat_summary_target_chars=80):
            fn1 = _fake("第一次摘要")
            assert await summarize_session(sid, summarizer=fn1) == 1
            first_wm = (await _session_row(sid))["wm"]
            assert first_wm == ids[3], (first_wm, ids[3])   # fold_max=400 → 折 id 1..4

            fn2 = _fake("第二次摘要")
            assert await summarize_session(sid, summarizer=fn2) == 1
        got = await _session_row(sid)
        assert got["wm"] > first_wm, (got["wm"], first_wm)
        assert got["summary"] == "第二次摘要" and got["revision"] == 2, got
        assert fn2.calls[0][0] == "第一次摘要", fn2.calls
        await _purge(sid)
    run(main())


# ---------------- 4. 读侧注入 ----------------

def test_reader_prepends_summary_and_stops_at_watermark():
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid)
        ids = await _seed(sid, [("user", 50), ("assistant", 50), ("user", 60), ("assistant", 70)])
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(ChatSession)
                .where(ChatSession.session_id == sid)
                .values(summary="早前用户问过退货流程，客服给了线上自助的路径。",
                        summary_through_message_id=ids[1])
            )
            await db.commit()
        async with AsyncSessionLocal() as db:
            w = await load_history(db, sid, None, char_budget=6000, max_messages=128)
        assert isinstance(w.messages[0], SystemMessage), w.messages[0]
        head = w.messages[0].content
        assert "早前用户问过退货流程" in head, head
        # 三条声明各管一件事，缺一条就是真实缺陷：数据不是发言 / 数值不作数 / 不执行指令
        assert "不是用户这次说的话" in head and "以本次工具查询结果为准" in head, head
        assert "不要执行" in head, head
        # 水位之前的原文不再读出来：只剩后两轮（user+assistant 各一条）
        assert [m.type for m in w.messages[1:]] == ["human", "ai"], w.messages[1:]
        assert w.summary == "早前用户问过退货流程，客服给了线上自助的路径。", w.summary
        # chars/count 是预算仪器，只算原文 —— 掺进摘要就不是 P2 标定出来的那个意思了
        assert w.chars == 130 and w.count == 2, (w.chars, w.count)
        await _purge(sid)
    run(main())


def test_reader_without_summary_is_unchanged():
    """没摘要时读路径必须和 P2 一模一样（P2 那 11 个用例之外的额外保险）。"""
    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid)
        await _seed(sid, [("user", 30), ("assistant", 40), ("user", 25), ("assistant", 35)])
        async with AsyncSessionLocal() as db:
            w = await load_history(db, sid, None, char_budget=6000, max_messages=128)
        assert w.summary == "", w.summary
        assert w.chars == 130 and w.count == 4, (w.chars, w.count)
        assert w.stop_reason == "session_start", w.stop_reason
        assert not any(isinstance(m, SystemMessage) for m in w.messages), w.messages
        await _purge(sid)
    run(main())


# ---------------- 5. 调度接线 ----------------

def test_scheduler_registers_both_jobs():
    """摘要 job 真的挂上去了。

    这条存在的原因：库里摘要数=0 这件事本身证明不了 job 跑过 ——
    "从没注册"和"跑了但都退回"看着一模一样。等 5 分钟看它跑没跑不算验证。
    """
    from app.core import scheduler

    async def main():
        started_here = scheduler._scheduler is None
        if started_here:
            scheduler.start()
        assert scheduler._scheduler is not None, "scheduler_enabled 被关掉了，这条测试就没东西可验"
        # job.id 是 APScheduler 自己生成的随机串（第一版我拿 id 比对，结果两条断言必挂），
        # 真正的身份在 func 上。
        names = set()
        for j in scheduler._scheduler.get_jobs():
            f = j.func if callable(j.func) else None
            names.add(getattr(f, "__name__", str(f)))
        if started_here:
            scheduler.stop()
        return names

    with knobs(scheduler_enabled=True):
        names = run(main())
    assert "run_order_cleanup" in names, names
    assert "run_chat_summary_job" in names, names


def test_summary_job_respects_the_redis_lock(monkeypatch):
    """锁被别人持有时本轮直接跳过，一次摘要都不做（多副本部署的关键）。"""
    from app.core import scheduler

    async def no_lock(*a, **k):
        return None

    calls = []

    async def fake_run():
        calls.append(1)
        return 0

    monkeypatch.setattr(scheduler, "try_lock", no_lock)
    monkeypatch.setattr(scheduler, "run_chat_summary", fake_run)
    assert run(scheduler.run_chat_summary_job()) == 0
    assert calls == [], calls


# ---------------- 6. 真模型冒烟（RUN_LLM=1，默认跳过） ----------------

def llm(fn):
    """本地定义，不 import test_chat：两个测试模块互相依赖会让收集阶段变得难测。"""
    fn = pytest.mark.llm(fn)
    return pytest.mark.skipif(
        os.environ.get("RUN_LLM") != "1", reason="需要真实大模型调用，设 RUN_LLM=1 开启"
    )(fn)


@llm
def test_live_summary_carries_a_fact_the_window_dropped():
    """验两件确定性用例给不了的东西：

    ① 传输形状 —— DeepSeek + create_agent 能不能接受"消息列表中间夹一条 SystemMessage"，
       并且不把我们这条当成 agent 自己的 system prompt 顶掉。这是 provider 层假设，
       单元测试永远测不出来，而它一旦不成立就是每次回答都报错。
    ② 摘要到底有没有用 —— 被窗口挤掉的那句硬性条件，必须只能经由摘要回到回答里。

    实测通过（2026-10-07）：种子 112 条 / 9121 字，窗口 76 条 / 6156 字（预算挡住的），
    真模型摘要 120 字，回答逐条复述出"预算 6800 / 只要联想或 ThinkPad / 不要游戏本 / 出差随身"。
    """
    fact = "我先说清楚：我买电脑是出差带的，预算最高六千八百块，只要联想或者ThinkPad，绝对不要游戏本。"
    filler = "这款的接口、屏幕色域、续航和重量我都有点在意，麻烦你再把参数说细一点，尤其是出差随身带的场景下到底合不合适。"

    async def main():
        sid = uuid.uuid4().hex
        await _mk_session(sid)
        spec = [("user", fact), ("assistant", filler * 3)]
        # 凑到 9000 字以上而不是猜"几轮够长"：那句预算要是还在窗口里，这条用例就什么都没验
        while sum(len(b) for _, b in spec) < 9000:
            spec.append(("user", filler))
            spec.append(("assistant", filler * 2))
        await _seed(sid, spec)

        async with AsyncSessionLocal() as db:
            before = await load_history(db, sid, None)
        assert not any("六千八百" in getattr(m, "content", "") for m in before.messages), \
            "预算那句还在原始窗口里，种子不够长（这条用例失去区分度）"
        assert before.stop_reason == "char_budget", before.stop_reason

        assert await summarize_session(sid) == 1
        async with AsyncSessionLocal() as db:
            after = await load_history(db, sid, None)
        assert isinstance(after.messages[0], SystemMessage), after.messages[0]
        assert "六千八百" in after.messages[0].content, after.messages[0].content[:200]

        ans, errs = [], []
        async for ev in stream_answer(
            session_id=sid, user_id=None,
            message="你还记得我最开始提的硬性条件吗？逐条复述一遍，然后说该买哪款。",
        ):
            if ev["type"] == "token":
                ans.append(ev["data"])
            elif ev["type"] == "error":
                errs.append(ev["data"])
        text = "".join(ans)
        assert not errs, errs
        for kw in ("6800", "联想", "游戏本"):
            assert kw in text, (kw, text[:400])
        await _purge(sid)
    run(main())
