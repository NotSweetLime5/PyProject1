from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TimestampMixin


class CartItem(Base, IdMixin, TimestampMixin):
       __tablename__ = "cart_items"

       __table_args__ = (UniqueConstraint("user_id", "goods_id", name="uq_cart_user_goods"),)

       user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
       goods_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("goods.id"), nullable=False)
       goods_name: Mapped[str] = mapped_column(String(255), nullable=False)
       goods_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
       goods_image: Mapped[str | None] = mapped_column(String(255), nullable=True)
       spec_values: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
       quantity: Mapped[int] = mapped_column(nullable=False, default=1)
       