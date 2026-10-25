from sqlalchemy import BigInteger, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TimestampMixin


class Category(Base, IdMixin, TimestampMixin):
       __tablename__ = "categories"

       name: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
       level: Mapped[int] = mapped_column(nullable=False, default=1)
       parent_id: Mapped[int | None] = mapped_column(
              BigInteger, ForeignKey("categories.id"), nullable=True
       )
       sort: Mapped[int] = mapped_column(nullable=False, default=0)
       
       
       
       
"""
这个表是用来存分类的
假设商品分类为
电子产品（level=1）
   手机（level=2）
       苹果（level=3）
       小米（level=3）
   平板（level=2）

服装（level=1）



name：分类名称，如手机，服装等
level：分类等级，如1，2，3等
parent_id：上级分类id，如手机（level=2）的parent_id为电子产品（level=1）的id
sort：排序号，用于在前端展示时按顺序显示（id创建时间是固定的，而业务经常需要调整顺序）
"""