"""会话归属测试：全部离线。

走的是"输入命中敏感词"那条回答分支 —— 那条路径在调模型之前就 return，
所以这些用例既不花钱也不抖动，但签发、归属判定、claim、历史读取全在同一条链上。
判据矩阵单独用纯函数测，因为它有 6 个分支，靠 HTTP 打满太贵。
"""

import json
import re
import uuid
from contextlib import contextmanager

from conftest import make_client, run
from sqlalchemy import delete, select, text

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.models.custcare import ChatMessage, ChatSession, SensitiveWord
from app.services import session_store


class _Row:
    """喂给 _verify 的最小行替身：它只读这三个属性，不需要碰数据库。"""

    def __init__(self, user_id=None, guest_secret_hash=None, created_by="guest"):
        self.user_id = user_id
        self.guest_secret_hash = guest_secret_hash
        self.created_by = created_by


def test_verify_matrix():
    h = session_store._digest("s3cret")
    # 已归属的会话只认 user_id
    assert session_store._verify(_Row(user_id=7), guest_secret=None, user_id=7) == (True, "owner")
    assert session_store._verify(_Row(user_id=7), guest_secret=None, user_id=8) == (
        False, "owner_mismatch")
    # 游客会话：secret 是唯一凭据；登录用户拿着它不算归属，必须先 claim
    assert session_store._verify(_Row(guest_secret_hash=h), guest_secret="s3cret", user_id=None) == (
        True, "guest_secret")
    assert session_store._verify(_Row(guest_secret_hash=h), guest_secret="wrong", user_id=None) == (
        False, "secret_mismatch")
    assert session_store._verify(_Row(guest_secret_hash=h), guest_secret=None, user_id=None) == (
        False, "secret_missing")
    assert session_store._verify(_Row(guest_secret_hash=h), guest_secret="s3cret", user_id=7) == (
        False, "unclaimed_guest")
    # 认领后凭据作废：hash 为 NULL 时任何 secret 都不认
    assert session_store._verify(_Row(guest_secret_hash=None), guest_secret="s3cret", user_id=None) == (
        False, "secret_revoked")


def test_digest_is_sha256_and_not_reversible():
    d = session_store._digest("abc")
    assert len(d) == 64 and d.startswith("ba7816bf")
    # 明文不同但都短，摘要不能长得像输入
    assert "abc" not in d


async def _one_blocked_word() -> str:
    async with AsyncSessionLocal() as db:
        w = await db.scalar(select(SensitiveWord.word).where(SensitiveWord.word_type == "in"))
    assert w, "敏感词表里必须有 in 类词，否则这些用例会去打真实模型"
    return w


async def _stream(client, body, token=None):
    """发一条消息，返回 (status, session 帧)。非 200 时帧为 None。"""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with client.stream("POST", "/api/v1/chat/stream", json=body, headers=headers) as r:
        if r.status_code != 200:
            await r.aread()
            return r.status_code, r.json()
        buf = ""
        async for chunk in r.aiter_text():
            buf += chunk
            m = re.search(r"event: session\ndata: (.*)", buf)
            if m:
                return r.status_code, json.loads(m.group(1))
    return r.status_code, None


async def _register(client) -> tuple[str, str]:
    name = f"tx_cs_{uuid.uuid4().hex[:6]}"
    phone = "13" + str(uuid.uuid4().int % 10**9).zfill(9)
    await client.post("/api/v1/auth/register",
                      json={"username": name, "password": "Passw0rd!x", "phone": phone})
    r = await client.post("/api/v1/auth/login",
                          json={"username": name, "password": "Passw0rd!x"})
    return name, r.json()["data"]["access_token"]


@contextmanager
def _mode(enforced: bool):
    """显式设定归属模式。

    测试一律不许依赖默认值：默认已经翻成强制了，靠默认跑出来的"只记录"用例
    会静默变成在测别的东西、还假装通过。恢复成进来时的值，不写死 True。
    """
    settings = get_settings()
    prev = settings.chat_ownership_enforced
    settings.chat_ownership_enforced = enforced
    try:
        yield
    finally:
        settings.chat_ownership_enforced = prev


def record_only():
    return _mode(False)


def enforced():
    return _mode(True)


def test_default_is_enforced():
    """P1 的完成标志就是这个默认值。谁把它退回 False，这条测试要响。"""
    from app.core.config import Settings

    assert Settings.model_fields["chat_ownership_enforced"].default is True, (
        "归属校验默认必须是强制开启；需要只记录请显式设环境变量"
    )


