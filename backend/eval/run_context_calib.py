"""P2 上下文标定台：多轮基准 + 历史边际 token 配对测量。

为什么单独一套而不是往 61 条里加：那 61 条是 P0 冻结的回归基线，
它的价值就在"数字可比"。标定要的是可控的历史形状（字数、条数、标记深度），
两件事混在一个文件里，以后两边都改不动。

为什么种库而不是让模型自己写长答案：`history_chars` 必须是我能指定的自变量。
指望模型产出 4000 字的回答既不确定也不可复现，标定就变成测模型心情。
种进去的消息走的是 `load_history` 那条真实读路径，唯一不真实的是内容。

成本：约 55 次真模型调用（26 条各 1 次 final + 工具类多几轮 + 配对档 15 次 + 条数对照 6 次），
低于一次全量评测。
"""

import asyncio
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.database import AsyncSessionLocal  # noqa: E402

BASE = os.getenv("EVAL_BASE_URL", "http://127.0.0.1:8010")
API = BASE + "/api/v1"
SET_PATH = Path(__file__).resolve().parent / "context_calibration.jsonl"
OUT_PATH = Path(__file__).resolve().parent / "calib_results.json"

USER = os.getenv("EVAL_USER", "zhangsan_demo")
PW = os.getenv("EVAL_PASS", "Passw0rd!x")

FILLER = [
    "关于订单配送的一般性说明已经同步给你，可以继续问下一个问题。",
    "平台规则里的运费与时效条款以页面公示为准，这里不做承诺。",
    "如需进一步核对商品参数，可以告诉我具体型号与预算区间。",
    "这类问题建议保留沟通记录，方便人工客服后续跟进处理。",
    "商品详情页标注的规格与库存为准，我这边不能替商家改口径。",
    "退换货需要先由人工核定，我没法替平台下结论。",
    "你可以换个问法，比如限定价格区间或具体品牌。",
    "这个信息我没有查到依据，不能凭空给答复。",
]


