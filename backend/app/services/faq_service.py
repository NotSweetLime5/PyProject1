import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.embedding import embed_one

settings = get_settings()

logger = logging.getLogger(__name__)


@dataclass
class FaqHit:
    id: int
    question: str
    answer: str
    similarity: float
    route: str

"""
语义这一路必须有阈值。A 批实测过：问"不存在的商品xxx"时向量检索仍然返回结果，
因为向量空间里永远存在"最近的邻居"。没有阈值的话 RAG 会把毫不相干的 FAQ 塞进
prompt，模型就照着瞎编 —— 这是 RAG 最常见的失败模式。
取值见 settings.faq_similarity_threshold 的实测依据。
"""

SEMANTIC_FLOOR = settings.faq_similarity_threshold
# pg_trgm 对中文短串的相似度天然偏低（三元组是按字符切的），设高了这一路等于关掉
TRGM_FLOOR = 0.12



# FAQ 知识库检索函数
async def search_faqs(db: AsyncSession, question: str, limit: int = 3) -> list[FaqHit]:
    """
    语义 + 字面模糊两路。

    分别执行两次查询：一次向量检索，一次字面关键词检索
    然后把结果合并、去重、排序
    
    
    只用语义会漏掉"7天无理由""订单号规则"这种必须字面精确命中的条目；
    只用字面会漏掉同义表述。FAQ 量小，两路各查一次比做混合融合划算。
    这里故意没有 tsvector 全文那一路 —— faqs 表没有 search_vector 列，
    和 goods 不对称是刻意的：几百条语料不值得为它维护分词字段和生成列。
    
    """
    # embedding 不可用（缺 key / 限流 / 超时）时降级成只走字面那一路。
    # 和 search_service 同一个理由：读路径的可用性优先于完整性。
    try:
        # 生成这条文本对应的 Embedding 向量，得到一个 Python 浮点数列表
        vec = await embed_one(question)
    except Exception:
        logger.exception("embedding 失败，FAQ 检索降级为字面单路")
        vec = None

    #  字典天然去重 
    hits: dict[int, FaqHit] = {}

    if vec is not None:
        sem = (
            await db.execute(
                text(
                    """
                    SELECT id, question, answer,
                           1 - (embedding <=> CAST(:v AS vector)) AS sim
                    FROM faqs
                    WHERE deleted_at IS NULL AND status = 1 AND embedding IS NOT NULL
                    ORDER BY embedding <=> CAST(:v AS vector)
                    LIMIT :lim
                    """
                ),
                {"v": _lit(vec), "lim": limit * 2},
            )
        ).all()
        for row in sem:
            if row.sim >= SEMANTIC_FLOOR:
                # 合格的 FAQ 包装成 FaqHit 对象，以 FAQ 的 id 作为 key 放进 hits 字典，并且打上标记：这条是语义检索召回的
                hits[row.id] = FaqHit(row.id, row.question, row.answer, float(row.sim), "semantic")

    trgm = (
        await db.execute(
            text(
                """
                SELECT id, question, answer, similarity(question, :q) AS sim
                FROM faqs
                WHERE deleted_at IS NULL AND status = 1
                  AND question % CAST(:q AS text)
                ORDER BY sim DESC
                LIMIT :lim
                """
            ),
            {"q": question, "lim": limit},
        )
    ).all()
    for row in trgm:
        if float(row.sim) >= TRGM_FLOOR:
            hits.setdefault(row.id, FaqHit(row.id, row.question, row.answer, float(row.sim), "trgm"))

    # 按每个 FAQ 的 similarity 相似度打分作为排序依据
    top = sorted(hits.values(), key=lambda h: h.similarity, reverse=True)[:limit]
    if top:
        await db.execute(
            text("UPDATE faqs SET use_count = use_count + 1 WHERE id = ANY(:ids)"),
            {"ids": [h.id for h in top]},
        )
    return top




def _lit(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"
