"""默认配置的上下文连续性、调度推进和跨会话偏好，零外部模型调用。"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessageChunk
from sqlalchemy import delete, select, update

from conftest import make_client, run
from app.core.database import AsyncSessionLocal
from app.core.security import create_access_token
from app.models.custcare import ChatMessage, ChatSession, UserMemory
from app.models.user import User
from app.services import chat_service, chat_summary, user_memory
from app.services.chat_lease import ChatBusy, session_lease
from test_session_summary import knobs


async def seed(spec, *, user_id=None, order=0):
    sid = uuid.uuid4().hex
    async with AsyncSessionLocal() as db:
        db.add(ChatSession(session_id=sid, user_id=user_id, created_by="user" if user_id else "guest"))
        await db.flush()
        for role, body in spec:
            await chat_service.save_message(db, session_id=sid, user_id=user_id, role=role,
                                            content=body if isinstance(body, str) else "内" * body)
        await db.execute(update(ChatSession).where(ChatSession.session_id == sid)
                         .values(last_at=datetime(2000, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=order)))
        await db.commit()
    return sid


async def purge(sids):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ChatMessage).where(ChatMessage.session_id.in_(sids)))
        await db.execute(delete(ChatSession).where(ChatSession.session_id.in_(sids)))
        await db.commit()


async def new_user():
    async with AsyncSessionLocal() as db:
        row = User(username=f"tx_mem_{uuid.uuid4().hex[:16]}", password_hash="unused",
                   phone="1" + str(uuid.uuid4().int)[-10:])
        db.add(row)
        await db.commit()
        return row.id


def test_fold_cap_keeps_complete_turn_under_default_limits():
    spec = [("user", 100), ("assistant", 3800)] * 3 + [("user", 400), ("assistant", 1000)] + [("user", 100), ("assistant", 3800)] * 3
    rows = [SimpleNamespace(id=i, role=r, content="内" * n) for i, (r, n) in enumerate(spec, 1)]
    plan = chat_summary.plan_fold(rows, char_budget=6000, max_messages=128, min_fold_chars=1500, fold_max_chars=12000)
    assert plan.cut_message_id == 8
    assert plan.rows[-1].role == "assistant" and rows[8].role == "user"


def test_dispatch_moves_past_ten_short_sessions(monkeypatch):
    async def fake_summary(prev, transcript):
        return "用户最早的预算是 6800。"
    monkeypatch.setattr(chat_summary, "_llm_summarize", fake_summary)

    async def main():
        sids = []
        try:
            for i in range(10):
                sids.append(await seed([("user", 10), ("assistant", 10)], order=i))
            long_sid = await seed([("user", 500), ("assistant", 1500)] * 5, order=10)
            sids.append(long_sid)
            assert await chat_summary.run_chat_summary() == 0
            assert await chat_summary.run_chat_summary() >= 1
            async with AsyncSessionLocal() as db:
                row = await db.scalar(select(ChatSession).where(ChatSession.session_id == long_sid))
                assert row.summary and row.summary_through_message_id
                short = await db.scalar(select(ChatSession).where(ChatSession.session_id == sids[0]))
                assert short.summary_checked_through_message_id == short.last_message_id
                old_checked = short.summary_checked_through_message_id
                msg = await chat_service.save_message(db, session_id=sids[0], user_id=None, role="user", content="再问一句")
                assert msg.id > old_checked
                await db.commit()
        finally:
            await purge(sids)
    run(main())


def test_short_message_fuse_gap_is_summarized_on_demand():
    calls = []
    async def fake(prev, transcript):
        calls.append(transcript)
        return "用户早先要求预算不超过 6800。"
    async def main():
        spec = [("user", "最初预算不超过6800"), ("assistant", 10)] + [("user", 10), ("assistant", 10)] * 99
        sid = await seed(spec)
        try:
            async with AsyncSessionLocal() as db:
                before = await chat_service.load_history(db, sid, None)
            assert before.has_gap and before.stop_reason == "max_messages"
            assert not any("6800" in m.content for m in before.messages)
            # 回滞会跳过，但请求内压缩必须补齐。
            assert await chat_summary.summarize_session(sid, summarizer=fake) == 0
            window = await chat_service.prepare_history(sid, None, summarizer=fake)
            assert not window.has_gap
            assert "6800" in window.summary and "最初预算" in calls[0]
        finally:
            await purge([sid])
    run(main())


def test_summary_failure_preserves_watermark_and_allows_retry():
    async def empty(prev, transcript):
        return ""
    async def good(prev, transcript):
        return "早期约束。"
    async def main():
        sid = await seed([("user", 500), ("assistant", 1500)] * 5)
        try:
            with pytest.raises(RuntimeError):
                await chat_service.prepare_history(sid, None, summarizer=empty)
            async with AsyncSessionLocal() as db:
                row = await db.scalar(select(ChatSession).where(ChatSession.session_id == sid))
                assert row.summary_through_message_id is None
            assert not (await chat_service.prepare_history(sid, None, summarizer=good)).has_gap
        finally:
            await purge([sid])
    run(main())


def test_old_scan_cap_does_not_mistake_old_messages_for_latest():
    async def fake(prev, transcript):
        return "保留早期约束。"
    async def main():
        sid = await seed([("user", 100), ("assistant", 100)] * 20)
        try:
            with knobs(chat_history_max_scanned=10, chat_history_fetch_block=4,
                       chat_history_char_budget=600, chat_summary_min_fold_chars=1,
                       chat_summary_fold_max_chars=400, chat_summary_request_max_passes=20):
                window = await chat_service.prepare_history(sid, None, summarizer=fake)
                assert not window.has_gap
                async with AsyncSessionLocal() as db:
                    latest = await db.scalar(select(ChatMessage.id).where(ChatMessage.session_id == sid)
                                             .order_by(ChatMessage.id.desc()).limit(1))
                assert window.rows[-1].id == latest
        finally:
            await purge([sid])
    run(main())


def test_request_summary_limit_preserves_progress_for_retry():
    async def fake(prev, transcript):
        return "早期约束。"
    async def main():
        sid = await seed([("user", 100), ("assistant", 100)] * 8)
        try:
            with knobs(chat_history_char_budget=600, chat_summary_fold_max_chars=400,
                       chat_summary_request_max_passes=1):
                with pytest.raises(RuntimeError):
                    await chat_service.prepare_history(sid, None, summarizer=fake)
                async with AsyncSessionLocal() as db:
                    row = await db.scalar(select(ChatSession).where(ChatSession.session_id == sid))
                    assert row.summary_through_message_id and row.summary_revision == 1
            with knobs(chat_history_char_budget=600, chat_summary_fold_max_chars=400):
                assert not (await chat_service.prepare_history(sid, None, summarizer=fake)).has_gap
        finally:
            await purge([sid])
    run(main())


def test_summary_kill_switch_never_calls_model_on_request():
    calls = []
    async def fake(prev, transcript):
        calls.append(transcript)
        return "摘要"
    async def main():
        sid = await seed([("user", 500), ("assistant", 1500)] * 5)
        try:
            with knobs(chat_summary_enabled=False):
                with pytest.raises(RuntimeError):
                    await chat_service.prepare_history(sid, None, summarizer=fake)
            assert calls == []
        finally:
            await purge([sid])
    run(main())


@pytest.mark.parametrize("message, expected", [
    ("记住：品牌偏好=联想", {"brand_preference": "联想"}),
    ("我一直偏好ThinkPad", {"brand_preference": "ThinkPad"}),
    ("我以后偏好苹果", {"brand_preference": "苹果"}),
    ("请记住我喜欢联想", {"brand_preference": "联想"}),
    ("记住我的品牌偏好是联想", {"brand_preference": "联想"}),
    ("我保存的长期品牌偏好是什么？只回答品牌", {}),
    ("你记住我的品牌偏好是联想吗？", {}),
    ("记住：品牌偏好是什么？", {}),
    ("记住：常用用途=出差办公；记住：回答风格=简洁", {"usual_use": "出差办公", "answer_style": "简洁"}),
    ("忘记我的品牌偏好", {"brand_preference": None}),
    ("请忘记我的品牌偏好", {"brand_preference": None}),
    ("帮我删除我的品牌偏好", {"brand_preference": None}),
    ("我想清除我的常用用途", {"usual_use": None}),
    ("删除品牌偏好", {"brand_preference": None}),
    # 撤销只认"这句话本身就是命令"。咨询怎么删、以及被否定词开头的，一律不动数据。
    # 「怎么删除我的品牌偏好」是原缺陷的复现：撤销分支以前用 search，会把这句执行成删除。
    ("怎么删除我的品牌偏好", {}),
    ("如何清除我的回答风格", {}),
    ("不要忘记我的品牌偏好", {}),
    # 刻意的保守方向：不以撤销动词开头的说法不触发删除，用户重说一遍或改用面板即可，
    # 而误删需要用户显式重设才能恢复 —— 两种错不等价，所以这里判"不删"。
    ("把我的品牌偏好删除", {}),
    ("这次记住：品牌偏好=联想", {}),
    ("这次买电脑预算6000", {}),
    ("我喜欢联想", {}),
    ("商品价格=6000，订单状态=已发货", {}),
])
def test_only_explicit_stable_preferences_are_captured(message, expected):
    assert user_memory.extract_preferences(message) == expected


class RecordingAgent:
    def __init__(self):
        self.contexts = []

    async def astream_events(self, payload, **kwargs):
        self.contexts.append(payload["messages"])
        yield {"event": "on_chat_model_stream", "data": {"chunk": AIMessageChunk(content="我可以按您的偏好查询商品。")}}


def test_preferences_cross_sessions_correct_delete_and_survive_new_db_session(monkeypatch):
    agent = RecordingAgent()
    monkeypatch.setattr(chat_service, "get_agent", lambda uid: agent)
    async def main():
        uid = await new_user()
        sids = []
        try:
            for message in ["我一直偏好联想", "帮我挑个电脑", "我以后偏好苹果", "再帮我挑个电脑", "忘记我的品牌偏好", "再帮我挑个电脑"]:
                sid = await seed([], user_id=uid)
                sids.append(sid)
                events = [ev async for ev in chat_service.stream_answer(session_id=sid, user_id=uid, message=message)]
                assert any(ev["type"] == "done" for ev in events), events
            assert len(agent.contexts) == 3  # 三条纯偏好操作由后端回执，另外三条商品咨询调用 Agent。
            assert "联想" in "".join(m.content for m in agent.contexts[0])
            corrected = "".join(m.content for m in agent.contexts[1])
            assert "苹果" in corrected and "联想" not in corrected
            deleted = "".join(m.content for m in agent.contexts[2])
            assert '"已删除偏好": ["品牌偏好"]' in deleted
            assert "联想" not in deleted and "苹果" not in deleted
            async with AsyncSessionLocal() as db:
                assert await user_memory.list_memories(db, uid) == []
                row = (await user_memory.list_memories(db, uid, include_deleted=True))[0]
                assert row.deleted_at and row.revision == 3 and row.value == ""
                other = await new_user()
                assert await user_memory.context_block(db, other) == ""
                assert await user_memory.context_block(db, None) == ""
        finally:
            await purge(sids)
    run(main())


def test_memory_api_ownership_validation_and_tombstones():
    async def main():
        a, b = await new_user(), await new_user()
        ah = {"Authorization": f"Bearer {create_access_token(a)}"}
        bh = {"Authorization": f"Bearer {create_access_token(b)}"}
        path = "/api/v1/chat/memories"
        async with make_client() as client:
            assert (await client.get(path)).status_code == 401
            assert (await client.put(path + "/brand_preference", headers=ah, json={"value": "联想"})).status_code == 200
            assert (await client.get(path, headers=bh)).json()["data"] == []
            assert (await client.put(path + "/brand_preference", headers=bh, json={"value": "苹果"})).status_code == 200
            assert (await client.get(path, headers=ah)).json()["data"][0]["value"] == "联想"
            for key, value in [("price", "6000"), ("usual_use", "   "), ("usual_use", "a\nb"), ("usual_use", "长" * 121)]:
                assert (await client.put(path + "/" + key, headers=ah, json={"value": value})).status_code == 400
            assert (await client.delete(path + "/brand_preference", headers=ah)).status_code == 200
            assert (await client.get(path, headers=ah)).json()["data"] == []
            assert (await client.get(path, headers=bh)).json()["data"][0]["value"] == "苹果"
            assert (await client.put(path + "/brand_preference", headers=ah, json={"value": "华为"})).status_code == 200
            assert (await client.get(path, headers=ah)).json()["data"][0]["revision"] == 3
    run(main())


def test_session_lease_rejects_overlap_releases_and_renews():
    async def main():
        sid = uuid.uuid4().hex
        async with session_lease(sid, ttl=1):
            with pytest.raises(ChatBusy):
                async with session_lease(sid, ttl=1):
                    pass
            await asyncio.sleep(1.2)
            with pytest.raises(ChatBusy):
                async with session_lease(sid, ttl=1):
                    pass
        async with session_lease(sid, ttl=1):
            pass
    run(main())


def test_summary_and_memory_usage_are_actually_logged(monkeypatch, caplog):
    agent = RecordingAgent()
    monkeypatch.setattr(chat_service, "get_agent", lambda uid: agent)
    async def main():
        uid = await new_user()
        sid = await seed([], user_id=uid)
        try:
            async with AsyncSessionLocal() as db:
                await db.execute(update(ChatSession).where(ChatSession.session_id == sid).values(summary="早期约束"))
                await db.commit()
            with caplog.at_level("INFO", logger="chat.obs"):
                events = [ev async for ev in chat_service.stream_answer(session_id=sid, user_id=uid, message="记住：回答风格=简洁。帮我推荐电脑。")]
            done = next(ev["data"] for ev in events if ev["type"] == "done")
            assert done["obs"]["memory_chars"] > 0
            assert "summary_chars=4" in caplog.text and "memory_chars=" in caplog.text
            # 这一轮没有任何工具事件，计数应为 0；但**字段必须出现在日志行里** ——
            # "算出来了却没接进 _log_obs" 是登记过的缺陷形状（分析 4.4），0 也能测出来。
            assert done["obs"]["goods_hits_evidenced"] == 0
            assert "goods_hits_evidenced=0" in caplog.text, caplog.text[-500:]
        finally:
            await purge([sid])
    run(main())


class _ToolAgent:
    """只发 on_tool_end，用来验 artifact 里的 hit_tiers 累加到 obs 这一段管线。"""

    def __init__(self):
        self.artifacts = [
            {"refs": [{"type": "goods", "id": 81, "name": "华为Mate60 智能手机", "price": "5499.00"}],
             "hit_tiers": {"evidenced": 2, "semantic_only": 1}},
            {"refs": [{"type": "goods", "id": 82, "name": "华为nova12 Ultra 手机", "price": "3999.00"}],
             "hit_tiers": {"evidenced": 0, "semantic_only": 3}},
        ]

    async def astream_events(self, payload, **kwargs):
        for art in self.artifacts:
            yield {"event": "on_tool_start", "name": "search_goods", "data": {}}
            yield {"event": "on_tool_end", "data": {"output": SimpleNamespace(artifact=art)}}
        yield {"event": "on_chat_model_stream",
               "data": {"chunk": AIMessageChunk(content="这几款的资料里没写明防水，我再帮您核对。")}}


def test_goods_hit_tiers_accumulate_from_artifact_into_log(monkeypatch, caplog):
    agent = _ToolAgent()
    monkeypatch.setattr(chat_service, "get_agent", lambda uid: agent)
    async def main():
        uid = await new_user()
        sid = await seed([], user_id=uid)
        try:
            with caplog.at_level("INFO", logger="chat.obs"):
                events = [ev async for ev in chat_service.stream_answer(
                    session_id=sid, user_id=uid, message="有没有防水的耳机")]
            done = next(ev["data"] for ev in events if ev["type"] == "done")
            # 2+0 与 1+3：同一轮多次 search_goods 必须累加，不能被最后一次覆盖
            assert done["obs"]["goods_hits_evidenced"] == 2, done["obs"]
            assert done["obs"]["goods_hits_semantic_only"] == 4, done["obs"]
            assert "goods_hits_evidenced=2 goods_hits_semantic_only=4" in caplog.text, caplog.text[-500:]
        finally:
            await purge([sid])
    run(main())


@pytest.mark.llm
@pytest.mark.skipif(os.environ.get("RUN_LLM") != "1", reason="需要真实模型，设 RUN_LLM=1 开启")
def test_live_agent_reads_corrected_and_deleted_memory_in_new_sessions():
    async def main():
        uid = await new_user()
        sids = []
        try:
            replies = []
            for value in ["联想", "苹果", None]:
                async with AsyncSessionLocal() as db:
                    await user_memory.set_memory(db, uid, "brand_preference", value)
                    await db.commit()
                sid = await seed([], user_id=uid)
                sids.append(sid)
                events = [ev async for ev in chat_service.stream_answer(
                    session_id=sid, user_id=uid, message="我保存的长期品牌偏好是什么？只回答偏好，没有保存就说不知道，不需要查询商品。")]
                assert not any(ev["type"] == "error" for ev in events), events
                answer = "".join(ev["data"] for ev in events if ev["type"] == "token")
                replies.append(answer)
            assert "联想" in replies[0], replies
            assert "苹果" in replies[1] and "联想" not in replies[1], replies
            assert "联想" not in replies[2] and "苹果" not in replies[2], replies
            assert any(word in replies[2] for word in ["不知道", "没有", "未", "暂无"]), replies
        finally:
            await purge(sids)
    run(main())
