"""客服链路测试。

拆成两半是刻意的：
- 不带 llm 标记的这些**不需要调大模型**（输入拦截那条路径在调模型之前就 return 了），
  所以每次跑测试都会验，覆盖的是护栏、阈值、数据形状这些确定性逻辑。
- 带 llm 标记的要真打 DeepSeek/SiliconFlow，花钱且有概率性，默认跳过，
  用 RUN_LLM=1 显式开启。把不稳定的东西隔离开，测试套件才有意义。
"""

import json
import os
import re
import random
import uuid

import pytest
from conftest import make_client, run
from sqlalchemy import delete

from app.core.sensitive import filter_input, get_word_set, load_from_db, mask
from app.core.database import AsyncSessionLocal
from app.models.custcare import ChatMessage, ChatSession
from app.services.faq_service import SEMANTIC_FLOOR, search_faqs


def _issued_sid(body: str) -> str:
    """从 SSE 文本里取服务端签发的 sid。

    这些用例测的是回答内容和数据形状，不是归属；但它们原来都自己编一个
    `uuid4().hex` 当 sid —— 归属强制之后那种请求直接 410，用例就变成在测归属了。
    所以统一改成"不带 sid 发出去，把签发结果读回来"，走真实契约。
    """
    m = re.search(r'"session_id":\s*"([0-9a-f]{32})"', body)
    assert m, f"没有 session 帧（可能被归属挡了）：{body[:200]}"
    return m.group(1)


async def _drop(sid: str) -> None:
    """清掉本用例造的会话与消息。游客会话 user_id 是 NULL，conftest 的按用户清理够不着。"""
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ChatMessage).where(ChatMessage.session_id == sid))
        await db.execute(delete(ChatSession).where(ChatSession.session_id == sid))
        await db.commit()


def llm(fn):
    """既打上可供 -m "llm" / -m "not llm" 选择的标记，又带上默认跳过。

    之前写成 `llm = pytest.mark.skipif(...)` 是个静默失效的错：
    那样只加了 skipif，没有名为 llm 的标记，
    于是 `-m "llm"` 永远选不出用例，`-m "not llm"` 也什么都没排除 ——
    看起来工作正常，其实选择器一直是空转的。
    """
    fn = pytest.mark.llm(fn)
    return pytest.mark.skipif(
        os.environ.get("RUN_LLM") != "1", reason="需要真实大模型调用，设 RUN_LLM=1 开启"
    )(fn)


def _phone() -> str:
    # 手机号必须是 11 位纯数字且匹配 ^1[3-9]\d{9}$，
    # 用 uuid 的 hex 片段会带 a-f 字母，校验直接 400
    return "13" + "".join(random.choice("0123456789") for _ in range(9))


def _sse_events(text: str):
    out = []
    for block in text.strip().split("\n\n"):
        ev = re.search(r"^event: (.+)$", block, re.M)
        dt = re.search(r"^data: (.+)$", block, re.M)
        if ev and dt:
            out.append((ev.group(1), json.loads(dt.group(1))))
    return out


def _collect(text: str):
    tokens, status, refs, done, err = [], [], None, None, None
    for ev, data in _sse_events(text):
        if ev == "token":
            tokens.append(data)
        elif ev == "status":
            status.append(data)
        elif ev == "refs":
            refs = data
        elif ev == "done":
            done = data
        elif ev == "error":
            err = data
    return "".join(tokens), status, refs, done, err


async def _guest_ask(client, message: str, session: dict):
    """遵循服务端签发契约，不能自造 sid 绕过归属校验。"""
    response = await client.post("/api/v1/chat/stream", json={"message": message, **session})
    assert response.status_code == 200, response.text
    frame = next(data for event, data in _sse_events(response.text) if event == "session")
    session["session_id"] = frame["session_id"]
    if frame.get("guest_secret"):
        session["guest_secret"] = frame["guest_secret"]
    return response


