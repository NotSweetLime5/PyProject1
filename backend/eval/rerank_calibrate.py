"""rerank 标定实验：只看 reranker 分数能不能把"该答的"和"擦边的"分开。

不跑大模型，只跑 retrieval + rerank。文档用「问题 + 答案」拼接，因为那才是
真正进 prompt 的内容。

判定标准提前定死：15 条口语真问题里"正确条目"的 rerank 分最小值，
必须高于 4 条表外问题里"最佳错误候选"的 rerank 分最大值，否则画不出界。
"""

import asyncio
import json

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.models.custcare import Faq
from app.services import faq_service

S = get_settings()
LIMIT = 5  # 给 rerank 的候选池，比最终 top3 宽一点，才看得出它能不能重新排序


async def rerank(query: str, docs: list[str]) -> list[float]:
    r = httpx.post(
        S.siliconflow_base_url.rstrip("/") + "/rerank",
        headers={"Authorization": f"Bearer {S.siliconflow_api_key}"},
        json={"model": "BAAI/bge-reranker-v2-m3", "query": query,
              "documents": docs, "top_n": len(docs), "return_documents": False},
        timeout=60,
    )
    r.raise_for_status()
    out = [0.0] * len(docs)
    for x in r.json()["results"]:
        out[x["index"]] = x["relevance_score"]
    return out


async def main():
    cases = []
    for line in open("eval/golden_set.jsonl", encoding="utf-8"):
        if not line.strip():
            continue
        d = json.loads(line)
        if d["kind"] == "faq":
            cases.append((d["id"], d["q"], d["refs"]["must_include_any_of"][0]))
        elif d["kind"] == "faq_miss":
            cases.append((d["id"], d["q"], None))

    good, bad = [], []
    print("%-10s %-26s %8s %8s  %s" % ("用例", "问题", "正确分", "最佳错分", "备注"))
    print("-" * 88)

    async with AsyncSessionLocal() as db:
        rows = {f.id: f for f in await db.scalars(select(Faq).where(Faq.status == 1))}
        for cid, q, gold_id in cases:
            hits = await faq_service.search_faqs(db, q, limit=LIMIT)
            if not hits:
                print("%-10s %-26s %8s %8s  %s" % (cid, q[:24], "-", "-", "检索池为空(阈值已拦)"))
                if gold_id is None:
                    bad.append(0.0)          # 拦住了，视为 0 分
                continue
            docs = [f"{h.question} {h.answer}" for h in hits]
            scores = await rerank(q, docs)
            best_wrong = max((s for h, s in zip(hits, scores) if h.id != gold_id), default=float("nan"))
            good_score = next((s for h, s in zip(hits, scores) if h.id == gold_id), None)

            if gold_id is None:
                best = max(scores)
                bad.append(best)
                print("%-10s %-26s %8s %8.4f  %s" % (cid, q[:24], "-", best, "无正确条目，取最佳错分"))
            else:
                if good_score is None:
                    good.append(0.0)
                    print("%-10s %-26s %8s %8.4f  %s" % (cid, q[:24], "掉池", best_wrong, "正确条目没进候选"))
                else:
                    good.append(good_score)
                    print("%-10s %-26s %8.4f %8.4f  %s" % (cid, q[:24], good_score, best_wrong, ""))
        await db.rollback()   # 别把 use_count 的写挂上去

    good.sort(); bad.sort()
    print("\n=== 分布 ===")
    print("15 条真问题的『正确条目』rerank 分: min=%.4f 中位=%.4f max=%.4f" % (
        good[0], good[len(good) // 2], good[-1]))
    print("表外问题的『最佳错误候选』rerank 分: min=%.4f max=%.4f  %s" % (
        bad[0], bad[-1], [round(b, 4) for b in bad]))
    gap = good[0] - max(b for b in bad if b > 0) if any(b > 0 for b in bad) else float("inf")
    print("\n可分性: 真命中最低 %.4f  vs  误召最高 %.4f  ->  间隔 %+.4f" % (
        good[0], max(bad), good[0] - max(bad)))
    print("判定:", "可以画界" if gap > 0 else "画不出界（重叠）——rerank 单独当开关也不行")


asyncio.run(main())
