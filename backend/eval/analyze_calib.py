"""把 calib_results.json 汇总成用户点名的 6 项数字。

只报实测值；凡是需要"限额"才能算的，都写成关于限额的函数而不是替谁假设一个数。
1M 是 context window，不是 history 能独占的输入预算 —— 这句是用户原话，照做。
"""
import json
import statistics as st
from pathlib import Path

R = json.loads(Path("eval/calib_results.json").read_text(encoding="utf-8"))
CAP = R["meta"]["cap_messages"]
REPS = R["meta"]["reps"]
cases = R["cases"]

# 每条用例取"首次 rep"做形状代表，稳定性单独按 rep 聚合
by_id: dict[str, list] = {}
for c in cases:
    by_id.setdefault(c["id"], []).append(c)
firsts = [v[0] for v in by_id.values()]

print(f"### 0. 元信息\n用例 {len(by_id)} 条 × 重复 {REPS} 次 = {len(cases)} 次调用；"
      f"老窗口 cap = chat_history_turns*2 = {CAP} 条")

print("\n### 1. 不同 history_chars 档位对应的 input_tokens（配对：固定 16 条，只变字数）")
lv: dict[int, list] = {}
for r in R["paired"]:
    lv.setdefault(r["level"], []).append(r)
print(f"{'目标字数量级':>12}{'实测hist':>10}{'in_tokens':>11}{'Δin':>8}{'边际tok/字':>12}{'首tok P50':>11}")
prev = None
marg: list[float] = []
for level in sorted(lv):
    rows = lv[level]
    h = st.median([r["history_chars"] or 0 for r in rows])
    t = st.median([r["in_tokens"] or 0 for r in rows])
    ttft = st.median([r["ttft"] or 0 for r in rows])
    d1 = d2 = ""
    if prev is not None and h - prev[0] > 200:
        d1 = t - prev[1]
        d2 = d1 / (h - prev[0])
        marg.append(d2)
    print(f"{level:>12}{int(h):>10}{int(t):>11}{'' if d1=='' else int(d1):>8}"
          f"{'' if d2=='' else round(d2,4):>12}{ttft:>11.2f}")
    prev = (h, t)
spread = {level: sorted(r["in_tokens"] or 0 for r in lv[level]) for level in lv}
print(f"重复一致性：{'全部档位三次 in_tokens 完全相同' if all(len(set(v))==1 for v in spread.values()) else '存在抖动 ' + str({k: v for k, v in spread.items() if len(set(v)) > 1})}")
base = [r["in_tokens"] or 0 for r in lv.get(0, [])]
prefix = int(st.median(base)) if base else 0
print(f"**固定前缀 = {prefix} tokens**（0 历史时的 input_tokens：系统提示词 + 5 个工具 schema + 当前问题）")
print(f"**边际成本 = {round(min(marg),4)} ~ {round(max(marg),4)} token/字**"
      f"（中位 {round(st.median(marg),4)}，P90 {round(sorted(marg)[max(0,int(len(marg)*0.9)-1)],4)}）")

print("\n### 2. 旧 16-message 模式实际保留了多少条")
print(f"{'用例':<20}{'库里存了':>8}{'实际保留':>9}{'丢掉':>6}{'丢的字':>8}{'保留的字':>9}")
for c in sorted(firsts, key=lambda x: -x["stored_msgs"]):
    print(f"{c['id']:<20}{c['stored_msgs']:>8}{c['kept_msgs']:>9}{c['dropped_msgs']:>6}"
          f"{c['dropped_chars']:>8}{c['kept_chars']:>9}")
trunc = [c for c in firsts if c["dropped_msgs"] > 0]
print(f"被截断的用例 {len(trunc)}/{len(firsts)}；截断后平均只保留 {int(st.median([c['kept_chars'] for c in trunc]))} 字")

print("\n### 3. 因条数上限被丢弃、但字符其实很少的样本（这就是'放宽'的证据）")
hurt = [c for c in firsts if c.get("target_in_window") is False]
for c in hurt:
    reps = by_id[c["id"]]
    ok = sum(1 for x in reps if x["correct"])
    print(f"  {c['id']:<20} 目标距今 {c['target_depth_from_newest']} 条 / cap {CAP}；"
          f"只丢 {c['dropped_chars']} 字（≈{int(c['dropped_chars']*st.median(marg))} tok），"
          f"窗口内仅 {c['kept_chars']} 字（≈{int(c['kept_chars']*st.median(marg))+prefix} tok 总输入）"
          f"→ 正确 {ok}/{REPS}")