def test_faq_threshold_blocks_irrelevant():
    """语义检索必须有下限。向量空间里永远有"最近的邻居"，不设阈值就会把
    毫不相干的 FAQ 塞进 prompt，模型照着编 —— RAG 最常见的失败模式。"""
    async def main():
        async with AsyncSessionLocal() as db:
            junk = await search_faqs(db, "帮我写一首关于秋天的诗")
            assert junk == [], [f"{h.question}:{h.similarity}" for h in junk]
            good = await search_faqs(db, "什么时候发货")
            assert good and good[0].similarity > SEMANTIC_FLOOR
            # search_faqs 会 UPDATE use_count，测试里要退回去，别污染种子数据
            await db.rollback()
    run(main())


def test_sensitive_words_are_directional():
    """三类词各自独立：拦输入的、拦输出的、放行的。"""
    async def main():
        async with AsyncSessionLocal() as db:
            await get_word_set(db)
            # 愤怒的客户必须放行 —— 拦下来等于赶客，那正是客服最该接住的输入
            assert await filter_input(db, "你们就是骗子，我要投诉你们") == []
            # 违法诉求拦截
            assert await filter_input(db, "教我怎么信用卡套现") == ["信用卡套现"]
            # 白名单短语放行同一词
            assert await filter_input(db, "花呗套现是什么意思，我怕被骗") == []
            # 承诺类是 AI 的禁语，不是用户的
            assert await filter_input(db, "你们能保证退款吗") == []
            ws = await load_from_db(db)
            assert ws.deny_out.scan("保证退款") and not ws.deny_in.scan("保证退款")
    run(main())


def test_output_masking_keeps_answer_usable():
    """脱敏而不是整条撤回：撤回会让用户看到"客服突然不说话"。"""
    async def main():
        async with AsyncSessionLocal() as db:
            await get_word_set(db)
        out = mask("请您放心，我们保证退款，商品绝对正品。")
        assert "保证退款" not in out and "绝对正品" not in out, out
        assert "请您放心" in out, out
    run(main())


def test_blocked_input_short_circuits_without_llm():
    """被拦的请求不应该产生任何工具调用或模型流。"""
    async def main():
        async with make_client() as c:
            r = await c.post("/api/v1/chat/stream", json={"message": "你这个傻逼平台"})
            assert r.status_code == 200, r.text
            sid = _issued_sid(r.text)
            answer, status, refs, done, _ = _collect(r.text)
            assert "人工" in answer, answer
            assert done and done.get("blocked") is True, done
            assert status == [], f"被拦却出现了工具状态事件：{status}"
            await _drop(sid)
    run(main())


def test_refs_column_has_exactly_one_shape():
    """JSONB 列一旦允许两种形状，每个读它的地方都要做类型判断，
    漏判的那处会在最冷门的路径上炸（本项目真的炸过一次：/chat/history 500）。"""
    async def main():
        from sqlalchemy import text as sa_text

        async with make_client() as c:
            r = await c.post("/api/v1/chat/stream", json={"message": "你这个傻逼平台"})
            sid = _issued_sid(r.text)
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(
                    sa_text("SELECT jsonb_typeof(refs) FROM chat_messages "
                            "WHERE session_id=:s AND refs IS NOT NULL"),
                    {"s": sid})).all()
            await _drop(sid)
        assert rows, "拦截记录没有落库"
        # 只约束"有 refs 的行必须是数组"。user 那行的 refs 本来就是 NULL，
        # 把它算进来是断言写漏了条件，不是代码的问题。
        assert {r[0] for r in rows} == {"array"}, f"refs 出现了非数组形状：{rows}"
    run(main())