def test_issue_reuse_and_no_plaintext_stored():
    async def main():
        word = await _one_blocked_word()
        async with make_client() as c:
            st, s = await _stream(c, {"message": word})
            assert st == 200, s
            assert (s["created"], s["owner_ok"], s["reason"]) == (True, True, "issued"), s
            sid, secret = s["session_id"], s["guest_secret"]
            assert len(secret) >= 32 and len(sid) == 32

            row = await _session_row(sid)
            assert row["created_by"] == "guest" and row["user_id"] is None
            # 库里只存摘要：明文出现在任何一列都算泄漏
            assert row["guest_secret_hash"] != secret and len(row["guest_secret_hash"]) == 64

            st, s2 = await _stream(c, {"message": word, "session_id": sid, "guest_secret": secret})
            assert (s2["created"], s2["owner_ok"], s2["reason"]) == (False, True, "guest_secret"), s2
            # 只有签发那一帧给明文，复用帧里必须是 null
            assert s2["guest_secret"] is None, s2

            st, s3 = await _stream(c, {"message": word, "session_id": "f" * 32})
            assert (s3["owner_ok"], s3["reason"], s3["guest_secret"]) == (
                False, "unknown_sid", None), s3
            await _purge(sid)
    with record_only():
        run(main())


def test_record_only_allows_but_enforced_rejects_410():
    """同一个失败请求，两种模式下唯一差别就是放行还是 410。

    跑两遍而不是改一次标志：这样"翻开开关到底改变了什么"是被实测对照出来的，
    不是读代码读出来的。
    """
    async def main(mode: bool):
        word = await _one_blocked_word()
        async with make_client() as c:
            st, s = await _stream(c, {"message": word})
            sid = s["session_id"]
            with _mode(mode):
                st, s2 = await _stream(c, {"message": word, "session_id": sid})
                if mode:
                    # 非 SSE 的普通 JSON 响应，业务码 = HTTP 码 = 410
                    assert (st, s2["code"]) == (410, 410), s2
                else:
                    assert (st, s2["owner_ok"], s2["reason"]) == (200, False, "secret_missing"), s2
                    st, s3 = await _stream(c, {"message": word, "session_id": sid,
                                               "guest_secret": "z" * 40})
                    assert (st, s3["reason"]) == (200, "secret_mismatch"), s3

                # 自造 sid：只记录时放行并如实标注，强制时 410
                st, s5 = await _stream(c, {"message": word, "session_id": "d" * 32})
                assert st == (410 if mode else 200), s5

                # 签发永远允许：不带 sid 就没有"归属失败"这回事
                st, s4 = await _stream(c, {"message": word})
                assert (st, s4["created"], s4["owner_ok"]) == (200, True, True), s4
                await _purge(s4["session_id"])
                if not mode:
                    await _purge("d" * 32)
            await _purge(sid)

    with record_only():
        run(main(False))
    with enforced():
        run(main(True))


