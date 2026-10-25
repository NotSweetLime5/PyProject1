from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.core.result import BaseResult
from app.models.user import User
from app.schemas.cart import CartAdd, CartItemOut, CartQuantity
from app.services import cart_service

router = APIRouter(prefix="/cart", tags=["购物车"])


class CartRemove(BaseModel):
    item_ids: list[int] = Field(min_length=1, max_length=100)


# 四个写接口一律返回"改动后的完整购物车"。
# 只返回被改的那一条，前端还得再发一次 GET 才能刷新角标和合计金额，
# 而合计金额必须由后端算（价格是实时的），所以整表返回反而是最省的。
@router.get("", response_model=BaseResult[list[CartItemOut]])
async def list_cart(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return BaseResult.ok(await cart_service.list_cart(db, user.id))


@router.post("", response_model=BaseResult[list[CartItemOut]])
async def add_cart_item(
    data: CartAdd,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await cart_service.add_to_cart(db, user.id, data.goods_id, data.quantity)
    return BaseResult.ok(await cart_service.list_cart(db, user.id), "已加入购物车")


@router.put("/{item_id}", response_model=BaseResult[list[CartItemOut]])
async def set_quantity(
    item_id: int,
    data: CartQuantity,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await cart_service.set_quantity(db, user.id, item_id, data.quantity)
    return BaseResult.ok(await cart_service.list_cart(db, user.id))


@router.post("/remove", response_model=BaseResult[list[CartItemOut]])
async def remove_items(
    data: CartRemove,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    removed = await cart_service.remove_items(db, user.id, data.item_ids)
    return BaseResult.ok(await cart_service.list_cart(db, user.id), f"已移除 {removed} 件")


@router.delete("", response_model=BaseResult[list[CartItemOut]])
async def clear_cart(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    n = await cart_service.clear_cart(db, user.id)
    return BaseResult.ok(await cart_service.list_cart(db, user.id), f"已清空 {n} 件")