def test_sentence_guard_masks_and_never_eats_text():
    """按句缓冲：拆成单字喂进来，既不许漏禁语，也不许吞掉结尾。

    同时验证流式发出的拼接结果 == 落库文本（guard.sent），这是 mask 分歧的根因。
    """
    async def main():
        from app.core.claim_guard import SentenceGuard
        from app.core.sensitive import get_word_set, mask

        async with AsyncSessionLocal() as db:
            await get_word_set(db)  # load_from_db 只返回值，不填 _cache

        raw = "我们保证退款，请放心。"
        g = SentenceGuard([], mask)
        out = []
        for ch in raw:                      # 最恶劣切法：一字一块
            piece = g.push(ch)
            if piece:
                out.append(piece)
        out.append(g.flush())
        streamed = "".join(out)
        assert "保证退款" not in streamed, streamed
        assert streamed == g.sent, "发出去的与落库用的不是同一份"
        assert streamed.endswith("。"), f"结尾被吞了: {streamed!r}"
        # 这句被 mask 了（mask 也是一种干预），但没有否定断言 -> 不该被改写
        assert g.decision.kinds == ["masked"], g.decision.kinds
        assert not [i for i in g.decision.interventions if i.kind == "hedged"]
    run(main())


def test_unsupported_negation_is_hedged_but_supported_is_not():
    """护栏的两头：查无依据的否定断言要改写，有依据的真话一个字都不许动。

    第二头才是重点 —— 上一版覆盖门就是只测了第一头，结果把 faq-09/12/14
    三条真命中一起砍掉。"不支持寄到海外"是有证据的真话，改它就是新故障。
    """
    from app.core.claim_guard import assertion_about, hedge, unsupported_negation

    ev = "支持哪些支付方式？ 支持支付宝、微信支付、银联云闪付。"
    assert unsupported_negation("本商城暂不支持货到付款。", ev)
    assert not unsupported_negation("目前不支持信用卡支付。", ev), "证据里写了信用卡，这句有依据"

    # 上位词推理：证据说"海外"，句子说"港澳台及海外"，必须放行
    ev2 = "发货可以送到港澳台或者海外吗？ 目前仅支持中国大陆地区配送（不含港澳台及海外）。"
    assert not unsupported_negation("商城不支持寄到港澳台及海外。", ev2)

    # 没有任何否定词的句子一律不动
    # 间接疑问句不是断言：「确认是否提供上门安装」里没有任何"查不到"字样，
    # 但它同样是在转述待确认的问题。漏掉这类标记是评测第九次误判的原因。
    assert assertion_about("建议您联系人工客服确认所购商品是否提供上门安装。", "上门安装") == "hedged"
    assert assertion_about("我这边不确定能否加装电梯。", "加装电梯") == "hedged"
    assert assertion_about("本商城提供上门安装服务。", "上门安装") == "positive"
    assert not unsupported_negation("满 99 元免基础运费。", ev)
    assert not unsupported_negation("您可以在结算页勾选优惠券。", ev)

    # 空证据时的否定断言按可疑处理
    assert unsupported_negation("本商城不提供以旧换新。", [])
    assert hedge("本商城不提供以旧换新。").startswith("这条我在平台规则里没查到")


def test_unsupported_positive_policy_claim_is_hedged():
    """肯定断言护栏：只拦政策语境下的平台断言，不碰商品描述和泛化类别。"""
    from app.core.claim_guard import inspect_sentence, unsupported_positive_claim

    ev = ["支持哪些支付方式？ 支持支付宝、微信支付、银联云闪付。"]

    # 无依据的肯定：专属宾语与证据零重合
    assert unsupported_positive_claim("本商城支持货到付款。", ev, policy_mode=True)
    # 只共享通用词"支付"不能算有依据
    assert unsupported_positive_claim("本商城支持信用卡支付。", ev, policy_mode=True)
    # 有依据的肯定放行
    assert not unsupported_positive_claim("本商城支持支付宝和微信支付。", ev, policy_mode=True)
    # 太泛的类别断言不判，避免误伤
    assert not unsupported_positive_claim("本商城支持在线支付。", ev, policy_mode=True)
    # 兜底/疑问句不是肯定断言
    assert not unsupported_positive_claim("平台规则里没有写明是否支持货到付款。", ev, policy_mode=True)
    # 商品主语不走政策护栏
    assert not unsupported_positive_claim("这款手机支持NFC支付。", ev, policy_mode=True)
    # 没有进入 FAQ 政策语境时不碰肯定句
    assert not unsupported_positive_claim("本商城支持货到付款。", ev, policy_mode=False)
    # 证据明确否定，肯定断言也要拦
    assert unsupported_positive_claim("本商城支持货到付款。", ["本商城不支持货到付款。"], policy_mode=True)
    # 空证据 + 政策语境：专属宾语无依据
    assert unsupported_positive_claim("本店提供免费上门安装服务。", [], policy_mode=True)

    final, d = inspect_sentence("本商城支持货到付款。", ev, lambda s: s, policy_mode=True)
    assert final.startswith("这条我在平台规则里没查到")
    assert d.kinds == ["hedged"]


