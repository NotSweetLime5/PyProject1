from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Computed, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.core.product_model import MODEL_KEY_SQL
from app.models.base import Base, IdMixin, TimestampMixin

settings = get_settings()


class Goods(Base, IdMixin, TimestampMixin):
       __tablename__ = "goods"
       __table_args__ = (Index("ix_goods_model_key", "model_key"),)

       goods_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
       model_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
       # 一个型号可对应多个颜色/容量版本，索引不能设为 UNIQUE。
       model_key: Mapped[str | None] = mapped_column(Text, Computed(MODEL_KEY_SQL, persisted=True), nullable=True)
       caption: Mapped[str | None] = mapped_column(String(255), nullable=True)
       price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
       stock: Mapped[int] = mapped_column(nullable=False, default=0)
       brand_name: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
       category1_id: Mapped[int | None] = mapped_column( BigInteger, ForeignKey("categories.id"), nullable=True)
       category2_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("categories.id"), nullable=True)
       category3_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("categories.id"), nullable=True)
       header_pic: Mapped[str | None] = mapped_column(String(255), nullable=True)
       is_marketable: Mapped[bool] = mapped_column(nullable=False, default=True)
       introduction: Mapped[str | None] = mapped_column(Text, nullable=True)
       spec_values: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
       search_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
       search_vector: Mapped[str | None] = mapped_column(TSVECTOR,Computed("to_tsvector('simple', search_text)", persisted=True),nullable=True,)
       embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dim), nullable=True)


class GoodsImage(Base, IdMixin, TimestampMixin):
       __tablename__ = "goods_images"

       goods_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("goods.id", ondelete="CASCADE"), nullable=False, index=True)
       url: Mapped[str] = mapped_column(String(255), nullable=False)
       sort: Mapped[int] = mapped_column(nullable=False, default=0)




"""
Goods表: 商品主表
category1_id：一级分类id，如电子产品（level=1）的id
spec_values：商品规格值，如{"品牌":"苹果","颜色":"红色"}
search_text：搜索用的纯文本，把名称、品牌、规格等拼在一起供搜索引擎索引
search_vector：PostgreSQL 全文搜索向量，把search_text转换为向量，用于相似度计算
embedding：AI语义向量，用于"以图搜图"或"相似商品推荐"等AI功能
"""
