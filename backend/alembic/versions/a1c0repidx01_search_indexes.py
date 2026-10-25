"""检索索引：GIN(tsvector) + GIN(pg_trgm) + HNSW(vector)

Revision ID: a1c0repidx01
Revises: 38a3f958c421
Create Date: 2026-09-28

autogenerate 生成不了这个文件 —— SQLAlchemy 的模型里表达不了 USING gin/hnsw、
WITH (m=…)、partial index 这些 PG 专有语法。手写迁移是正常路径，不是妥协。

索引取舍全部来自 10 万行基准的 EXPLAIN (ANALYZE, BUFFERS) 输出，不是拍脑袋：
  fulltext  高选择性 1.236ms -> 0.313ms（Buffers 174 -> 25）
            低选择性 planner 仍选 Seq Scan，索引无副作用
  fuzzy     44.063ms -> 4.689ms（9.4x）
  semantic  32.421ms -> 1.757ms（18x）
"""

from typing import Sequence, Union

from alembic import op

revision: str = "a1c0repidx01"
down_revision: Union[str, Sequence[str], None] = "38a3f958c421"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 扩展本身不在模型里，必须显式声明，否则新环境跑迁移会挂在 operator does not exist
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # 全文：GIN 是 tsvector 的标配索引
    op.execute(
        "CREATE INDEX ix_goods_search_vector_gin ON goods USING gin (search_vector)"
    )

    # 模糊：pg_trgm 的 GIN 要用专门的 opclass，普通 GIN 对 text 没有意义
    op.execute(
        "CREATE INDEX ix_goods_name_trgm ON goods USING gin (goods_name gin_trgm_ops)"
    )

    # 语义：HNSW。ef_construction/m 是建索引时的精度-内存权衡，默认 64/16 够用
    # partial 到 embedding IS NOT NULL：没嵌上向量的商品不该占索引空间，
    # 而且所有语义查询都带这个条件，planner 能安全复用部分索引
    op.execute(
        "CREATE INDEX ix_goods_embedding_hnsw ON goods "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64) "
        "WHERE embedding IS NOT NULL"
    )

    # FAQ 的向量索引：阶段8 的 RAG 检索走这条
    op.execute(
        "CREATE INDEX ix_faqs_embedding_hnsw ON faqs "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64) "
        "WHERE embedding IS NOT NULL"
    )

    # FAQ 问答对也要能被全文检索命中（客服场景里"退款"这种词必须精确匹配）
    op.execute(
        "CREATE INDEX ix_faqs_question_trgm ON faqs USING gin (question gin_trgm_ops)"
    )


def downgrade() -> None:
    for name in (
        "ix_faqs_question_trgm",
        "ix_faqs_embedding_hnsw",
        "ix_goods_embedding_hnsw",
        "ix_goods_name_trgm",
        "ix_goods_search_vector_gin",
    ):
        op.execute(f"DROP INDEX IF EXISTS {name}")