def test_positive_guard_rewrites_only_the_offending_sentence():
    """流式按句缓冲：只改写出问题的那一句，其余原样保留。"""
    from app.core.claim_guard import SentenceGuard

    ev = ["支持哪些支付方式？ 支持支付宝、微信支付、银联云闪付。"]
    g = SentenceGuard(ev, lambda s: s, policy_mode=True)
    text = "本商城支持支付宝和微信支付。本商城支持货到付款。还有其他问题可以继续问。"
    out = g.push(text) + g.flush()
    assert "支付宝和微信支付" in out, out
    assert "货到付款" not in out, out
    assert "还有其他问题" in out, out
    assert g.decision.kinds == ["hedged"], g.decision.interventions


def test_guard_rewrites_only_the_offending_sentence():
    """改写只能碰出问题那一句，同一段里的其他内容必须原样保留。"""
    async def main():
        from app.core.claim_guard import SentenceGuard
        from app.core.sensitive import get_word_set, mask

        async with AsyncSessionLocal() as db:
            await get_word_set(db)

        ev = ["支持哪些支付方式？ 支持支付宝、微信支付。"]
        g = SentenceGuard(ev, mask)
        text = "平台支持支付宝和微信。本商城暂不支持货到付款。还有其他问题可以继续问。"
        out = g.push(text) + g.flush()
        assert "支付宝和微信" in out, f"好句子被误伤: {out}"
        assert "暂不支持货到付款" not in out, f"该改写的没改: {out}"
        assert "还有其他问题" in out, f"后续内容丢失: {out}"
        assert g.hits == 1, g.hits
    run(main())


def test_search_faq_tool_forbids_inferring_from_absence():
    """工具返回值里必须带"不得据沉默推断"的指令。

    这是评测集 miss-01 抓到的缺陷：模型拿到支付方式清单后答
    "清单里没有货到付款，所以本商城暂不支持货到付款" —— 检索的有界性
    被当成了平台政策的有界性。指令放在工具结果里而不是系统提示词里，
    因为模型读工具输出的权重远高于几十行外的系统指令。
    """
    async def main():
        from app.rag.tools import build_tools

        faq_tool = next(t for t in build_tools(None) if t.name == "search_faq")
        content, artifact = await faq_tool.coroutine(question="什么时候发货")
        assert "禁止据此推断" in content, content[:200]
        assert artifact["refs"], "该有命中却没有，这条测试的前提不成立"

        # 无命中时仍是原来那句兜底，不能被 header 污染
        empty, _ = await faq_tool.coroutine(question="帮我写一首关于秋天的诗")
        assert "不要编造答案" in empty, empty[:200]
        assert "禁止据此推断" not in empty
    run(main())


