from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusException
from app.core.result import CodeEnum
from app.models.cart import CartItem
from app.models.goods import Goods
from app.schemas.cart import CartItemOut

MAX_CART_LINES = 100


async def add_to_cart(
    db: AsyncSession, user_id: int, goods_id: int, quantity: int
) -> CartItem:
    goods = await db.get(Goods, goods_id)
    if goods is None or goods.deleted_at is not None:
        raise BusException(CodeEnum.NOT_FOUND, "商品不存在")
    if not goods.is_marketable:
        raise BusException(CodeEnum.BAD_REQUEST, "商品已下架")
    if goods.stock < quantity:
        raise BusException(CodeEnum.BAD_REQUEST, "库存不足")

    existing = await db.scalar(
        select(CartItem).where(
            CartItem.user_id == user_id,
            CartItem.goods_id == goods_id,
            CartItem.deleted_at.is_(None),
        )
    )
    # 购物车中已有该商品，数量累加，规格快照被最新的覆盖
    if existing is not None:
        new_qty = existing.quantity + quantity
        if new_qty > goods.stock:
            raise BusException(CodeEnum.BAD_REQUEST, f"库存不足，当前最多可购 {goods.stock} 件")
        existing.quantity = new_qty
        existing.goods_price = goods.price
        existing.spec_values = goods.spec_values
        await db.flush()
        return existing

    lines = await db.scalar(
        select(func.count())
        .select_from(CartItem)
        .where(CartItem.user_id == user_id, CartItem.deleted_at.is_(None))
    )
    if (lines or 0) >= MAX_CART_LINES:
        raise BusException(CodeEnum.BAD_REQUEST, f"购物车最多 {MAX_CART_LINES} 种商品")

    
    #  存储购物车条目到数据库，建立快照
    item = CartItem(
        user_id=user_id,
        goods_id=goods.id,
        goods_name=goods.goods_name,
        goods_price=goods.price,
        goods_image=goods.header_pic,
        spec_values=goods.spec_values,
        quantity=quantity,
    )
    db.add(item)
    # 用来处理“两个请求同时修改同一条购物车记录”时的冲突
    # 不加 from None,会打印出重要信息，造成安全
    try:
        await db.flush()
    except IntegrityError:
        raise BusException(CodeEnum.CONFLICT, "购物车已变化，请刷新后重试") from None
    return item


async def list_cart(db: AsyncSession, user_id: int) -> list[CartItemOut]:
    rows = await db.execute(
        select(
            CartItem,
            Goods.price,
            Goods.stock,
            Goods.is_marketable,
            Goods.deleted_at,
        )
        .outerjoin(Goods, Goods.id == CartItem.goods_id)     # 商品被删除，但是购物车里保留了记录
        .where(CartItem.user_id == user_id, CartItem.deleted_at.is_(None))
        .order_by(CartItem.id.desc())
    )
    out: list[CartItemOut] = []
    for item, price, stock, marketable, deleted in rows:
        #  1. `price is not None`：能关联到商品主表
        #   2. `deleted is None`：商品没有被软删除
        #   3. `marketable`：商品处于上架售卖状态
        alive = price is not None and deleted is None and marketable
        out.append(
            CartItemOut(
                id=item.id,
                goods_id=item.goods_id,
                goods_name=item.goods_name,
                goods_image=item.goods_image,
                goods_price=item.goods_price,
                spec_values=item.spec_values,
                quantity=item.quantity,
                created_at=item.created_at,
                current_price=price,
                current_stock=stock or 0,
                price_changed=bool(alive and price != item.goods_price),
                available=alive and (stock or 0) >= item.quantity,
                insufficient_stock=bool(alive and (stock or 0) < item.quantity),
            )
        )
    return out


async def set_quantity(db: AsyncSession, user_id: int, item_id: int, quantity: int) -> CartItem:
    item = await _owned_item(db, user_id, item_id)
    goods = await db.get(Goods, item.goods_id)
    if goods is None or goods.deleted_at is not None or not goods.is_marketable:
        raise BusException(CodeEnum.BAD_REQUEST, "商品已下架")
    if quantity > goods.stock:
        raise BusException(CodeEnum.BAD_REQUEST, f"库存不足，当前最多可购 {goods.stock} 件")
    item.quantity = quantity
    await db.flush()
    return item


async def remove_items(db: AsyncSession, user_id: int, item_ids: list[int]) -> int:
    """购物车用物理删除。

    软删会和 uq_cart_user_goods 打架：删掉的商品行还占着 (user_id, goods_id)，
    用户再加购同一个商品就撞唯一约束，表现为"删了就加不回来"。
    """
    # 同一个 user_id + goods_id，不能同时存在两条未删除的购物车记录
    # 保证一个用户购物车里同一个商品只能有一行，重复加购做数量累加，不新增行
    if not item_ids:
        return 0
    result = await db.execute(
        select(CartItem).where(
            CartItem.user_id == user_id, CartItem.id.in_(item_ids)
        )
    )
    items = list(result.scalars())
    for item in items:
        await db.delete(item)
    await db.flush()
    return len(items)


async def clear_cart(db: AsyncSession, user_id: int) -> int:
    result = await db.execute(delete(CartItem).where(CartItem.user_id == user_id))
    await db.flush()
    return result.rowcount or 0


async def _owned_item(db: AsyncSession, user_id: int, item_id: int) -> CartItem:
    item = await db.get(CartItem, item_id)
    if item is None or item.user_id != user_id or item.deleted_at is not None:
        raise BusException(CodeEnum.NOT_FOUND, "购物车条目不存在")
    return item
