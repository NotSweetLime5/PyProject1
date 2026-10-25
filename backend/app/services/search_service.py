import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embedding import embed_one
from app.core.exceptions import BusException
from app.core.product_model import normalize_model_key
from app.core.result import CodeEnum
from app.core.text import segment, segment_query
from app.schemas.search import SearchHit

logger = logging.getLogger(__name__)

# Reciprocal Rank Fusion。k=60 是信息检索里的通用取值，作用是压掉头部名次的权重差：
# 第 1 名和第 2 名的贡献只差 1/61 - 1/62，不会因为某一路的分数尺度不同而主导融合结果。
# 我们不用加权求和，正是因为 ts_rank、similarity、余弦相似度三个量纲根本不可比。
# RRF_K ：排名靠后的商品分数衰减速度
RRF_K = 60


"""
每一路独立查询数据库 / 向量库，各自返回 Top30 条高分商品。
然后把这几路拿到的候选集合合并、去重，送入 RRF 融合排序，最后再截断成最终返回给用户的结果
"""

CANDIDATES_PER_ROUTE = 30




"""

如果传进来的参数是 NULL → 这一整段条件直接 = True，等于【不做这个筛选】
如果传进来参数有值 → 就执行后面的【过滤逻辑】

"""

_FILTERS = """
          AND (CAST(:brand AS text) IS NULL OR g.brand_name = CAST(:brand AS text))
          AND (CAST(:cat AS bigint) IS NULL OR g.category1_id = CAST(:cat AS bigint)
                                        OR g.category2_id = CAST(:cat AS bigint)
                                        OR g.category3_id = CAST(:cat AS bigint))
          AND (CAST(:max_price AS numeric) IS NULL OR g.price <= CAST(:max_price AS numeric))
          AND (CAST(:requested_model AS text) IS NULL OR g.model_key = CAST(:requested_model AS text))
"""


"""
每一路 CTE 独立查询商品，算出本条商品在这一路里的排名 r；
然后合并四路结果，执行 RRF 倒数排名融合，计算综合 score；
最后关联 goods 表取出商品详情，返回给后端，直接拿到score和srcs（就是matched_by）
"""

_HYBRID_SQL = text(
    f"""
    WITH model_match AS (
        SELECT g.id, ROW_NUMBER() OVER (ORDER BY g.id) AS r
        FROM goods g
        WHERE g.model_key = CAST(:model_key AS text)
          AND g.deleted_at IS NULL AND g.is_marketable
          {_FILTERS}
        ORDER BY g.id
        LIMIT CAST(:exact_cand AS int)
    ),
    ft AS (
        SELECT g.id,
               ROW_NUMBER() OVER (
                   ORDER BY ts_rank(g.search_vector, q) DESC, g.id
               ) AS r
        FROM goods g, to_tsquery('simple', :ftq) q
        WHERE g.search_vector @@ q
          AND g.deleted_at IS NULL AND g.is_marketable
          {_FILTERS}
        LIMIT CAST(:cand AS int)
    ),
    tg AS (
        SELECT g.id,
               ROW_NUMBER() OVER (
                   ORDER BY similarity(g.goods_name, :raw) DESC, g.id
               ) AS r
        FROM goods g
        WHERE similarity(g.goods_name, :raw) >= CAST(:sim_min AS float8)
          AND g.deleted_at IS NULL AND g.is_marketable
          {_FILTERS}
        LIMIT CAST(:cand AS int)
    ),
    sem AS (
        SELECT g.id,
               ROW_NUMBER() OVER (
                   ORDER BY g.embedding <=> CAST(:vec AS vector), g.id
               ) AS r
        FROM goods g
        WHERE CAST(:vec AS text) IS NOT NULL  -- semantic=False 时传 NULL，整路短路
          AND g.embedding IS NOT NULL
          AND g.deleted_at IS NULL AND g.is_marketable
          {_FILTERS}
        ORDER BY g.embedding <=> CAST(:vec AS vector)
        LIMIT CAST(:cand AS int)
    ),
    merged AS (
        SELECT id, r, 'model_exact' AS src FROM model_match
        UNION ALL
        SELECT id, r, 'fulltext' AS src FROM ft
        UNION ALL
        SELECT id, r, 'fuzzy'    AS src FROM tg
        UNION ALL
        SELECT id, r, 'semantic' AS src FROM sem
    ),
    fused AS (
        SELECT id,
               SUM(1.0 / (CAST(:k AS int) + r)) AS score,
               BOOL_OR(src = 'model_exact') AS exact_model,
               ARRAY_AGG(src ORDER BY src) AS srcs
        FROM merged
        GROUP BY id
        ORDER BY exact_model DESC, score DESC, id
        LIMIT CAST(:lim AS int)
    )
    SELECT g.id, g.goods_name, g.model_name, g.model_key, g.caption, g.price, g.stock,
           g.brand_name, g.header_pic, g.is_marketable,
           f.score, f.srcs
    FROM fused f
    JOIN goods g ON g.id = f.id
    ORDER BY f.exact_model DESC, f.score DESC, f.id
    """
)