def test_claim_is_idempotent_and_keeps_pre_claim_history():
    async def main():
        word = await _one_blocked_word()
        async with make_client() as c:
            name, tok = await _register(c)
            st, s = await _stream(c, {"message": word})
            sid, secret = s["session_id"], s["guest_secret"]

            # 登录态拿着未认领的游客会话：只记录阶段放行，但归属明确是失败的
            st, s2 = await _stream(c, {"message": word, "session_id": sid}, token=tok)
            assert (s2["owner_ok"], s2["reason"]) == (False, "unclaimed_guest"), s2

            # 缺凭据 -> 400；错凭据 -> 410；都不改归属。
            # 必须带 token：没有 JWT 时依赖先抛 401，请求体压根走不到校验。
            r = await c.post("/api/v1/chat/session/claim", json={"session_id": sid},
                             headers={"Authorization": f"Bearer {tok}"})
            assert (r.status_code, r.json()["code"]) == (400, 400), r.text
            r = await c.post("/api/v1/chat/session/claim",
                             json={"session_id": sid, "guest_secret": "q" * 40},
                             headers={"Authorization": f"Bearer {tok}"})
            assert (r.status_code, r.json()["code"]) == (410, 410), r.text
            assert (await _session_row(sid))["user_id"] is None

            r = await c.post("/api/v1/chat/session/claim",
                             json={"session_id": sid, "guest_secret": secret},
                             headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 200 and r.json()["data"]["reason"] == "claimed", r.text
            row = await _session_row(sid)
            uid = row["user_id"]
            assert row["guest_secret_hash"] is None, "认领后凭据必须作废"
            assert row["created_by"] == "guest", "created_by 记的是当初谁建的，不该被改写"

            # 幂等：第三次、第四次结果一致，且都是 200
            for _ in range(2):
                r = await c.post("/api/v1/chat/session/claim",
                                 json={"session_id": sid, "guest_secret": secret},
                                 headers={"Authorization": f"Bearer {tok}"})
                assert r.status_code == 200 and r.json()["data"]["reason"] == "already_claimed", r.text

            # 回填：认领前的发言 user_id 不是 NULL 了，否则登录后的过滤会把它吞掉
            nulls = await _count_nulls(sid)
            assert nulls == 0, f"认领后仍有 {nulls} 条消息 user_id 为 NULL"

            # 认领前聊的内容，认领后读得到
            r = await c.get("/api/v1/chat/history", params={"session_id": sid},
                            headers={"Authorization": f"Bearer {tok}"})
            data = r.json()["data"]
            assert r.status_code == 200 and len(data) >= 2, r.text
            assert {i["role"] for i in data} == {"user", "assistant"}, data

            # 别人 claim 不动
            _, tok2 = await _register(c)
            r = await c.post("/api/v1/chat/session/claim",
                             json={"session_id": sid, "guest_secret": secret},
                             headers={"Authorization": f"Bearer {tok2}"})
            assert r.status_code == 410, r.text
            await _purge(sid, uid=uid)
    with record_only():
        run(main())


def test_history_without_credentials_still_401_for_guests():
    """只记录模式必须和 P1 之前一模一样：游客读历史还是 401，不是 200 空表。

    这条是给"record-only 不改变可观测行为"兜底的。哪天有人为了让游客能读历史
    把这里改成 200/[]，"翻开前后没有行为变化"那句话就再也没法证明了。
    强制模式下同样一个请求是 410，由 test_enforced_history_rejects_forged_sid_with_410 覆盖。
    """
    async def main():
        sid = uuid.uuid4().hex
        async with make_client() as c:
            r = await c.get("/api/v1/chat/history", params={"session_id": sid})
            assert (r.status_code, r.json()["code"]) == (401, 401), r.text
            name, tok = await _register(c)
            r = await c.get("/api/v1/chat/history", params={"session_id": sid},
                            headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 200 and r.json()["data"] == [], r.text
    with record_only():
        run(main())


def test_enforced_history_rejects_forged_sid_with_410():
    """强制模式：没有凭据就是 410（不再是 401），带对 secret 就正常读。"""
    async def main():
        word = await _one_blocked_word()
        async with make_client() as c:
            st, s = await _stream(c, {"message": word})
            sid, secret = s["session_id"], s["guest_secret"]
            r = await c.get("/api/v1/chat/history", params={"session_id": "e" * 32})
            assert (r.status_code, r.json()["code"]) == (410, 410), r.text
            r = await c.get("/api/v1/chat/history",
                            params={"session_id": sid, "guest_secret": secret})
            assert r.status_code == 200 and len(r.json()["data"]) >= 2, r.text
            await _purge(sid)
    with enforced():
        run(main())


def test_enforced_rejection_writes_nothing_into_anothers_session():
    """归属没过就一个字节都不许写。

    这是完整性的那一半：只记录阶段放行时，外来请求会把自己的消息**写进受害者的会话**
    （下一轮受害者读到的上下文里就多了别人的一句话）。410 必须挡在写库之前，
    所以这里断言的是"消息条数一根手指都没动"，不是"没返回内容"。
    """
    async def main():
        word = await _one_blocked_word()
        async with make_client() as c:
            _, tokA = await _register(c)
            st, s = await _stream(c, {"message": word}, token=tokA)
            sid = s["session_id"]
            _, tokB = await _register(c)
            before = await _count_msgs(sid)
            assert before >= 2, before

            with enforced():
                # 另一个登录用户拿别人的 sid
                st, body = await _stream(c, {"message": word, "session_id": sid}, token=tokB)
                assert (st, body["code"]) == (410, 410), body
                assert await _count_msgs(sid) == before, "被拒的请求写进了别人的会话"
                # 游客拿别人的 sid
                st, body = await _stream(c, {"message": word, "session_id": sid})
                assert st == 410, body
                assert await _count_msgs(sid) == before, "被拒的游客请求写进了别人的会话"
                # 读也不行
                r = await c.get("/api/v1/chat/history", params={"session_id": sid},
                                headers={"Authorization": f"Bearer {tokB}"})
                assert (r.status_code, r.json()["code"]) == (410, 410), r.text
                # 主人自己当然照常能用
                st, s2 = await _stream(c, {"message": word, "session_id": sid}, token=tokA)
                assert (st, s2["owner_ok"], s2["reason"]) == (200, True, "owner"), s2
                assert await _count_msgs(sid) == before + 2, "主人的请求该被写入"
            await _purge(sid)
    run(main())


async def _count_msgs(sid) -> int:
    async with AsyncSessionLocal() as db:
        return int(await db.scalar(
            text("select count(*) from chat_messages where session_id = :s"), {"s": sid}))


async def _session_row(sid) -> dict:
    """返回普通 dict 而不是 ORM 对象：session 一关，属性访问就会抛 DetachedInstanceError。"""
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(ChatSession.created_by, ChatSession.user_id, ChatSession.guest_secret_hash)
            .where(ChatSession.session_id == sid)
        )).first()
    assert row is not None, sid
    return {"created_by": row[0], "user_id": row[1], "guest_secret_hash": row[2]}


async def _count_nulls(sid) -> int:
    async with AsyncSessionLocal() as db:
        n = await db.scalar(text(
            "select count(*) from chat_messages where session_id = :s and user_id is null"),
            {"s": sid})
    return int(n)


async def _purge(sid, uid=None):
    """测试留下的会话和消息一律清掉：共享库里的脏用例会污染下一次评测。"""
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ChatMessage).where(ChatMessage.session_id == sid))
        await db.execute(delete(ChatSession).where(ChatSession.session_id == sid))
        if uid:
            await db.execute(delete(ChatMessage).where(ChatMessage.user_id == uid))
            await db.execute(delete(ChatSession).where(ChatSession.user_id == uid))
        await db.commit()