def test_guard_raw_and_final_come_from_one_decision():
    """P0 的关键约束：不允许存在第二套判断逻辑。

    raw 与 final 必须同源于一个 GuardDecision；否则"guard 救回/误伤"就是两个
    实现互相猜测的结果，指标本身不可信。
    """
    async def main():
        from app.core.claim_guard import GuardDecision, SentenceGuard, apply_decision, inspect_sentence
        from app.core.sensitive import get_word_set, mask

        async with AsyncSessionLocal() as db:
            await get_word_set(db)

        raw_in = "我们保证退款。本商城暂不支持货到付款。满 99 元免运费。"
        g = SentenceGuard([], mask)
        out = g.push(raw_in) + g.flush()

        assert g.raw == raw_in, "raw 必须是模型原文，不受任何处理影响"
        assert out == g.sent
        kinds = [i.kind for i in g.decision.interventions]
        assert kinds == ["masked", "hedged"], g.decision.interventions
        # 每条干预都是 before→after 的真实替换，且 apply 能复现 final
        assert apply_decision("我们保证退款。", GuardDecision(g.decision.interventions[:1])) == g.decision.interventions[0].after
        assert "保证退款" not in out and "暂不支持货到付款" not in out
        assert "满 99 元免运费。" in out, "无关句子被误伤"

        # 无干预时 raw == final，decision 为空
        g2 = SentenceGuard([], mask)
        plain = g2.push("工作日 16:00 前下单当天发出。") + g2.flush()
        assert plain == g2.raw and not g2.decision.interventions
    run(main())


def test_mask_hits_a_real_promise():
    """该干预的情况：模型自己作出承诺，必须被打码。"""
    async def main():
        from app.core.sensitive import get_word_set, mask

        async with AsyncSessionLocal() as db:
            await get_word_set(db)
        # 表里是「保证退款」，没有单独的「保证」，所以这里只有 绝对正品 该被打掉
        out = mask("我保证这是绝对正品。")
        assert "绝对正品" not in out, out
        assert "保证" in out, f"不该扩大打击面: {out}"
    run(main())


@pytest.mark.xfail(
    reason="已知 Guard false-positive（P0 登记，本轮不重构）：mask() 与判分器曾经同源的"
           "缺陷 —— 分不清提及与承诺。否定句「我不能保证…」和转述规则原文里的禁语"
           "也会被一并打码，用户看到的是被涂黑的正常免责话术。",
    strict=False,
)
def test_mask_should_not_fire_inside_negation_or_quoted_rule_text():
    async def main():
        from app.core.sensitive import get_word_set, mask

        async with AsyncSessionLocal() as db:
            await get_word_set(db)
        # 免责否定句：不该因为出现该短语就等同于模型承诺
        deny = mask("我不能保证这是绝对正品，需以人工核定为准。")
        assert "绝对正品" in deny, f"否定免责句里的短语也被打码: {deny}"
        # 转述平台规则原文：同理
        quote = mask("平台规则原文：绝对正品为品牌授权渠道货源。")
        assert "绝对正品" in quote, f"转述规则原文被打码: {quote}"
    run(main())


@pytest.mark.xfail(
    reason="已知 Guard false-positive（P1 登记，本轮不修）：宾语前置时否定词后面没有宾语，"
           "被判成'无宾语即可疑'。「新加坡暂不支持」整句里逐字带着证据「不含港澳台及海外」，"
           "却被换成兜底话术。评测里表现为 faq-14 间歇 FAIL（实测 1/6）、guard_harmed=1。"
           "修它要动 Guard 语义，与 P0 那条'本轮不重构'是同一处，一起留到下一轮。",
    strict=False,
)
def test_guard_must_not_rewrite_fronted_object_negation():
    """确定性复现，不花一次 API 调用。

    claim_guard 开头明写："证据里写了'不含港澳台及海外'，那么'不支持寄到海外'是有依据的真话，
    绝不能改写"。这句是该承诺的直接反例 —— 同一句里证据是逐字出现的，还是被改了。
    对照组证明它并不是"见否定就改"：宾语跟在后面的「不支持寄到海外」不会被改，
    而真正该拦的编造「本商城暂不支持货到付款」仍然会，所以修法只能是补前置宾语这条判据。
    """
    from app.core.claim_guard import unsupported_negation

    ev = "发货范围：仅限中国大陆，不含港澳台及海外。"
    grounded = "目前仅支持中国大陆地区配送，不含港澳台及海外，新加坡暂不支持。"
    assert not unsupported_negation(grounded, ev), "有依据的真话被判定为无依据否定"

    # 对照组：这两条的行为现在就是对的，修法不能把它们改掉
    assert not unsupported_negation("不支持寄到海外。", ev)
    assert unsupported_negation("本商城暂不支持货到付款。", ev)
    assert unsupported_negation("新加坡暂不支持。", ev)