async def hybrid_search(
    db: AsyncSession,
    query: str,
    *,
    #  最终返回多少条商品
    limit: int = 20,
    brand_name: str | None = None,
    category_id: int | None = None,
    max_price: float | None = None,
    # 前端单独型号筛选框传入的型号
    model: str | None = None,
    # 是否开启语义搜索
    semantic: bool = True,
) -> tuple[list[SearchHit], str, bool]:
    """
    型号精确命中优先，再融合全文/模糊/语义。
    model 为显式的严格型号过滤；不传时仅尝试将完整 query 作为型号键。
    不从自由文本猜型号，也不将相似型号视为显式型号的替代品。
    返回 (命中列表, 分词后的查询串, 是否降级)。
    """
    raw = query.strip()
    # normalize_model_key : 把型号名称统一成固定格式，消除空格、大小写差异
    requested_model = normalize_model_key(model) if model is not None else None
    if model is not None and not requested_model:
        raise BusException(CodeEnum.BAD_REQUEST, "型号不能为空")
    if not raw and requested_model is None:
        return [], "", False

    ftq = segment_query(raw)
    # 用来存放用户搜索文本的向量，初始为空
    vector = None
    # 降级标记
    degraded = False
    if semantic:
        try:
            vector = await embed_one(raw)
        except Exception:
            # 向量服务不可用（缺 key、限流、超时）不能让搜索整个变成 500。
            # 降级成全文+模糊两路，结果质量下降但服务还在 —— 检索是读路径，
            # 读路径的可用性优先于完整性。
            logger.exception("embedding 失败，本次检索降级为词面两路")
            degraded = True

    
    # 执行混合检索 SQL
    rows = await db.execute(
        _HYBRID_SQL,
        {   
            # ftq 就是前面 segment_query(raw) 生成的 词1 & 词2，用于 Postgres 全文检索 to_tsquery
            "ftq": ftq,  
            # 用户输入文本
            "raw": raw,
            # 向量相似度最低阈值
            "sim_min": 0.05,  # 中文短查询的相似度本来就低，设高了这一路等于关掉
            "vec": None if vector is None else _vec_literal(vector),
            "k": RRF_K,
            "cand": CANDIDATES_PER_ROUTE,
            # 精确匹配的候选条数，取两个值里更大的那个
            "exact_cand": max(CANDIDATES_PER_ROUTE, limit),
            "lim": limit,
            "brand": brand_name,
            "cat": category_id,
            "max_price": max_price,
            "requested_model": requested_model,
            # 如果没有选型号，尝试把搜索框文字 raw标准化，当成 model_key 精确匹配
            "model_key": requested_model or normalize_model_key(raw) or None,
        },
    )

    # 数据库查询拿到rows（混合检索出来的商品结果），循环每一行，包装成SearchHit对象放进列表hits
    hits: list[SearchHit] = []
    for row in rows:
        hits.append(
            SearchHit(
                id=row.id,
                goods_name=row.goods_name,
                model_name=row.model_name,
                model_key=row.model_key,
                caption=row.caption,
                price=row.price,
                stock=row.stock,
                brand_name=row.brand_name,
                header_pic=row.header_pic,
                is_marketable=row.is_marketable,
                score=round(float(row.score), 6),
                matched_by=list(row.srcs),
            )
        )
    # 返回 (命中列表, 分词后的查询串, 是否降级)
    return hits, segment(raw), degraded


# 把 Python 的浮点向量列表，转成 pgvector 能识别的字符串格式 [0.123456,0.654321,...]，用于拼进原生 SQL
def _vec_literal(vector: list[float]) -> str:
    """
    pgvector 的字面量形式是 '[1.0,2.0,...]'。

    走字符串 + SQL 里 CAST(... AS vector)，而不是让 asyncpg 直接绑定 list，
    因为这里用的是裸 text() SQL，SQLAlchemy 没有类型信息可推断。
    """
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"
