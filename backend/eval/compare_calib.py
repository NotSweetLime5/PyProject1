"""old(16 条) vs new(字符预算 6000) 的稳定类对照。

判据按用户定的四条来，不用单次 PASS/FAIL：
  旧 3/3 稳定正确 → 新不得变成 0/3（退化）
  旧 0/3 且因条数截断 → 新应当翻正
  窗口内仍稳定错误（cal-long-1k2 那类）→ 归模型能力，不计入预算账
其余不稳定（部分 rep 对）的单列，不当成功也不当失败。
"""
import json
from pathlib import Path

REPS = 3


def load(path):
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    by = {}
    for r in rows["cases"]:
        by.setdefault(r["id"], []).append(r)
    return by, rows.get("meta", {})


old, old_meta = load("eval/calib_old_16msg.json")
new, new_meta = load("eval/calib_new_6000.json")

print(f"旧配置 meta: cap={old_meta.get('cap_messages')}")
print(f"新配置 meta: char_budget={new_meta.get('char_budget')} max_messages={new_meta.get('max_messages')}")
print()

HDR = f"{'用例':<20}{'旧留条':>6}{'新留条':>6}{'旧字':>7}{'新字':>7}{'旧tok':>7}{'新tok':>7}  {'旧稳定':<7}{'新稳定':<7}{'新停止原因':<14}判定"
print(HDR)
print("-" * len(HDR))


def stab(rows):
    ok = sum(1 for r in rows if r["correct"])
    return f"{ok}/{REPS}"


def cls(rows):
    ok = sum(1 for r in rows if r["correct"])
    if ok == REPS:
        return "稳定对"
    if ok == 0:
        return "稳定错"
    return "不稳定"


regress, flipped, still_bad, unstable = [], [], [], []
for cid in sorted(new):
    o, n = old.get(cid), new[cid]
    if not o:
        continue
    co, cn = cls(o), cls(n)
    if co == "稳定对" and cn == "稳定错":
        regress.append(cid)
    if co == "稳定错" and cn == "稳定对":
        flipped.append(cid)
    if co == "稳定错" and cn == "稳定错":
        still_bad.append(cid)
    if "不稳定" in (co, cn) and not (co == "稳定对" and cn == "稳定错"):
        unstable.append(cid)
    verdict = ""
    if co == "稳定错" and cn == "稳定对":
        verdict = "翻正 ✓"
    elif co == "稳定对" and cn == "稳定对":
        verdict = "保持"
    elif co == "稳定对" and cn == "不稳定":
        verdict = "抖动（记账不当退化）"
    elif co == "稳定对" and cn == "稳定错":
        verdict = "!! 回归"
    elif cn == "稳定错":
        verdict = "仍错（另归因）"
    else:
        verdict = "改善"
    fo, fn = o[0], n[0]
    print(f"{cid:<20}{fo['kept_msgs']:>6}{fn['kept_msgs']:>6}{fo['history_chars']:>7}"
          f"{fn['history_chars']:>7}{fo['in_tokens']:>7}{fn['in_tokens']:>7}  "
          f"{stab(o):<7}{stab(n):<7}{str(fn.get('stop_reason')):<14}{verdict}")

print("\n=== 汇总 ===")
print(f"翻正（旧 0/{REPS} 因截断 → 新 {REPS}/{REPS}）: {len(flipped)} {flipped}")
print(f"回归（旧稳定对 → 新稳定错）: {len(regress)} {regress}")
print(f"仍稳定错误: {len(still_bad)} {still_bad}")
print(f"落在不稳定档（单次判定不可信）: {len(unstable)} {unstable}")

toks_old = [r["in_tokens"] for rows in old.values() for r in rows if r.get("in_tokens")]
toks_new = [r["in_tokens"] for rows in new.values() for r in rows if r.get("in_tokens")]
hk = [r["history_chars"] for rows in old.values() for r in rows if r.get("history_chars") is not None]
hn = [r["history_chars"] for rows in new.values() for r in rows if r.get("history_chars") is not None]


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else 0


print(f"\nhistory_chars  旧 P50={pct(hk, .5)} P95={pct(hk, .95)} 最大={max(hk)}")
print(f"               新 P50={pct(hn, .5)} P95={pct(hn, .95)} 最大={max(hn)}")
print(f"input_tokens   旧 P50={pct(toks_old, .5)} P95={pct(toks_old, .95)} 最大={max(toks_old)}")
print(f"               新 P50={pct(toks_new, .5)} P95={pct(toks_new, .95)} 最大={max(toks_new)}")

reasons = {}
for rows in new.values():
    for r in rows:
        k = r.get("stop_reason")
        reasons[k] = reasons.get(k, 0) + 1
print(f"\n新逻辑 stop_reason 命中分布（{sum(reasons.values())} 次调用）: {reasons}")
print("注意：标定集是被故意做难的（超长/超多轮），命中率不代表真实流量；"
      "max_messages 是否白设要看线上 chat.obs 日志里 stop_reason=max_messages 的占比。")

over = [r["id"] for rows in new.values() for r in rows if r.get("over_budget")]
print(f"触发软预算保底（最近一轮自身超 6000）的用例: {sorted(set(over))}")