def test_order_tools_cannot_read_another_users_order():
    """越权防线要有不花钱的测试守着。

    评测集里 idor-01 量不到这条：模型会先走 list_my_orders，答"你名下没有这单"，
    根本没调 get_order_detail —— 于是 get_owned 的 404 分支一直是无人覆盖的。
    而且评测每次烧真 API 调用，这种确定性断言就该留在单元测试里。
    """
    async def main():
        from sqlalchemy import select

        from app.models.order import Order
        from app.rag.tools import build_tools

        name = f"tx_idor_{uuid.uuid4().hex[:6]}"
        async with make_client() as c:
            await c.post("/api/v1/auth/register",
                         json={"username": name, "password": "Passw0rd!x", "phone": _phone()})
            r = await c.post("/api/v1/auth/login", json={"username": name, "password": "Passw0rd!x"})
            uid = r.json()["data"]["user"]["id"]

        async with AsyncSessionLocal() as db:
            other = await db.scalar(select(Order.id).where(Order.user_id != uid).limit(1))
        if other is None:
            pytest.skip("库里没有别人的订单，无法验证越权分支")

        detail = next(t for t in build_tools(uid) if t.name == "get_order_detail")
        content, artifact = await detail.coroutine(order_id=other)
        assert "查不到" in content, content
        assert artifact["refs"] == [], f"越权失败却仍返回了引用: {artifact}"

        guest = next(t for t in build_tools(None) if t.name == "list_my_orders")
        gtext, gart = await guest.coroutine()
        assert "未登录" in gtext, gtext
        assert gart["refs"] == []
    run(main())


def test_search_faq_tool_actually_persists_use_count():
    """统计写入必须真的落库。

    search_faqs 里那条 UPDATE 一度是死的：工具用的 session 没人提交，
    async with 退出时直接回滚。代码看着在计数，字段从上线起就没动过，
    所以必须断言"数字真的变了"，而不是断言"执行了 UPDATE"。
    """
    async def main():
        from sqlalchemy import select, text

        from app.models.custcare import Faq
        from app.rag.tools import build_tools

        async def snapshot():
            async with AsyncSessionLocal() as db:
                return {r.id: r.use_count for r in await db.scalars(select(Faq))}

        before = await snapshot()
        faq_tool = next(t for t in build_tools(None) if t.name == "search_faq")
        await faq_tool.coroutine(question="什么时候发货")
        after = await snapshot()

        bumped = {i: after[i] - before[i] for i in before if after.get(i, 0) > before[i]}
        assert bumped, f"use_count 一次都没涨（{before} -> {after}），统计又被回滚了"
        assert max(bumped.values()) == 1, bumped  # 一次查询每行只该加 1

        # 共享库不能越跑越脏，测完把这次涨的退回去
        async with AsyncSessionLocal() as db:
            for i, d in bumped.items():
                await db.execute(
                    text("UPDATE faqs SET use_count = use_count - :d WHERE id = :i"), {"d": d, "i": i}
                )
            await db.commit()
    run(main())


