from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TimestampMixin


class Order(Base, IdMixin, TimestampMixin):
       __tablename__ = "orders"

       order_no: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
       user_id: Mapped[int] = mapped_column(
              BigInteger, ForeignKey("users.id"), nullable=False, index=True
       )
       # 1 未付款 2 已付款待发货 3 已发货 4 已收货 5 交易完成 6 已退款 7 已关闭
       status: Mapped[int] = mapped_column(nullable=False, default=1, index=True)
       total_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
       receiver_name: Mapped[str] = mapped_column(String(50), nullable=False)
       receiver_phone: Mapped[str] = mapped_column(String(20), nullable=False)
       receiver_address: Mapped[str] = mapped_column(String(255), nullable=False)
       pay_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
       consign_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
       confirm_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
       finish_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
       close_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
       close_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)



# 存储商品明细
class OrderItem(Base, IdMixin, TimestampMixin):
       __tablename__ = "order_items"

       order_id: Mapped[int] = mapped_column(
              BigInteger, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
       )
       goods_id: Mapped[int | None] = mapped_column(
              BigInteger, ForeignKey("goods.id"), nullable=True
       )
       goods_name: Mapped[str] = mapped_column(String(255), nullable=False)
       goods_image: Mapped[str | None] = mapped_column(String(255), nullable=True)
       goods_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
       quantity: Mapped[int] = mapped_column(nullable=False, default=1)
       spec_values: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
