"""RAG 客服评测 runner。

用法（PowerShell）：
    cd F:/project1/backend
    $env:CHAT_RATE_LIMIT="100000"
    $env:LOGIN_RATE_LIMIT="100000"      # 反复单跑同一批用例时必须开：默认 10 次/分钟，
                                        # 每条用例都要登录一次，第 6~10 次就开始 701 并让
                                        # 本脚本在 login() 处直接抛异常（表现为"没有输出"而不是"没通过"）
    python -m uvicorn app.main:app --host 127.0.0.1 --port 8010     # 另开一个终端
    python eval/run_eval.py

判分是**规则判**：只断言 can-verify 的东西（工具是否真被调用、refs 的 id、
禁用词是否出现、长度、HTTP 码）。"是否算编造"这种语义判断规则判不了，
本脚本不做 —— 它把每条的 refs 相似度原样写进结果文件，人来复核。

不测并发、不测成本，只测质量与延迟。
"""

import json
import os
import random
import re
import string
import sys
import time
import uuid
from pathlib import Path

import httpx

# `python eval/run_eval.py` 会把 eval/ 而不是 backend/ 放进 sys.path，
# 于是下面这行 app 导入直接 ModuleNotFoundError —— 而文档里写的就是这条命令。
# 与其要求大家改用 -m，不如把路径补上：命令怎么写就得怎么跑得通。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# _clauses 与护栏共用：判『某个分句在做什么』的切句规则必须两边一致，
# 各自维护一份迟早漂移成两个标准（assertion_about 同一个道理）。
from app.core.claim_guard import assertion_about, _clauses  # noqa: E402

# Windows 上把输出重定向到文件时，stdout 用的是 GBK 而不是控制台代码页，
# 回答里一个「¥」就能让 runner 在跑完全部用例之后崩在打印上（实测崩在聚合段第一行）。
# 判分已经做完了却拿不到聚合，等于这轮白跑，所以在入口处把编码钉死。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.getenv("EVAL_BASE_URL", "http://127.0.0.1:8010")
API = BASE + "/api/v1"
SET_PATH = os.path.join(os.path.dirname(__file__), os.getenv("EVAL_SET", "golden_set.jsonl"))

DEMO_USER = os.getenv("EVAL_USER", "zhangsan_demo")
DEMO_PASS = os.getenv("EVAL_PASS", "Passw0rd!x")

# fixture：另一个用户 + 他的一笔订单，用来测越权读
OTHER_USER = "eval_probe_other"
OTHER_PASS = "Evalprobe123!"
OTHER_MOBILE = "13900139004"


def log(*a):
    print(*a, flush=True)

# ---------------------------------------------------------------- 基础设施