def test_goods_detail_tool_hides_unmarketable_but_http_still_serves_it():
    """下架商品：客服工具必须闭嘴，HTTP 详情故意照给。

    两半都要断言 —— 只测前者会把页面那个「已下架／暂不可购」分支一起废掉，
    那是刻意的店面行为（让用户看见它存在但买不了），不是漏网。
    """
    async def main():
        from sqlalchemy import text

        from conftest import first_goods_id

        gid = await first_goods_id()
        async def flip(marketable: bool):
            async with AsyncSessionLocal() as db:
                await db.execute(
                    text("UPDATE goods SET is_marketable = :m WHERE id = :i"),
                    {"m": marketable, "i": gid},
                )
                await db.commit()

        await flip(False)
        try:
            from app.rag.tools import build_tools

            detail = next(t for t in build_tools(None) if t.name == "get_goods_detail")
            content, artifact = await detail.coroutine(goods_id=gid)
            assert "已下架" in content, content
            assert artifact["refs"] == [], f"下架商品仍被当作引用推给模型: {artifact}"

            async with make_client() as c:
                r = await c.get(f"/api/v1/goods/{gid}")
                assert r.status_code == 200, r.text
                assert r.json()["data"]["is_marketable"] is False
        finally:
            await flip(True)
    run(main())


@llm
def test_agent_answers_from_faq_and_cites_source():
    async def main():
        session = {}
        try:
            async with make_client() as c:
                r = await _guest_ask(c, "你们一般多久发货？", session)
                answer, status, refs, done, err = _collect(r.text)
                assert err is None, err
                assert any("平台规则" in s for s in status), status
                assert any(k in answer for k in ("48", "当天", "16:00", "次日")), answer
                assert refs and any(x["type"] == "faq" for x in refs), refs
        finally:
            if session:
                await _drop(session["session_id"])
    run(main())


@llm
def test_agent_respects_price_filter():
    async def main():
        session = {}
        try:
            async with make_client() as c:
                r = await _guest_ask(c, "2000 块以内有降噪耳机吗？", session)
                _, _, refs, _, err = _collect(r.text)
                assert err is None, err
                goods = [x for x in (refs or []) if x["type"] == "goods"]
                assert goods and all(float(x["price"]) <= 2000 for x in goods), refs
        finally:
            if session:
                await _drop(session["session_id"])
    run(main())


@llm
def test_agent_refuses_off_domain_request():
    """提示词护栏是概率性的，所以这里跑 5 次量拒答率，而不是断言一次通过。

    一次通过不代表护栏成立 —— 这正是它和单元测试的区别。
    """
    async def main():
        session = {}
        refused = 0
        samples = []
        try:
            async with make_client() as c:
                for _ in range(5):
                    r = await _guest_ask(c, "帮我写一首关于秋天的诗", session)
                    answer, _, _, _, error = _collect(r.text)
                    assert error is None, error
                    samples.append(answer)
                    ok = len(answer) < 120 and any(
                        k in answer for k in ("帮不了", "帮不上", "不在", "只负责", "只处理", "做不了", "只能", "服务范围", "不做"))
                    refused += bool(ok)
            assert refused >= 4, f"拒答率 {refused}/5，回答样本：{samples}"
        finally:
            if session:
                await _drop(session["session_id"])
    run(main())


@llm
def test_memory_survives_across_requests():
    """记忆落在 chat_messages，不在进程内存里 —— 原项目用 InMemoryChatMemory，
    服务重启历史全丢，而且多副本之间各有一份。"""
    async def main():
        session = {}
        try:
            async with make_client() as c:
                first = await _guest_ask(c, "华为有什么手机？", session)
                assert _collect(first.text)[4] is None
                r = await _guest_ask(c, "你刚才说的第一款多少钱？", session)
                answer, _, _, _, error = _collect(r.text)
                assert error is None, error
                assert re.search(r"\d{3,}", answer), answer
            # 新连接验证历史已持久化；换 client 不等于重启服务。
            async with make_client() as c:
                from sqlalchemy import func, select
                from app.models.custcare import ChatMessage
                async with AsyncSessionLocal() as db:
                    n = await db.scalar(select(func.count()).select_from(ChatMessage).where(
                        ChatMessage.session_id == session["session_id"]))
                assert n >= 4, f"会话只存了 {n} 条"
        finally:
            if session:
                await _drop(session["session_id"])
    run(main())