def make_text(chars: int, marker: str | None, at: str) -> str:
    """定长中文填充 + 按位置嵌入标记。长度就是 history_chars 的自变量。"""
    if chars <= 0:
        return marker or ""
    buf = []
    n = 0
    while n < chars:
        seg = FILLER[len(buf) % len(FILLER)]
        buf.append(seg)
        n += len(seg)
    out = "".join(buf)[:chars]
    if not marker:
        return out
    pos = {"head": 0, "mid": max(1, len(out) // 2), "tail": max(0, len(out) - len(marker))}[at]
    return out[:pos] + marker + "。" + out[pos:]


async def seed_msgs(db, sid: str, uid: int, spec: list[dict]) -> tuple[int, int]:
    """按顺序种入，返回 (条数, 字符总数)。id 递增 = 时间正序，和 load_history 的 desc 取法对齐。"""
    total = 0
    for m in spec:
        content = make_text(int(m.get("chars", 0)), m.get("marker"), m.get("at", "head"))
        await db.execute(
            text("insert into chat_messages (session_id, user_id, role, content, created_at, updated_at) "
                 "values (:s, :u, :r, :c, now(), now())"),
            {"s": sid, "u": uid, "r": m["role"], "c": content},
        )
        total += len(content)
    await db.commit()
    return len(spec), total


FRAME_RE = re.compile(r"event: (\S+)\ndata: (.*)", re.S)


async def ask(client, body, token):
    """发一次流式请求，返回 (http, 帧列表, 首token秒数, 端到端秒数)。

    延迟必须一起量：预算不是只由 token 数决定，历史越长首 token 越慢，
    而 P95 延迟才是用户真正感受到的那个约束。
    """
    frames = []
    t0 = time.perf_counter()
    ttft = None
    async with client.stream("POST", f"{API}/chat/stream",
                             json=body, headers={"Authorization": f"Bearer {token}"},
                             timeout=300) as r:
        if r.status_code != 200:
            await r.aread()
            return r.status_code, [("http", r.json())], None, time.perf_counter() - t0
        async for chunk in r.aiter_text():
            for block in chunk.split("\n\n"):
                m = FRAME_RE.search(block)
                if not m:
                    continue
                # ttft 只认第一个 `token` 帧：归属改动之后 SSE 第一帧是 session，
                # 按"第一个 chunk 到达"量出来的延迟是 0.01s 级别的假数字（我第一版就踩了）。
                if ttft is None and m.group(1) == "token":
                    ttft = time.perf_counter() - t0
                try:
                    frames.append((m.group(1), json.loads(m.group(2))))
                except Exception:
                    pass
    return r.status_code, frames, ttft, time.perf_counter() - t0


async def new_session(uid: int) -> str:
    """直接建一行归属会话，不靠 HTTP 签发。

    两个理由：① 签发必须发一条真消息，那条会占掉 16 格窗口里的 2 格、还会成为
    "最早的一条"，把所有问"最早那个标记"的用例语义污染掉；删掉它虽然可行，但
    ② 每条用例就得多烧一次模型调用，26 条就是 26 次纯浪费。
    归属行本身不是我要测的东西（P1 的测试已经钉过签发），我要测的是读路径，
    而读路径只看 chat_messages + load_history，建出来的行形状与签发的一致。
    """
    sid = secrets.token_hex(16)
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("insert into chat_sessions (session_id, user_id, guest_secret_hash, created_by, "
                 "deleted_at) values (:s, :u, null, 'user', null)"),
            {"s": sid, "u": uid},
        )
        await db.commit()
    return sid


async def window_facts(sid: str, target: str | None, cap: int) -> dict:
    """从库里量"老窗口实际留了什么"，不靠数据文件声明。

    为什么必须实测：live（真工具轮）也会往同一个会话写 user+assistant 两条，
    于是种入时声明的深度在 final 请求时已经往前挪了。只有按 id 从最新往回数量，
    才知道目标到底被不被 16 条窗口看见。kept_chars 还应当等于 obs.history_chars，
    这两个数对不上就说明我对读路径的理解是错的。
    """
    async with AsyncSessionLocal() as db:
        res = await db.execute(text(
            "select id, role, content, length(content) from chat_messages "
            "where session_id = :s order by id desc"
        ), {"s": sid})
        rows = res.all()
    total = len(rows)
    kept = rows[:cap]
    dropped = rows[cap:]
    depth = None
    if target:
        hit = [i for i, r in enumerate(rows) if target in (r[2] or "")]
        depth = hit[0] + 1 if hit else None  # 从最新往回数第几条（1 = 最近一条）
    return {
        "stored_msgs": total,
        "stored_chars": sum(r[3] for r in rows),
        "kept_msgs": len(kept),
        "kept_chars": sum(r[3] for r in kept),
        "dropped_msgs": len(dropped),
        "dropped_chars": sum(r[3] for r in dropped),
        "target_depth_from_newest": depth,
        "target_in_window": None if depth is None else depth <= cap,
    }


async def run_case(client, token, uid, case, keep, rep=0):
    """种库 -> （可选）真工具轮 -> final 轮。返回这一条的实测记录。"""
    sid = await new_session(uid)
    keep.append(sid)
    async with AsyncSessionLocal() as db:
        n_seed, seed_chars = await seed_msgs(db, sid, uid, case.get("seed") or [])

    live_tools = 0
    live_http = []
    for item in case.get("live") or []:
        # live 在数据文件里是 [{"q": "..."}]，不是裸字符串 —— 早先直接把 dict 当 message 发，
        # 服务端报 "message Input should be a valid string"，整个 tool_reference 类别静默废掉。
        q = item["q"] if isinstance(item, dict) else item
        st, frames, _, _ = await ask(client, {"message": q, "session_id": sid}, token)
        # 非 200 时把响应体也留下：400/410 长得不一样，只记状态码就永远查不出是谁的锅
        body = frames[0][1] if st != 200 and frames else None
        live_http.append((st, body))
        live_tools += sum(1 for n, _ in frames if n == "status")

    win = await window_facts(sid, case.get("target"), CAP_MESSAGES)

    final = case["final"]
    st, frames, ttft, wall = await ask(client, {"message": final["q"], "session_id": sid}, token)
    sess = next((d for n, d in frames if n == "session"), {})
    done = next((d for n, d in frames if n == "done"), {})
    obs = done.get("obs") or {}
    answer = "".join(d for n, d in frames if n == "token")

    any_of = final.get("any_of") or []
    none_of = final.get("none_of") or []
    hit = [p for p in any_of if p in answer]
    bad = [p for p in none_of if p in answer]
    correct = bool(hit) and not bad
    return {
        "id": case["id"], "cat": case["cat"], "sid": sid, "rep": rep,
        "seed_msgs": n_seed, "seed_chars": seed_chars,
        "live_tool_calls": live_tools,
        "live_http": live_http,
        "http": st, "owner_ok": sess.get("owner_ok"), "reason": sess.get("reason"),
        "history_chars": obs.get("history_chars"),
        "history_messages": obs.get("history_messages"),
        "stop_reason": obs.get("stop_reason"),
        "over_budget": obs.get("history_over_budget"),
        "current_user_chars": obs.get("current_user_chars"),
        "message_chars": obs.get("message_chars"),
        "in_tokens": obs.get("in_tokens"), "out_tokens": obs.get("out_tokens"),
        "ttft": round(ttft, 3) if ttft else None, "wall": round(wall, 3) if wall else None,
        "tools_status_events": sum(1 for n, _ in frames if n == "status"),
        "answer_len": len(answer), "correct": correct,
        "hit_any": hit, "hit_none": bad,
        "expect_tools": case.get("expect_tools"),
        "record_only": case.get("record_only"),
        "declared_depth": case.get("target_depth"),
        **win,
        "answer_head": answer[:120],
    }


# 窗口宽度从配置读，不写死任何数字。
# P2 之前它是 chat_history_turns*2 = 16 条；现在是"字符预算为主 + max_messages 当保险丝"，
# 16 这个数只存在于 calib_old_16msg.json 那份历史基线里。写死会让"被截掉多少条"的
# 结论随配置漂移而悄悄失真 —— 而那正是这套台子的主要产出。
from app.core.config import get_settings  # noqa: E402

CAP_MESSAGES = get_settings().chat_history_max_messages
CHAR_BUDGET = get_settings().chat_history_char_budget

LEVELS = [0, 1000, 3000, 6000, 10000, 20000, 40000]
FINAL_Q = "第一条消息里的校验码是什么？只回答校验码本身。"


async def paired(client, token, uid, keep, reps=3):
    """配对测量：固定 16 条历史，只变总字符数。

    为什么固定条数：Δtokens/Δchars 要在"其他都不变"下才算得出边际。
    条数本身也要 token（每条的角色/分隔/结构），所以另测一组同字数不同条数当对照。
    """
    rows = []
    for level in LEVELS:
        for rep in range(reps):
            sid = await new_session(uid)
            keep.append(sid)
            spec = []
            if level:
                n = 16
                per = max(1, round(level / n))
                for i in range(n):
                    role = "user" if i % 2 == 0 else "assistant"
                    spec.append({"role": role, "chars": per,
                                 "marker": f"M{i}" if i == 0 else None, "at": "head"})
            async with AsyncSessionLocal() as db:
                n_seed, seed_chars = await seed_msgs(db, sid, uid, spec)
            st, frames, ttft, wall = await ask(client, {"message": FINAL_Q, "session_id": sid}, token)
            obs = (next((d for n, d in frames if n == "done"), {}) or {}).get("obs") or {}
            rows.append({"level": level, "rep": rep, "sid": sid, "http": st,
                         "seed_msgs": n_seed, "seed_chars": seed_chars,
                         "history_chars": obs.get("history_chars"),
                         "in_tokens": obs.get("in_tokens"),
                         "out_tokens": obs.get("out_tokens"),
                         "message_chars": obs.get("message_chars"),
                         "ttft": round(ttft, 3) if ttft else None,
                         "wall": round(wall, 3) if wall else None})

    # 条数对照：同样 3000 字，一条 4 段 vs 16 段，差值就是"每条消息的固定开销"
    ctrl = []
    for n in (2, 4, 8, 16):
        sid = await new_session(uid)
        keep.append(sid)
        per = round(3000 / n)
        spec = [{"role": "user" if i % 2 == 0 else "assistant", "chars": per,
                 "marker": f"C{i}" if i == 0 else None, "at": "head"} for i in range(n)]
        async with AsyncSessionLocal() as db:
            n_seed, seed_chars = await seed_msgs(db, sid, uid, spec)
        st, frames, ttft, wall = await ask(client, {"message": FINAL_Q, "session_id": sid}, token)
        obs = (next((d for n2, d in frames if n2 == "done"), {}) or {}).get("obs") or {}
        ctrl.append({"msgs": n_seed, "seed_chars": seed_chars,
                     "history_chars": obs.get("history_chars"),
                     "in_tokens": obs.get("in_tokens"), "http": st, "sid": sid,
                     "ttft": round(ttft, 3) if ttft else None,
                     "wall": round(wall, 3) if wall else None})
    return rows, ctrl


async def main():
    cases = [json.loads(l) for l in SET_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    only = os.getenv("CALIB_ONLY")
    if only:
        cases = [x for x in cases if only in x["id"] or only == x["cat"]]
    limit = int(os.getenv("CALIB_LIMIT", "0"))
    if limit:
        cases = cases[:limit]
    skip_paired = os.getenv("CALIB_SKIP_PAIRED") == "1"
    skip_cases = os.getenv("CALIB_SKIP_CASES") == "1"
    reps = int(os.getenv("CALIB_REPS", "3"))
    keep: list[str] = []
    prev: dict = {}
    if skip_cases and OUT_PATH.exists():
        # 只补测配对档时，把已有的用例结果原样留在文件里（同一份产品代码、几分钟内测的），
        # 不然第 2/3/7 节的数字会被清空。provenance 记在 meta 里。
        prev = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    out = {"cases": prev.get("cases", []), "paired": [], "count_control": [],
           "meta": {**prev.get("meta", {}), "cases": len(cases), "reps": reps,
                    "cap_messages": CAP_MESSAGES,
                    "char_budget": get_settings().chat_history_char_budget,
                    "max_messages": get_settings().chat_history_max_messages,
                    "cases_from_run": None if skip_cases else "this_run",
                    "paired_from_run": "this_run"}}
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{API}/auth/login", json={"username": USER, "password": PW})
        if r.status_code != 200:
            print(f"登录失败 http={r.status_code}：这套台子必须在强制模式下有主会话", flush=True)
            return 2
        token = r.json()["data"]["access_token"]
        async with AsyncSessionLocal() as db:
            uid = await db.scalar(text("select id from users where username=:u"), {"u": USER})

        # 每条用例跑 reps 次：同一份代码、同一份输入、temperature=0，上一轮两次全量
        # 有 6/23 条结果翻转。单次跑出来的对/错不具备"回归判据"资格，必须先量噪声。
        for i, case in enumerate(cases, 1):
            if skip_cases:
                break
            runs = []
            for rep in range(reps):
                runs.append(await run_case(c, token, uid, case, keep, rep=rep))
            out["cases"].extend(runs)
            ok = sum(1 for x in runs if x["correct"])
            first = runs[0]
            print(f"[{i}/{len(cases)}] {case['id']:<18} stored={first['stored_msgs']:<3} "
                  f"kept={first['kept_msgs']:<3} drop={first['dropped_msgs']} "
                  f"depth={first['target_depth_from_newest']} inWin={first['target_in_window']} "
                  f"hist={first['history_chars']} in={first['in_tokens']} 正确 {ok}/{reps}", flush=True)

        print(f"=== 配对测量（{LEVELS} × {reps} 次） ===", flush=True)
        if skip_paired:
            print("  已跳过（CALIB_SKIP_PAIRED=1）", flush=True)
        else:
            paired_rows, ctrl = await paired(c, token, uid, keep, reps=reps)
            out["paired"], out["count_control"] = paired_rows, ctrl
            for row in paired_rows:
                print(f"  level={row['level']:<6} rep={row['rep']} hist={row['history_chars']} "
                      f"in={row['in_tokens']} out={row['out_tokens']} ttft={row['ttft']}", flush=True)
            print("=== 条数对照（同为 ~3000 字） ===", flush=True)
            for row in ctrl:
                print(f"  msgs={row['msgs']:<3} hist={row['history_chars']} in={row['in_tokens']}", flush=True)

    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from chat_messages where session_id = any(:s)"), {"s": keep})
        await db.execute(text("delete from chat_sessions where session_id = any(:s)"), {"s": keep})
        await db.commit()
    out["meta"]["cleaned_sessions"] = len(keep)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"WROTE {OUT_PATH.name}", flush=True)
    return 0


sys.exit(asyncio.run(main()))