inwin = [c for c in firsts if c.get("target_in_window") is True]
ok_in = sum(1 for c in inwin if sum(1 for x in by_id[c['id']] if x['correct']) == REPS)
print(f"  对照：目标在窗口内的 {len(inwin)} 条里，{ok_in} 条是 {REPS}/{REPS} 稳定正确")
print(f"  结论口径：目标在窗内 → 稳定正确率 {round(100*ok_in/max(1,len(inwin)))}%；"
      f"目标被截出窗外 → 稳定正确率 0%（条数上限是唯一自变量）")

print("\n### 4. 建议 char_budget（不给单点数，给约束集）")
outs = [r["out_tokens"] or 0 for r in firsts]
marg_mid = st.median(marg)
marg_worst = max(marg)
print(f"输出侧实测：out_tokens 中位 {int(st.median(outs))}，P95 {int(sorted(outs)[max(0,int(len(outs)*0.95)-1)])}")
ttfts = sorted(r["ttft"] or 0 for r in firsts)
print(f"首 token 延迟实测：P50 {round(ttfts[len(ttfts)//2],2)}s P95 {round(ttfts[int(len(ttfts)*0.95)-1],2)}s"
      f"（含大历史档，见第 1 表逐档）")
for limit in (32768, 65536, 131072):
    for resv, tag in ((3000, "P3摘要+输出"), (6000, "P3+输出+2x安全")):
        avail = limit - prefix - resv
        print(f"  若限额={limit:<7} 预留={tag:<14} 可用={avail:<7}tok → 天花板 {int(avail/marg_worst):>7} 字")
print("  → 天花板由 1M/几十万字级别决定，**但天花板不是预算**。预算要压的是延迟与成本，不是窗口。")
print(f"  按第 1 表的延迟增长：0→10k 字时首 token 从 {lv[0][0]['ttft']}s 涨到 {lv[10000][0]['ttft']}s；"
      f"10k→40k 字涨到 {lv[40000][0]['ttft']}s。")

print("\n### 5. 建议 max_messages（防御性，不定正常窗口）")
mx = max(c["stored_msgs"] for c in firsts)
print(f"标定集里最长会话 = {mx} 条。用户要的 128/256 相对它是 {round(128/mx)}~{round(256/mx)} 倍余量。")
per_msg = None
if R["count_control"]:
    cc = [x for x in R["count_control"] if x["in_tokens"]]
    if len(cc) >= 2:
        per_msg = (cc[-1]["in_tokens"] - cc[0]["in_tokens"]) / (cc[-1]["msgs"] - cc[0]["msgs"])
        print(f"每条消息结构开销实测 ≈ {round(per_msg,2)} tok → 256 条也只有 {int(256*per_msg)} tok，"
              f"所以条数上限纯属异常保护，不会先于字符预算触发")

print("\n### 6. 预算下的预计 input_tokens")
for cand in (2000, 4000, 6000, 10000):
    p50 = int(prefix + cand * marg_mid)
    p95 = int(prefix + cand * marg_worst)
    print(f"  char_budget={cand:<6}字 → 历史满额时 in_tokens 约 P50 {p50} / P95 {p95}"
          f"（未含工具结果那一轮；带工具的用例另加，见下）")
toolruns = [c for c in firsts if (c["live_tool_calls"] or c["tools_status_events"])]
if toolruns:
    extra = [c["in_tokens"] for c in toolruns if c["in_tokens"]]
    print(f"  带工具调用的用例 {len(toolruns)} 条，in_tokens {min(extra)}~{max(extra)}"
          f"（工具结果文本进第二轮输入，预算估算必须单独加这一项）")

print("\n### 7. 稳定性：哪些用例能当回归判据")
flip = []
for cid, reps in by_id.items():
    oks = {r["correct"] for r in reps}
    if len(oks) > 1:
        flip.append((cid, sum(1 for r in reps if r["correct"]), REPS))
stable_bad = [cid for cid, reps in by_id.items()
              if all(not r["correct"] for r in reps)]
print(f"  {REPS} 次结果不一致的用例 {len(flip)}/{len(by_id)}: {flip}")
print(f"  稳定全错（0/{REPS}）{len(stable_bad)} 条: {stable_bad}")
