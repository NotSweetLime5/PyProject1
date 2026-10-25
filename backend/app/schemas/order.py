from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class OrderCreate(BaseModel):
    cart_item_ids: list[int] = Field(min_length=1, max_length=50)
    address_id: int


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    goods_id: int | None
    goods_name: str
    goods_image: str | None
    goods_price: Decimal
    quantity: int
    spec_values: dict | None


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_no: str
    # 1 未付款 2 已付款待发货 3 已发货 4 已收货 5 交易完成 6 已退款 7 已关闭
    status: int
    total_price: Decimal
    receiver_name: str
    receiver_phone: str
    receiver_address: str
    pay_time: datetime | None
    consign_time: datetime | None
    confirm_time: datetime | None
    finish_time: datetime | None
    close_time: datetime | None
    close_reason: str | None
    created_at: datetime


class OrderDetailOut(OrderOut):
    items: list[OrderItemOut] = []