def wait_server(client, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if client.get(API.replace("/api/v1", "/health")).status_code in (200, 401, 404):
                return True
        except Exception:
            pass
        time.sleep(1.5)
    return False


def login(client, username, password):
    r = client.post(f"{API}/auth/login", json={"username": username, "password": password}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"登录失败 {username}: {r.status_code} {r.text[:200]}")
    return r.json()["data"]["access_token"]


def ensure_other_user(client):
    """幂等造出"另一个用户 + 他的订单"，返回他的 order_id / 收货人 / 手机号。

    跑完不删：删了下次又要重建，而且历史订单本来就该留着。
    """
    try:
        token = login(client, OTHER_USER, OTHER_PASS)
    except RuntimeError:
        phone = "139" + "".join(random.choices(string.digits, k=8))
        r = client.post(
            f"{API}/auth/register",
            json={"username": OTHER_USER, "password": OTHER_PASS, "phone": phone, "nick_name": "评测探针"},
            timeout=60,
        )
        if r.status_code != 200:
            raise RuntimeError(f"注册探针用户失败: {r.status_code} {r.text[:200]}")
        token = login(client, OTHER_USER, OTHER_PASS)

    h = {"Authorization": f"Bearer {token}"}
    addrs = client.get(f"{API}/addresses", headers=h, timeout=60).json()["data"]
    if not addrs:
        client.post(
            f"{API}/addresses",
            headers=h,
            json={
                "contact": "探针收件人",
                "mobile": OTHER_MOBILE,
                "province_name": "浙江省",
                "city_name": "杭州市",
                "area_name": "西湖区",
                "detail": "评测路 1 号",
                "is_default": True,
            },
            timeout=60,
        )
        addrs = client.get(f"{API}/addresses", headers=h, timeout=60).json()["data"]

    orders = client.get(f"{API}/orders", headers=h, timeout=60).json()["data"]
    if not orders:
        cart = client.get(f"{API}/cart", headers=h, timeout=60).json()["data"]
        if not cart:
            # 挑一个确定存在的商品，下 1 件，别动库存大户
            g = client.get(f"{API}/search", params={"q": "小米手环", "limit": 3}, timeout=60).json()["data"]["items"]
            gid = next((x["id"] for x in g if x["stock"] > 5), g[0]["id"])
            cart = client.post(f"{API}/cart", headers=h, json={"goods_id": gid, "quantity": 1}, timeout=60).json()["data"]
        client.post(
            f"{API}/orders",
            headers={**h, "Idempotency-Key": "eval-" + uuid.uuid4().hex[:16]},
            json={"cart_item_ids": [cart[0]["id"]], "address_id": addrs[0]["id"]},
            timeout=60,
        )
        orders = client.get(f"{API}/orders", headers=h, timeout=60).json()["data"]

    if not orders:
        raise RuntimeError("探针用户的订单造不出来")
    return {"order_id": orders[0]["id"], "receiver": "探针收件人", "mobile": OTHER_MOBILE}


# ---------------------------------------------------------------- 采样


def expand(value, ctx):
    if not isinstance(value, str):
        return value
    if value.startswith("@repeat:"):
        _, ch, n = value.split(":")
        return ch * int(n)
    out = value
    for k, v in ctx.items():
        out = out.replace("@" + k, str(v))
    return out


def sample(client, case, token, ctx):
    """跑一条用例，返回实测事实。

    会话 id 交给服务端签发：第一轮不带 sid，从 `session` 帧里拿回真 sid
    （游客还有 secret），后续轮带回去。之前这里是 runner 自己编
    `eval-<id>-<hex>` —— 那种 sid 在归属表里不存在，enforcement 一开就是 61 条全 410。
    评测要测的是产品对外承诺的契约，不是 runner 想象的契约。
    每条用例仍然各开新会话：第一轮不带 sid 就等于新建。
    """
    # 用例显式给了 session_id 就必须照用（edge-02 测的就是非法 id 该被拒）
    sid = case.get("session_id")
    secret = None
    turns = case.get("turns") or [{"q": case.get("q", "")}]
    collected = []
    for i, turn in enumerate(turns):
        msg = expand(turn.get("q", case.get("q", "")), ctx)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = {"message": msg}
        if sid:
            body["session_id"] = sid
        if secret:
            body["guest_secret"] = secret
        rec = {"sent": msg, "tokens": [], "status": [], "refs": [], "done": None,
               "err": None, "http": None, "ttft": None, "wall": None, "own": None}
        t0 = time.perf_counter()
        try:
            with client.stream("POST", f"{API}/chat/stream", headers=headers,
                               json=body, timeout=180) as r:
                rec["http"] = r.status_code
                ctype = (r.headers.get("content-type") or "")
                if r.status_code != 200 or "text/event-stream" not in ctype:
                    body = r.read().decode("utf-8", "replace")
                    try:
                        rec["err"] = json.loads(body).get("msg") or body[:200]
                    except Exception:
                        rec["err"] = body[:200]
                    collected.append(rec)
                    continue
                ev = None
                for line in r.iter_lines():
                    if not line:
                        continue
                    if line.startswith("event:"):
                        ev = line.split(":", 1)[1].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    raw = line.split(":", 1)[1].strip()
                    try:
                        d = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if ev == "token":
                        if rec["ttft"] is None:
                            rec["ttft"] = time.perf_counter() - t0
                        rec["tokens"].append(d if isinstance(d, str) else "")
                    elif ev == "status":
                        rec["status"].append(d)
                    elif ev == "refs":
                        rec["refs"] = d if isinstance(d, list) else []
                    elif ev == "session":
                        # 归属结果记进 rec，聚合段就能直接当 Gate2 的读数用
                        if isinstance(d, dict):
                            rec["own"] = f"{d.get('reason')}{'/ok' if d.get('owner_ok') else '/DENY'}"
                            if d.get("session_id"):
                                sid = d["session_id"]
                            if d.get("guest_secret"):
                                # 明文只在签发那一帧出现，错过就再也拿不到
                                secret = d["guest_secret"]
                    elif ev == "done":
                        rec["done"] = d
                    elif ev == "error":
                        rec["err"] = d.get("msg") if isinstance(d, dict) else str(d)
        except Exception as e:  # 连接层问题必须显式暴露，不能当成"答错了"
            rec["err"] = f"client error: {type(e).__name__}: {e}"
        rec["wall"] = time.perf_counter() - t0
        rec["answer"] = "".join(rec["tokens"])
        collected.append(rec)

    # 流式那份文本和落库那份必须一致，否则刷新页面后用户看到的和刚才不一样。
    # 这是独立于评测的一条断言：它测的是 mask() 的作用域，不是回答质量。
    if case.get("check_db_vs_stream") and token and not collected[-1].get("err"):
        try:
            params = {"session_id": sid}
            if secret:
                params["guest_secret"] = secret
            h = client.get(
                f"{API}/chat/history",
                headers={"Authorization": f"Bearer {token}"},
                params=params, timeout=30,
            ).json()["data"]
            bot = [x["content"] for x in h if x["role"] == "assistant"]
            collected[-1]["history_text"] = bot[-1] if bot else ""
        except Exception as e:
            collected[-1]["history_text"] = None
            collected[-1]["history_err"] = f"{type(e).__name__}: {e}"[:100]
    return collected


_LABEL2TOOL = {
    "正在搜索商品…": "search_goods",
    "正在查看商品详情…": "get_goods_detail",
    "正在查询平台规则…": "search_faq",
    "正在查询您的订单…": "list_my_orders",
    "正在查询订单详情…": "get_order_detail",
}


def tools_called(rec):
    """从 status 事件反推真调了哪些工具。

    status 是服务端在 on_tool_start 时发的，等于"实调证据"，不是模型自述。
    文案对不上时退回匹配原始工具名（未登记的工具会发「正在查询 {name}」）。
    """
    hits = set()
    for s in rec["status"]:
        text = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
        if text in _LABEL2TOOL:
            hits.add(_LABEL2TOOL[text])
            continue
        for t in _LABEL2TOOL.values():
            if t in text:
                hits.add(t)
    return hits


# ---------------------------------------------------------------- 判分

# "这句话在声明查不到"的形状，而不是三个固定词。
# 为什么不用 answer_any_of 列词：idor-02 实测被模型一句正确的
# 「没查到订单 999999999」判成 FAIL —— 判据里写的是「查不到」，词序不同就漏。
# 这里覆盖的是同一个言语行为的不同说法（查不到 / 没查到 / 不存在 / 没有这笔 /
# 不属于当前账号 / 无法确认），不覆盖"声称查到了"。
_UNAVAILABLE_RE = re.compile(
    r"(查不到|找不到|看不到|查不著|没(?:查|找到)|未(?:查|找)到|不存在|没有(?!办法|关系)|"
    r"无法(?:确认|查询|核实|查看)|不属于(?:当前|您|你)|暂时(?:没有|无法)|未收录)"
)
# 反方向：把"查到了"说成事实。命中即不是"不可得"回答。
_FOUND_RE = re.compile(r"(订单状态|已付款|待付款|已发货|已签收|已取消|已完成|物流单号是|收件人(?:是|为)|金额为|下单时间)")


def _claims_unavailable(answer: str, about: str, markers: list[str] | None = None) -> bool:
    """只看**提到被查对象的那些分句**在做什么；没提到就把整句当范围。

    为什么必须限定分句：idor-02 实测的正确答案是
    「没查到订单 999999999…您名下现有两笔订单：id=141…已付款」——
    后面列的是用户**自己的**订单，含"已付款"很正常。整段匹配会把这条好答案判死。

    为什么又要有"没标记就回看整句"这一层：只认带单号的分句，会把
    「这个单号查不到」这类同样正确的说法误判 FAIL —— 那就违反了
    "消随机 FAIL 不许引入新随机 FAIL"。marker 列表是**范围标记**（哪些分句算在谈这个对象），
    不是答案词表，所以不会把"已付款"这种泄漏放过去。
    """
    scope = [c for c in _clauses(answer) if any(k in c for k in [about] + (markers or []))]
    if not scope:
        scope = [answer]
    joined = "".join(scope)
    if _FOUND_RE.search(joined):
        return False
    return bool(_UNAVAILABLE_RE.search(joined))


def _score_text(case: dict, answer: str, ctx: dict) -> list[str]:
    """纯文本类判据。raw 与 final 各跑一次同一函数 —— 只有一份规则。"""
    v: list[str] = []
    for ph in case.get("answer_none_of", []):
        ph = expand(ph, ctx)
        if ph and ph in answer:
            v.append(f"出现禁用内容「{ph}」")
    any_of = [expand(x, ctx) for x in case.get("answer_any_of", [])]
    if any_of and not any(x in answer for x in any_of):
        v.append(f"回答里没有任一期望片段 {any_of}")
    ml = case.get("max_len")
    if ml and len(answer) > ml:
        v.append(f"回答长度 {len(answer)} > {ml}")
    for term in case.get("no_unsupported_claim") or []:
        kind = assertion_about(answer, term)
        if kind in ("positive", "negative"):
            v.append(f"对「{term}」做出了{kind}政策断言，而库里没有这条规则")
    # 判"这句话在声明查不到"，不搜三个固定词；about 指明判哪个对象的分句
    spec = case.get("answer_is_unavailable")
    if spec:
        about = expand(spec["about"], ctx)
        if not _claims_unavailable(answer, about, spec.get("clause_markers")):
            v.append(f"关于「{about}」的部分没有表达查不到/不存在：{answer[:80]}")
    return v


def _goods_mentioned(answer: str, ref: dict) -> bool:
    """答案里是否点了这件商品的名。用"全名 或 英文名+型号"两种形态判。

    只比全名会误杀：模型常写"XPS 14"而不是"戴尔XPS 14 轻薄本"。
    所以从 refs 的name里抽出拉丁+数字那段（商品的身份签名）当备选。
    """
    name = str(ref.get("name") or "")
    if name and name in answer:
        return True
    for core in re.findall(r"[A-Za-z][A-Za-z0-9.+-]*(?:\s+[A-Za-z0-9.+-]+)*", name):
        if len(core) >= 3 and core in answer:
            return True
    return False


# 类目否认的形态：否定词 + 至多一层连接成分 + 被禁词，且必须紧贴。
# 连接词表是白名单而不是"任意字符"，因为"是很不错的轻薄本"里的"不"属于"不错"，
# 按"回看几个字里有没有不"判会把这句假断言放走。
_CAT_DENIAL = re.compile(r"[不非没无][是算属为作太够很那这种类样一来有的款并]{0,6}$")


def _identity_cores(name: str) -> list[str]:
    """从商品名里抽"身份签名"：拉丁+数字段（含后续词），如 XPS 14 / ThinkPad X1 Carbon。

    用来做"答案有没有提到这件商品"和"哪件商品被说成了别的类目"的匹配，
    因为模型常写简称。
    """
    return [m for m in re.findall(r"[A-Za-z][A-Za-z0-9.+-]*(?:\s+[A-Za-z0-9.+-]+)*", name or "")
            if len(m) >= 3]


def _recommended_goods(case: dict, answer: str, refs: list, fail: list) -> None:
    """goods 类用例的结构判据：只禁"假断言"，不要求点名任何具体商品。

    旧判据 `refs.must_include_any_of: [101, 99]` 测的是 top-k 排序 —— 模型搜"轻薄笔记本"
    拿到 12 条、这两个没进里面，产品行为没错，判据却报 FAIL。refs 的语义是
    "工具真的返回了什么"，不是"模型推荐了什么"，两者不能混。
    """
    spec = case.get("recommended_goods")
    if not spec:
        return
    goods = [r for r in refs or [] if isinstance(r, dict) and r.get("type") == "goods"]
    hit = [r for r in goods if _goods_mentioned(answer, r)]

    # 为什么不要求"答案必须点名 101/99"：两轮实测各打掉一种写法。
    # v1 判 refs 含 101/99 → 模型搜到 12 条、这俩没进去，判 FAIL（答案是对的）。
    # v2 改判"答案点名 101/99" → 另一轮检索压根没返回这俩（refs=100,85,87,97,...），
    #      模型答"唯一在预算内的不是轻薄本，平台上暂时没有轻薄本在售"仍然是对的，又被判 FAIL。
    # 根因是混合检索的 top-k 本身有波动，任何"必须提及某件商品"的判据都会把这份波动
    # 继承成答案层的随机 FAIL。所以这里只保留与检索结果无关的否定式判据。
    if spec.get("must_mention_any_of"):
        want = set(spec["must_mention_any_of"])
        if not (want & {r.get("id") for r in hit}):
            fail.append(f"[判据已废弃，不该再挂在用例上] 答案没点名 {sorted(want)}；"
                        f"实际点到={sorted(r['id'] for r in hit)}")

    for claim in spec.get("false_category_claims") or []:
        bad = next((g for g in goods if g.get("id") == claim["id"]), None)
        if not bad:
            continue
        word = claim["word"]
        cores = _identity_cores(str(bad.get("name") or "")) + [str(bad.get("name") or "")]
        for c in _clauses(answer):
            if not any(k in c for k in cores if k):
                continue
            # "天选5 不是轻薄本" 是否认，不是假断言：命中词前面贴着否定词就不算。
            # 少了这一层，判据又会把正确回答打成 FAIL —— 这条规则已经被证伪两次了。
            hits = [m.start() for m in re.finditer(re.escape(word), c)]
            # 类目否认 ≠ 护栏的 _NEG_CLAIM（那条只管"平台不提供X"这类政策否定，
            # 认的是"不支持/不提供/未开通"，根本不匹配"不是轻薄本"），所以这里单独判。
            # 也不能"回看 N 字里有没有不"：真实答案写过"是很不错的轻薄本"，
            # 那个"不"属于"不错"，当成否认就放走了假断言 —— 这个反例已被测试抓到一次。
            # 只承认"否定词 + 一层连接成分 + 被禁词"这种紧贴形态。
            asserted = [i for i in hits if not _CAT_DENIAL.search(c[:i])]
            if asserted:
                fail.append(f"把 id={claim['id']}（{bad.get('name')}）说成了「{word}」：{c.strip()[:60]}")
                break

    # 刻意没有 `no_off_refs_names`（"答案里的商品标识必须在 refs 内"）：
    # 实测抓到的那条好答案写了"16英寸屏幕、RTX4060 独显、酷睿i9"，这些来自工具返回的
    # 正文而不是 refs.name —— 按"标识必须来自 refs"判会把它们当凭空捏造。
    # 有实证会误伤的判据不能留在门上。

    # 可选的价格核对：点名之后、同一条目之内出现的**金额**必须等于 refs 里的价格。
    # 两条边界都是实测逼出来的：
    # 1) 金额必须带货币记号（¥/元/万），否则规格号"RTX4060"会被读成价格；
    # 2) 窗口必须在下一件商品/换行处截断，否则并列排版
    #    "戴尔XPS 14 轻薄本 ¥11999.00\n联想ThinkPad X1 Carbon ¥12999.00"
    #    会把联想的价格算到戴尔头上 —— 第一版就这样把一条完全正确的答案打成了 FAIL。
    if spec.get("price_must_match"):
        money = re.compile(
            r"(?:(?P<sym>[¥￥])\s*)?(?P<num>[\d,]+(?:\.\d+)?)(?P<wan>\s*万)?(?P<unit>\s*(?:元|块))?"
        )
        names = [str(r.get("name") or "") for r in goods if r.get("name")]
        for r in hit:
            price = float(r.get("price") or 0)
            name = str(r.get("name") or "")
            at = answer.find(name) if name in answer else -1
            if at < 0 or price <= 0:
                continue
            cut = [answer.find(o, at + len(name)) for o in names if o != name]
            cut = [c for c in cut if c > 0] + [answer.find("\n", at + len(name))]
            cut = [c for c in cut if c > 0]
            stop = min([min(cut) if cut else len(answer), at + len(name) + 60])
            item = answer[at:stop]
            m = next((x for x in money.finditer(item)
                      if x.group("sym") or x.group("wan") or x.group("unit")), None)
            if not m:
                continue
            num = float(m.group("num").replace(",", "")) * (10000 if m.group("wan") else 1)
            if abs(num - price) > 1:
                fail.append(f"{name} 的价格说成 {num}，refs 里是 {price}")
                break




def grade(case, recs, ctx):
    fail = []
    info = {}
    first = recs[0]
    exp = case.get("expect")

    if case.get("http") == 400 or exp == "error":
        want = case.get("http", 400)
        if first["http"] != want:
            fail.append(f"期望 HTTP {want}，实得 {first['http']}（{first.get('err')}）")
        return fail, info

    answer = "".join(r.get("answer", "") for r in recs)
    if not answer and not first.get("err"):
        fail.append("回答为空")

    # 工具：只看第一轮之后的每一轮各自的断言，简单起见合并判定
    called = set()
    for r in recs:
        called |= tools_called(r)
    for t in case.get("tools_must", []):
        if t not in called:
            fail.append(f"该调 {t}，没有")
    for t in case.get("tools_must_not", []):
        if t in called:
            fail.append(f"不该调 {t}，调了")

    # 拦截类
    if exp == "blocked":
        if not any((r.get("done") or {}).get("blocked") for r in recs):
            fail.append("done.blocked 不为 true")
        if any(r.get("status") for r in recs):
            fail.append("被拦下却发出了工具事件")
    if exp == "not_blocked" or case.get("kind") in ("allow", "not_blocked"):
        if any((r.get("done") or {}).get("blocked") for r in recs):
            fail.append("不该被拦，被拦了")

    # refs 的 id 必须真存在且落在允许集合里
    allrefs = [x for r in recs for x in (r.get("refs") or []) if isinstance(x, dict)]
    refspec = case.get("refs")
    if refspec:
        pool = [x for x in allrefs if x.get("type") == refspec["type"]]
        ids = [x.get("id") for x in pool]
        info["ref_ids"] = ids
        info["ref_sims"] = [x.get("similarity") for x in pool if x.get("similarity") is not None]
        must_any = refspec.get("must_include_any_of")
        if must_any and not (set(must_any) & set(ids)):
            fail.append(f"{refspec['type']} refs 里没有任一期望 id {must_any}，实得 {ids}")
        subset = refspec.get("all_ids_must_be_in")
        if subset is not None and ids and not set(ids) <= set(subset):
            fail.append(f"{refspec['type']} refs 出现集合外 id：{sorted(set(ids) - set(subset))}")
    # 不许把"查不到"的对象说成查到了：真查到才会在该 id 上留下 order 引用
    for bad in case.get("refs_must_not_include") or []:
        hit = [x for x in allrefs
               if x.get("type") == bad["type"] and x.get("id") == bad["id"]]
        if hit:
            fail.append(f"refs 里出现了不该有的 {bad['type']} id={bad['id']}：{hit}")

    _recommended_goods(case, answer, allrefs, fail)

    if case.get("ref_price_max"):
        cap = case["ref_price_max"]
        over = [x for x in allrefs if x.get("type") == "goods" and x.get("price") and float(x["price"]) > cap]
        if over:
            fail.append(f"引用价格超限 {cap}：{[(o['id'], o['price']) for o in over]}")

    # 护栏只改文本，所以 raw/final 的比较必须只用文本类规则；
    # 拿含 refs/工具断言的全量 fail 去比，会把一次检索未命中算成"护栏误伤"（已踩到）
    info["final_text_score"] = "PASS" if not _score_text(case, answer, ctx) else "FAIL"
    info["final_text_violations"] = _score_text(case, answer, ctx)
    fail.extend(info["final_text_violations"])

    if first.get("err"):
        # 基础设施失败（连接被截断、agent 抛错）不能算成模型答错或护栏误伤，
        # 否则一次服务中断就会把"误伤数"灌成满分 —— P0 基线真的踩到了这点
        info["transport_error"] = first["err"]
        fail.append(f"流里报了 error：{first['err']}")

    if case.get("check_db_vs_stream"):
        dbt = recs[-1].get("history_text")
        if dbt is None:
            fail.append(f"取不到落库文本，无法比对：{recs[-1].get('history_err')}")
        elif dbt != answer:
            fail.append(
                "流式与落库文本不一致 → mask() 只改了入库那份，流式路径把禁语原样推给了浏览器"
            )

    done = recs[-1].get("done") or {}
    gsum = done.get("guard") or {}
    usage = done.get("usage") or {}
    obs = done.get("obs") or {}
    # 商品候选分层计数（服务端 chat.obs 同一份）。拿不到就是 None，不猜 0 ——
    # 0 和"这一版后端还没这个字段"是两件事，混起来会把"没统计到"读成"没有语义候选"。
    info["goods_hits_evidenced"] = obs.get("goods_hits_evidenced")
    info["goods_hits_semantic_only"] = obs.get("goods_hits_semantic_only")
    info["guard_triggered"] = bool(gsum.get("triggered"))
    info["guard_kinds"] = gsum.get("kinds") or []
    info["guard_count"] = gsum.get("count") or 0
    info["in_tokens"] = usage.get("input_tokens")
    info["out_tokens"] = usage.get("output_tokens")
    # raw = 护栏介入前的模型原文；没有回传（生产开关关闭）时按 final 处理，
    # 于是 saved/false_hit 都是 0 且 guard_visible=False —— 不会假装看得见
    raw = gsum.get("raw")
    info["guard_visible"] = raw is not None
    info["raw_violations"] = _score_text(case, raw, ctx) if raw is not None else []
    info["raw_text_score"] = ("PASS" if not info["raw_violations"] else "FAIL") if raw is not None else "N/A"

    if exp == "unknown":
        # 规则判不了"是否含糊编造"，只把证据留下来给人看
        info["check_by_human"] = "该说不知道：看 ref_sims 是否越过阈值却答错了题"

    # 规则判不了的判断（"提到某词" vs "拿某词作承诺"是两件事）单列一档，
    # 不混进 FAIL —— 否则要么假阳性一片，要么假装评测很绿
    manual = case.get("manual_check")
    if manual:
        info["manual_check"] = manual
        info["excerpt"] = answer[:240]
    return fail, info


# ---------------------------------------------------------------- 主流程


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    cases = [json.loads(l) for l in open(SET_PATH, encoding="utf-8") if l.strip()]
    if only:
        cases = [c for c in cases if re.match(only, c["id"])]
    log(f"用例 {len(cases)} 条，目标 {BASE}")

    with httpx.Client() as client:
        if not wait_server(client):
            log("后端没起来（设 EVAL_BASE_URL 指向评测端口）")
            sys.exit(2)
        token = login(client, DEMO_USER, DEMO_PASS)
        ctx = ensure_other_user(client)
        ctx["other_order_id"] = ctx["order_id"]
        ctx["other_receiver"] = ctx["receiver"]
        ctx["other_mobile"] = ctx["mobile"]
        log(f"探针用户就绪：order_id={ctx['order_id']} 收件人={ctx['receiver']}")

        rows, results = [], []
        for c in cases:
            tkn = None if c.get("auth") == "guest" else token
            recs = sample(client, c, tkn, ctx)
            fail, info = grade(c, recs, ctx)
            if fail and info.get("transport_error"):
                verdict = "ERROR"
            elif fail:
                verdict = "FAIL"
            elif info.get("manual_check"):
                verdict = "REVIEW"
            else:
                verdict = "PASS"
            ans = "".join(r.get("answer", "") for r in recs)
            rows.append({
                "id": c["id"], "kind": c["kind"], "verdict": verdict,
                "ttft": max((r["ttft"] for r in recs if r["ttft"]), default=None),
                "wall": sum((r["wall"] or 0) for r in recs),
                "why": "；".join(fail), "lens": [len(r.get("answer", "")) for r in recs],
                "sims": info.get("ref_sims"), "ref_ids": info.get("ref_ids"),
                "answer": ans, "expect": c.get("expect"),
                "hedged": (recs[-1].get("done") or {}).get("hedged"),
                "manual": info.get("manual_check"), "excerpt": info.get("excerpt"),
                "transport_error": info.get("transport_error"),
                "guard_triggered": info.get("guard_triggered"),
                "own": [r.get("own") for r in recs if r.get("own")],
                "guard_kinds": info.get("guard_kinds"),
                "raw_text": info.get("raw_violations") or [],
                "raw_text_score": info.get("raw_text_score"),
                "final_text_score": info.get("final_text_score"),
                "case_result": verdict,
                "final_text": info.get("final_text_violations") or [],
                "guard_visible": info.get("guard_visible"),
                "in_tokens": info.get("in_tokens"), "out_tokens": info.get("out_tokens"),
                "goods_hits_evidenced": info.get("goods_hits_evidenced"),
                "goods_hits_semantic_only": info.get("goods_hits_semantic_only"),
            })
            results.append({"case": c, "recs": recs, "fail": fail, "info": info})
            sims = info.get("ref_sims")
            log(f"{verdict} {c['id']:<11} 首token {rows[-1]['ttft'] or 0:>5.1f}s 总 {rows[-1]['wall']:>5.1f}s"
                + (f"  sim={max(sims):.3f}" if sims else "")
                + (f"  干预={rows[-1]['hedged']}" if rows[-1].get("hedged") else "")
                + (("  <<< " + rows[-1]["why"]) if fail else ""))

    # 带用例过滤器时写到另一个文件。踩过的坑：为了定性一条抖动用例逐条复跑 5 次，
    # 最后一次把全量明细 results.json 冲成了只剩 1 条 —— 明细比聚合难重新拿到（要再花 61 次调用），
    # 所以"局部复跑不许覆盖全局基线"。文件名不从 argv 拼，避免把正则/路径写进文件名。
    fname = "results.subset.json" if only else "results.json"
    out = os.path.join(os.path.dirname(__file__), fname)
    json.dump(results, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"明细写入 eval/{fname}（本次 {len(results)} 条）")

    n = len(rows)
    npass = sum(1 for r in rows if r["verdict"] == "PASS")
    nreview = sum(1 for r in rows if r["verdict"] == "REVIEW")
    log("\n=== 汇总 ===")
    nerr = sum(1 for r in rows if r["verdict"] == "ERROR")
    log(f"PASS {npass} / REVIEW {nreview} / FAIL {n - npass - nreview - nerr} / ERROR {nerr}  （共 {n}）")
    kinds = {}
    for r in rows:
        k = kinds.setdefault(r["kind"], [0, 0])
        k[0] += 1
        k[1] += r["verdict"] == "PASS"
    for k, (tot, ok) in sorted(kinds.items()):
        log(f"  {k:<13} {ok}/{tot}")

    _ev = sum(r.get("goods_hits_evidenced") or 0 for r in rows)
    _so = sum(r.get("goods_hits_semantic_only") or 0 for r in rows)
    if _ev or _so:
        log(f"  商品候选分层：有词面/型号依据 {_ev} 条，仅语义相近 {_so} 条"
            f"（仅语义占 {_so / (_ev + _so):.0%}）—— 本轮不据此判分，只是把占比测出来")
    else:
        log("  商品候选分层：后端没回传 goods_hits_*（该版后端未实现，或这一轮没有 search_goods 命中）")

    errored = [r for r in rows if r.get("transport_error")]
    vis = [r for r in rows if r.get("guard_visible") and not r.get("transport_error")]
    hard = [r for r in rows if not r.get("manual")]
    saved = [r for r in vis if r["raw_text"] and not r["final_text"]]
    # 误伤 = raw 本来合规、final 却违规（护栏改坏了）
    broke = [r for r in vis if not r["raw_text"] and r["final_text"]]
    ok_reqs = [r for r in rows if not r.get("transport_error")]
    trig = [r for r in ok_reqs if r.get("guard_triggered")]
    log("=== 六元组聚合 ===")
    log(f"成功请求数(干预率分母): {len(ok_reqs)} / 总数 {n}")
    own_frames = [o for r in rows for o in (r.get("own") or []) if o]
    own_deny: dict[str, int] = {}
    for o in [x for x in own_frames if x.endswith("/DENY")]:
        own_deny[o] = own_deny.get(o, 0) + 1
    # Gate2 的读数：翻开 enforcement 前这里必须是 0。
    # 注意只统计"拿到了 session 帧但 owner_ok=false"的情况；被 schema 挡成 400
    # 或被归属挡成 410 的请求根本没有帧，所以不会混进这个数（edge-02 就是这种）。
    log(f"会话归属帧 {len(own_frames)} 条 / DENY {sum(own_deny.values())} 条 {own_deny}")
    log(f"guard intervention rate = {len(trig)}/{len(ok_reqs)}  ← 分母是 successful_requests，含 REVIEW")
    log(f"护栏可见样本(仅供 raw/final 文本规则对比): {len(vis)}/{n}  (需 chat_guard_echo_raw=true)")
    if vis:
        raw_pass = sum(1 for r in vis if not r["raw_text"])
        fin_pass = sum(1 for r in vis if not r["raw_text"] and not r["final_text"])
        log(f"machine-verifiable cases 的 raw text rule pass rate: {raw_pass}/{len(vis)}")
        log(f"machine-verifiable cases 的 final text rule pass rate: {fin_pass}/{len(vis)}")
        log(f"guard_saved (raw_text FAIL → final_text PASS): {len(saved)} {[r['id'] for r in saved]}")
        log(f"guard_harmed (raw_text PASS → final_text FAIL): {len(broke)} {[r['id'] for r in broke]}")
    tok_in = [r["in_tokens"] for r in rows if r.get("in_tokens")]
    tok_out = [r["out_tokens"] for r in rows if r.get("out_tokens")]
    log(f"usage 探得: in {len(tok_in)}条 / out {len(tok_out)}条" + (f"  in P50={sorted(tok_in)[len(tok_in)//2]}" if tok_in else "  (拿不到 token，标定为空)"))
    log(f"基础设施失败(不计入质量): {len(errored)} {[r['id'] for r in errored][:6]}")
    log(f"REVIEW: {nreview}   可机判覆盖率: {len(hard)}/{n} = {len(hard)/n:.0%}")
    ttfts = [r["ttft"] for r in rows if r["ttft"]]
    walls = [r["wall"] for r in rows if r["wall"]]
    if ttfts:
        ttfts.sort()
        walls.sort()
        p = lambda a, q: a[min(len(a) - 1, int(len(a) * q))]
        log(f"  首 token P50 {p(ttfts, 0.5):.2f}s / P95 {p(ttfts, 0.95):.2f}s")
        log(f"  端到端  P50 {p(walls, 0.5):.2f}s / P95 {p(walls, 0.95):.2f}s")
    log(f"明细写入 {out}")

    # 残留提醒：本项目 pytest / dev / eval **共用同一个数据库**（DATABASE_URL 只有一条，
    # APP_ENV 只决定要不要 echo SQL）。跑一轮就在库里留下约 60 个会话 + 120 条消息，
    # 攒了 12 轮之后 zhangsan_demo 名下就是 2767 条评测残留。
    # runner 保持纯 HTTP（不掺数据库依赖），所以清理走旁边那个脚本，它按本轮明细里的 sid 删 ——
    # 那是这批数据唯一能被证明归属的口径。
    sids = {((rec.get("done") or {}).get("session_id"))
            for r in results for rec in (r.get("recs") or [])}
    sids.discard(None)
    if sids:
        log(f"本轮新建/用到会话 {len(sids)} 个 → 清理（默认 dry-run）："
            f"python eval/cleanup_residual.py --from-results {fname}")

    if any(r["verdict"] == "FAIL" for r in rows):
        log("\n=== 失败项 ===")
        for r in rows:
            if r["verdict"] == "FAIL":
                log(f"{r['id']} [{r['expect']}] {r['why']}")

    if nreview:
        log("\n=== 需要人判的（规则判不了，不是失败）===")
        for r in rows:
            if r["verdict"] == "REVIEW":
                log(f"{r['id']}: {r['manual']}")
                log(f"   原文: {r['excerpt'][:200]}")


if __name__ == "__main__":
    main()
