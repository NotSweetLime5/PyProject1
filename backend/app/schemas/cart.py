from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class CartAdd(BaseModel):
    goods_id: int
    quantity: int = Field(default=1, ge=1, le=999)


class CartQuantity(BaseModel):
    quantity: int = Field(ge=1, le=999)


class CartItemOut(BaseModel):
    """购物车条目 = 加购时的快照 + 下单前的实时校验。

    两套价格并存是刻意的：goods_price 是你加购那一刻的价格（历史事实），
    current_price 是现在卖多少。前端要显示"价格已变动"，靠的就是这两个字段不相等。
    结算时一律用 current_price，绝不用快照价 —— 否则用户可以用旧低价下单。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    goods_id: int
    goods_name: str
    goods_image: str | None
    goods_price: Decimal
    spec_values: dict | None
    quantity: int
    created_at: datetime

    current_price: Decimal | None
    current_stock: int = 0
    price_changed: bool = False
    available: bool = True
    insufficient_stock: bool = False

    @property
    def line_total(self) -> Decimal:
        base = self.current_price if self.current_price is not None else self.goods_price
        return base * self.quantity
# @property 作用：把方法伪装成一个属性
# 调用时，实时执行里面的代码动态计算
