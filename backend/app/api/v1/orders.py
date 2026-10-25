from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.core.exceptions import BusException
from app.core.redis_ops import (
    acquire_idempotency,
    release_idempotency,
    save_idempotent_result,
)
from app.core.result import BaseResult, CodeEnum
from app.models.user import User
from app.schemas.order import OrderCreate, OrderDetailOut, OrderItemOut, OrderOut
from app.services import order_service

router = APIRouter(prefix="/orders", tags=["订单"])


@router.post("", response_model=BaseResult[OrderDetailOut])
async def create_order(
    data: OrderCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(
        None, alias="Idempotency-Key", max_length=64, description="客户端生成的 UUID，防重复下单"
    ),
):
    if not idempotency_key:
        order = await order_service.create_order(db, user, data)
        return BaseResult.ok(await _detail(db, user.id, order.id))

    state = await acquire_idempotency("order", idempotency_key, user.id)
    if state != "NEW":
        return await _replay(db, user.id, idempotency_key, state)

    try:
        order = await order_service.create_order(db, user, data)
    except Exception:
        # 业务失败必须清掉占位，否则用户改对了参数也永远提交不上来
        await release_idempotency("order", idempotency_key, user.id)
        raise

    await save_idempotent_result("order", idempotency_key, user.id, str(order.id))
    return BaseResult.ok(await _detail(db, user.id, order.id))


async def _replay(db: AsyncSession, user_id: int, key: str, state: str):
    if state == "PROCESSING":
        raise BusException(CodeEnum.CONFLICT, "订单正在处理中，请勿重复提交")
    try:
        detail = await _detail(db, user_id, int(state))
    except BusException:
        # Redis 里记着订单 id，库里却没有 —— 说明那次请求最终没提交成功
        # （commit 在接口返回之后才发生）。清掉占位让用户能重试，
        # 而不是返回一个指向空气的订单号。
        await release_idempotency("order", key, user_id)
        raise BusException(CodeEnum.CONFLICT, "上次下单未成功，请重新提交") from None
    return BaseResult.ok(detail, "订单已创建，返回首次结果")


@router.get("", response_model=BaseResult[list[OrderOut]])
async def list_orders(
    status: int | None = Query(None, ge=1, le=7),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=50),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await order_service.list_orders(db, user.id, status=status, page=page, size=size)
    return BaseResult.ok([OrderOut.model_validate(r) for r in rows])


@router.get("/{order_id}", response_model=BaseResult[OrderDetailOut])
async def get_order(
    order_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    order, items = await order_service.get_order_detail(db, user.id, order_id)
    return BaseResult.ok(_build_detail(order, items))


@router.post("/{order_id}/pay", response_model=BaseResult[OrderOut])
async def pay_order(
    order_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """模拟支付成功。真接支付渠道时这个入口换成异步回调 + 签名校验 + 幂等。"""
    order = await order_service.pay_order(db, user.id, order_id)
    return BaseResult.ok(OrderOut.model_validate(order))


@router.post("/{order_id}/cancel", response_model=BaseResult[OrderOut])
async def cancel_order(
    order_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    order = await order_service.cancel_order(db, user.id, order_id)
    return BaseResult.ok(OrderOut.model_validate(order), "订单已取消")


async def _detail(db: AsyncSession, user_id: int, order_id: int) -> OrderDetailOut:
    # 走 get_order_detail 而不是自己 db.get：那个函数里带归属校验，
    # 在这里另起一条查询路径就等于绕过了越权检查
    order, items = await order_service.get_order_detail(db, user_id, order_id)
    return _build_detail(order, items)


def _build_detail(order, items) -> OrderDetailOut:
    detail = OrderDetailOut.model_validate(order)
    detail.items = [OrderItemOut.model_validate(i) for i in items]
    return detail
