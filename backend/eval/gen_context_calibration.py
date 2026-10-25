"""生成 context_calibration.jsonl。

为什么用生成器而不是手写 27 行长 JSON：这批数据的自变量是**深度**（目标标记在第几条消息里），
手写很容易写出"以为在第 20 条、实际在第 17 条"的用例，而深度正是"16 条上限有没有误伤"的唯一判据。
生成器末尾有 assert，深度不符就当场报错，不会让错的数据跑完 100 次调用才发现。

目标内容全是购物形状的（价格、型号、收件人、城市、码数、订单号），不用随机校验码：
上一版用「K-4OLD」这种串，正撞 system prompt 的离题闸门，模型答"这超出我的服务范围"，
测出来的是拒答率不是上下文能力。
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "context_calibration.jsonl"

# 7 类，按用户点名的覆盖清单
SHORT_FILLER = [
    "好的，那我再问一下别的。",
    "嗯嗯，先这样。",
    "知道了，谢谢。",
    "行，我回头看看。",
    "哦哦明白了。",
    "那我先不加购了。",
]


def msg(role, chars, marker=None, at="head"):
    return {"role": role, "chars": chars, "marker": marker, "at": at}


def chain(n, base_chars=40):
    """n 条无关短闲聊，交替 user/assistant。"""
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append(msg(role, base_chars))
    return out


def alternating(target_at_depth_from_end, total, target_role, target_text, target_chars=45):
    """造 total 条交替消息，把目标放在"从最新往回数第 d 条"。

    用"距今多深"而不是"从最早数第几条"来定义，因为 load_history 是从最新往回取
    `chat_history_turns * 2` 条 —— 决定它会不会被截掉的正是这个深度。
    目标一律放在 user 位置上（下标为偶），这样提问才能说"我提过/我问过"。
    """
    assert 1 <= target_at_depth_from_end <= total, "目标深度必须在窗口可达范围内"
    idx = total - target_at_depth_from_end
    assert idx % 2 == 0, f"{target_text} 会落到 assistant 位置（下标 {idx}），换 total"
    assert target_role == "user", "目标只在 user 消息里"
    return [msg("user" if i % 2 == 0 else "assistant",
                target_chars if i == idx else 40,
                marker=target_text if i == idx else None, at="head")
            for i in range(total)]


def case(cid, cat, note, seed=None, live=None, final=None, expect_tools=False, record_only=False,
         drop_reason=None, target=None, target_depth=None):
    # 传参救急：live 只能是列表，进来的是 dict 就说明把 final 按位置塞错了（我在这批数据里
    # 真的错了一次，症状是 final=None，而 JSONL 照样写得出来 —— 静默坏数据）。
    if isinstance(live, dict) and final is None:
        live, final = [], live
    seed = seed or []
    live = live or []
    assert isinstance(final, dict) and final.get("q"), f"{cid} 的 final 没传对: {final}"
    assert isinstance(seed, list) and isinstance(live, list), f"{cid} 的 seed/live 类型不对"
    assert all(isinstance(x, dict) for x in live), f"{cid} 的 live 元素必须是 {{q: ...}}"
    c = {"id": cid, "cat": cat, "note": note, "seed": seed, "live": live,
         "final": final, "expect_tools": expect_tools}
    if target:
        c["target"] = target
        c["target_depth"] = target_depth
    if record_only:
        c["record_only"] = True
        c["expect_dropped"] = True
    if drop_reason:
        c["drop_reason"] = drop_reason
    return c


CASES = []

# ---------- 1. 多轮短消息：字符极少，只因"条数"被截 ----------
# 深度 4 / 14 在 16 条内（老逻辑该答对）；深度 18 / 26 被截（老逻辑必错，且 chars 很小）
CASES += [
    case("cal-short-d4", "short_multi", "目标距今 4 条，总 12 条，字符很少",
         alternating(4, 12, "user", "深圳"),
         {"q": "我前面说收货城市是哪个？只回答城市名。", "any_of": ["深圳"], "none_of": ["不知道"]},
         target="深圳", target_depth=4),
    case("cal-short-d14", "short_multi", "目标距今 14 条，总 16 条，正好压边界内侧",
         alternating(14, 16, "user", "5499"),
         {"q": "我之前问过一款手机的价格，是多少？只回答数字。", "any_of": ["5499"], "none_of": ["不知道"]},
         target="5499", target_depth=14),
    case("cal-short-d17", "short_multi", "目标距今 17 条，总 21 条：只超 1 条就被截，字符仍极少",
         alternating(17, 21, "user", "李香菊"),
         {"q": "我最开始提到的收件人叫什么？只回答姓名。", "any_of": ["李香菊"], "none_of": ["不知道"]},
         target="李香菊", target_depth=17, record_only=True),
    case("cal-short-d26", "short_multi", "目标距今 26 条，总 30 条：闲聊很多但总字数很低",
         alternating(26, 30, "user", "38 码"),
         {"q": "我之前提过我的鞋码，是多少？只回答码数。", "any_of": ["38"], "none_of": ["不知道"]},
         target="38 码", target_depth=26, record_only=True),
    case("cal-short-d40", "short_multi", "目标距今 40 条，总 44 条：纯闲聊堆出来的长会话",
         alternating(40, 44, "user", "要送人的", target_chars=30),
         {"q": "我最早说过这件商品是自用还是送人？只回答那两个字。", "any_of": ["送人"],
          "none_of": ["不知道", "自用"]},
         target="要送人的", target_depth=40, record_only=True),
    case("cal-short-d60", "short_multi", "目标距今 60 条，总 64 条：老逻辑只剩 16/64，字符却仍极少",
         alternating(60, 64, "user", "别推荐红色的", target_chars=30),
         {"q": "我很早以前提过一个颜色方面的忌讳，是什么？简短回答。", "any_of": ["红色"],
          "none_of": ["不知道"]},
         target="别推荐红色的", target_depth=60, record_only=True),
]

# ---------- 2. 少轮超长回答：条数少，字符压力主导 ----------
CASES += [
    case("cal-long-1k2", "long_few_turns", "4 条里有两条 1200 字，目标在较早那条末尾",
         [msg("user", 40, marker="帮我对比一下这两款手机", at="head"),
          msg("assistant", 1200, marker="Mate60 Pro", at="tail"),
          msg("assistant", 1200, marker="小米14 Ultra", at="mid"),
          msg("user", 40, marker=None)],
         {"q": "你 earlier 提到的第一款型号是什么？只回答型号。", "any_of": ["Mate60 Pro"],
          "none_of": ["不知道"]}),
    case("cal-long-2k6", "long_few_turns", "目标埋在 2600 字长回答中部",
         [msg("user", 40, marker=None),
          msg("assistant", 2600, marker="FreeBuds Pro 3", at="mid"),
          msg("user", 40, marker=None)],
         {"q": "那段长说明里提到的耳机型号是什么？只回答型号。", "any_of": ["FreeBuds"],
          "none_of": ["不知道"]}),
    case("cal-long-5k", "long_few_turns", "单条 5000 字，目标在其中",
         [msg("user", 40, marker=None),
          msg("assistant", 5000, marker="20261004084513368396", at="mid"),
          msg("user", 40, marker=None)],
         {"q": "刚才那段里出现的订单号是什么？只回答数字串。", "any_of": ["20261004084513368396"],
          "none_of": ["不知道"]}),
    case("cal-long-6k-far", "long_few_turns", "两条各 3000 字，目标在最早一条末尾（又长又远）",
         [msg("assistant", 3000, marker="广州", at="tail"),
          msg("assistant", 3000, marker="北京", at="mid"),
          msg("user", 60, marker=None)],
         {"q": "最早那条说明里提到的城市是哪个？只回答城市名。", "any_of": ["广州"],
          "none_of": ["北京", "不知道"]}),
]

# ---------- 3. 远距离约束/指代：真实的购买约束 ----------
CASES += [
    case("cal-con-budget", "distant_constraint", "目标在最旧一条（距今 10 条），后面全是闲聊，最后要用它判断",
         # 深度 10 / 总 10 == 目标落在下标 0，也就是最早那条
         alternating(10, 10, "user", "我预算不超过两千块", target_chars=60),
         {"q": "按我说过的预算，两千以内有没有降噪耳机？只回答有或没有。", "any_of": ["有"],
          "none_of": ["不知道", "没查到"]}),
    case("cal-con-city", "distant_constraint", "第 1 条给收货城市，最后问能否配送",
         [msg("user", 55, marker="我在新加坡", at="head"), msg("assistant", 60, marker=None),
          msg("user", 40, marker=None), msg("assistant", 60, marker=None),
          msg("user", 40, marker=None), msg("assistant", 60, marker=None),
          msg("user", 40, marker=None), msg("assistant", 60, marker=None)],
         {"q": "按我之前说的所在地，能寄过去吗？只回答能或不能。", "any_of": ["不能"],
          "none_of": ["不知道"]}),
    case("cal-con-brand", "distant_constraint", "约束在第 3 条（中段）",
         [msg("user", 40, marker=None), msg("assistant", 40, marker=None),
          msg("user", 60, marker="只要国产品牌", at="head"), msg("assistant", 40, marker=None),
          msg("user", 40, marker=None), msg("assistant", 40, marker=None),
          msg("user", 40, marker=None)],
         {"q": "我之前提过一个品牌方面的要求，是什么？简短复述。", "any_of": ["国产"],
          "none_of": ["不知道"]}),
    case("cal-con-two", "distant_constraint", "两个约束并存，问较早那个（测混淆）",
         [msg("user", 50, marker="我要 512G 的", at="head"), msg("assistant", 50, marker=None),
          msg("user", 50, marker="预算三千以内", at="head"), msg("assistant", 50, marker=None),
          msg("user", 40, marker=None), msg("assistant", 50, marker=None),
          msg("user", 40, marker=None), msg("assistant", 50, marker=None)],
         {"q": "我最开始提的存储容量要求是多少？只回答容量。", "any_of": ["512"],
          "none_of": ["不知道"]}),
]

# ---------- 4. 最近一轮超长：近端大块内容会不会挤掉远端小目标 ----------
CASES += [
    case("cal-huge-older", "recent_huge", "最近一条 4000 字，目标在最早那条 45 字里",
         [msg("user", 45, marker="上海", at="head"), msg("assistant", 60, marker=None),
          msg("user", 40, marker=None), msg("assistant", 4000, marker="干扰内容", at="mid")],
         {"q": "最早那条用户消息里的城市是哪个？只回答城市名。", "any_of": ["上海"],
          "none_of": ["不知道"]}),
    case("cal-huge-newer", "recent_huge", "同一份历史，改问近端长文里的城市",
         [msg("user", 45, marker="上海", at="head"), msg("assistant", 60, marker=None),
          msg("user", 40, marker=None), msg("assistant", 4000, marker="杭州", at="mid")],
         {"q": "最后那条长回答里出现的城市是哪个？只回答城市名。", "any_of": ["杭州"],
          "none_of": ["不知道"]}),
    case("cal-huge-6k5", "recent_huge", "近端 6500 字，远端目标只有 30 字",
         [msg("user", 30, marker="1288", at="head"), msg("assistant", 40, marker=None),
          msg("assistant", 6500, marker="干扰内容", at="mid")],
         {"q": "第一条消息里提到的价格是多少？只回答数字。", "any_of": ["1288"],
          "none_of": ["不知道"]}),
]

# ---------- 5. 商品/工具连续引用：真工具轮 ----------
CASES += [
    case("cal-tool-name", "tool_reference", "两轮真工具，回指第一款",
         live=[{"q": "华为Mate60 多少钱"}, {"q": "小米14 Ultra 多少钱"}],
         final={"q": "我第一次问的那款手机是哪款？只回答型号。", "any_of": ["Mate60"],
                "none_of": ["不知道"]}, expect_tools=True),
    case("cal-tool-price", "tool_reference", "回指第一轮的价格",
         live=[{"q": "华为Mate60 多少钱"}, {"q": "华为Watch GT4 多少钱"}],
         final={"q": "第一个问题的商品售价是多少？只回答价格。", "any_of": ["5499"],
                "none_of": ["不知道"]}, expect_tools=True),
    case("cal-tool-mid", "tool_reference", "三轮后问中间那轮",
         live=[{"q": "华为FreeBuds Pro 3 多少钱"}, {"q": "小米14 多少钱"},
               {"q": "Redmi K70 Pro 多少钱"}],
         final={"q": "第二个问题对应的商品叫什么？只回答名称。", "any_of": ["小米14"],
                "none_of": ["不知道"]}, expect_tools=True),
    case("cal-tool-con", "tool_reference", "预算约束（种入）+ 一次真工具问价，再合起来判断",
         seed=[msg("user", 50, marker="我要控制在三千以内", at="head")],
         live=[{"q": "华为Mate60 Pro 多少钱"}],
         final={"q": "按我之前说的预算，刚才那款能买吗？只回答能或不能。", "any_of": ["不能"],
                "none_of": ["不知道"]}, expect_tools=False),
]

# ---------- 6. 中英数字混合 ----------
CASES += [
    case("cal-mix-sku", "mixed_script", "字母-数字混合 SKU",
         [msg("user", 80, marker="SKU 是 A7x-K39-Bq2", at="head"), msg("assistant", 70, marker=None),
          msg("user", 40, marker=None), msg("assistant", 70, marker=None),
          msg("user", 40, marker=None)],
         {"q": "我说过的 SKU 完整串是什么？只回答 SKU。", "any_of": ["A7x-K39-Bq2"],
          "none_of": ["不知道"]}),
    case("cal-mix-model", "mixed_script", "中文+英文型号+价格三件套",
         [msg("user", 90, marker="看中的是 FreeBuds Pro 3，1499 那款", at="head"),
          msg("assistant", 80, marker=None), msg("user", 40, marker=None),
          msg("assistant", 80, marker=None)],
         {"q": "我提的那款耳机叫什么？只回答型号。", "any_of": ["FreeBuds"], "none_of": ["不知道"]}),
    case("cal-mix-order", "mixed_script", "20 位订单号",
         [msg("user", 110, marker="订单号是 20261004084331535549", at="tail"),
          msg("assistant", 90, marker=None), msg("user", 40, marker=None),
          msg("assistant", 90, marker=None)],
         {"q": "我之前给的那串订单号是什么？只回答数字。", "any_of": ["20261004084331535549"],
          "none_of": ["不知道"]}),
    case("cal-mix-nums", "mixed_script", "一条里塞三个价格，问第一个",
         [msg("user", 90, marker="6999 5499 3999 我都看了", at="head"),
          msg("assistant", 80, marker=None), msg("user", 40, marker=None),
          msg("assistant", 80, marker=None)],
         {"q": "我第一条消息里写的第一个价格是多少？只回答数字。", "any_of": ["6999"],
          "none_of": ["3999", "不知道"]}),
]

# ---------- 7. 大量无关旧历史 + 有关近期 ----------
CASES += [
    case("cal-bulk-near", "irrelevant_bulk", "6000 字无关在前，目标在近期 50 字里",
         [msg("user", 3000, marker="无关", at="head"), msg("assistant", 3000, marker="无关", at="head"),
          msg("user", 50, marker="我改地址要寄到成都", at="head"), msg("assistant", 60, marker=None)],
         {"q": "我最新说的要寄去哪个城市？只回答城市名。", "any_of": ["成都"],
          "none_of": ["不知道"]}),
    case("cal-bulk-far", "irrelevant_bulk", "同样 6000 字无关，但目标在最早一条",
         [msg("user", 50, marker="我要寄到成都", at="head"),
          msg("assistant", 3000, marker="无关", at="head"),
          msg("user", 3000, marker="无关", at="head"), msg("assistant", 60, marker=None)],
         {"q": "第一条消息里的收货城市是哪个？只回答城市名。", "any_of": ["成都"],
          "none_of": ["不知道"]}),
    case("cal-bulk-10k", "irrelevant_bulk", "10000 字无关 + 近期小目标，最大压力档",
         [msg("user", 5000, marker="无关", at="head"), msg("assistant", 5000, marker="无关", at="head"),
          msg("user", 60, marker="我只要黑色", at="mid"), msg("assistant", 60, marker=None)],
         {"q": "我最近提的颜色要求是什么？只回答颜色。", "any_of": ["黑色"], "none_of": ["不知道"]}),
    case("cal-bulk-manyshort", "irrelevant_bulk", "16 条各 350 字（条数撞顶、字数中等）",
         [msg("user" if i % 2 == 0 else "assistant", 350,
              marker="昆明" if i == 14 else None, at="head") for i in range(16)],
         {"q": "倒数第三条消息里提到的城市是哪个？只回答城市名。", "any_of": ["昆明"],
          "none_of": ["不知道"]}),
]

# 结构自检：目标必须真的落在声明的深度上。
# 之前这里是"从 seed 里挑一个不像数字的 marker"，结果 "5499" 被当成闲聊滤掉，
# 自检自己先错了 —— 判据不能靠启发式猜哪个是目标。
for c in CASES:
    tgt = c.get("target")
    if not tgt:
        continue
    idx = [i for i, m in enumerate(c["seed"]) if m.get("marker") == tgt]
    assert len(idx) == 1, f"{c['id']} 目标 {tgt} 出现 {len(idx)} 次"
    depth = len(c["seed"]) - idx[0]
    assert depth == c["target_depth"], f"{c['id']} 声明深度 {c['target_depth']}，实际 {depth}"
    assert c["seed"][idx[0]]["role"] == "user", f"{c['id']} 目标不在 user 消息上"

# 其余类别同样把深度推出来：判据是"哪个 seed 的 marker 含期望答案片段"，
# 这是精确定位而不是猜（marker 里就写着要答出来的那个串）。
for c in CASES:
    if "target_depth" in c:
        continue
    frags = [f for f in (c["final"].get("any_of") or []) if len(f) >= 2]
    hits = [i for i, m in enumerate(c["seed"])
            if m.get("marker") and any(f in m["marker"] for f in frags)]
    if len(hits) == 1:
        c["target_depth"] = len(c["seed"]) - hits[0]
        c["target_role"] = c["seed"][hits[0]]["role"]
        # 把 marker 原文也写回去：运行时要拿它在库里定位，才能实测"距今第几条"
        c.setdefault("target", c["seed"][hits[0]]["marker"])
    elif c["seed"]:
        # 有 seed 但定位不到目标：这批数据的"深度"自变量对它是空的，标出来让人看见，
        # 而不是默默当正常用例参与统计
        c["target_depth"] = None

seen = set()
for c in CASES:
    assert c["id"] not in seen, f"重复 id {c['id']}"
    seen.add(c["id"])
    assert c["final"].get("any_of"), f"{c['id']} 没有期望片段"

with OUT.open("w", encoding="utf-8") as f:
    for c in CASES:
        f.write(json.dumps(c, ensure_ascii=False) + "\n")

print(f"WROTE {OUT.name} cases={len(CASES)}")
cats = {}
for c in CASES:
    cats[c["cat"]] = cats.get(c["cat"], 0) + 1
print("CATS", cats)
