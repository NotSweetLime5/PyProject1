import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.core.exceptions import BusException
from app.core.result import CodeEnum
from app.models.cart import CartItem
from app.models.goods import Goods
from app.models.order import Order, OrderItem
from app.models.user import Address, User
from app.schemas.order import OrderCreate
from app.services.ownership import get_owned

settings = get_settings()

STATUS_UNPAID = 1
STATUS_PAID = 2
STATUS_CLOSED = 7


def _now() -> datetime:
    return datetime.now(timezone.utc)


def generate_order_no() -> str:
    """
    对外单号：时间戳 + 6 位随机，20 字符。
    内部主键 id 不外露 —— 把自增 id 放进 URL 等于告诉别人你的单日订单量，
    而且能被顺序遍历。
    如果使用数据库自增 id，可能会被攻击者直接遍历 id：/order/10001、/order/10002、...，批量爬取所有用户订单
    
    """
    return f"{_now():%Y%m%d%H%M%S}{random.randint(0, 999_999):06d}"


async def create_order(db: AsyncSession, user: User, data: OrderCreate) -> Order:
    
    
    # 判断地址是否存在且属于用户
    address = await get_owned(db, Address, data.address_id, user.id, "地址不存在")
    
    
    # 根据前端传入的cart_item_ids，从数据库中查询对应的购物车条目
    # ORDER BY goods_id 是给下面的扣库存循环定的**加锁顺序**：两个事务若按不同顺序
    # 锁同一批 goods 行（A 拿 80→87、B 拿 87→80），PG 会判死锁并回滚其中一个 → 500。
    # 全项目按 goods_id 升序拿行锁，环就无法形成。改这里顺序前先看 _restore_stock。
    cart_items = list(
        await db.scalars(
            select(CartItem)
            .where(
                CartItem.user_id == user.id, CartItem.id.in_(data.cart_item_ids)
            )
            .order_by(CartItem.goods_id)
        )
    )
    
    
    # set(data.cart_item_ids)：去重，判断是否有重复的购物车条目
    if len(cart_items) != len(set(data.cart_item_ids)):
        raise BusException(CodeEnum.BAD_REQUEST, "购物车条目有变化，请刷新后重试")


    # 用来累加订单总金额
    total = Decimal("0")
    
    
    # lines 是一个临时列表，用来缓存循环过程中收集到的数据。类型注解：列表里面每一项是一个元组 (CartItem对象, 快照字典dict)
    lines: list[tuple[CartItem, dict]] = []

    # item是CartItem对象，有两个属性：goods_id和quantity
    for item in cart_items:
        result = await db.execute(
            update(Goods)
            .where(
                Goods.id == item.goods_id,
                Goods.stock >= item.quantity,
                Goods.is_marketable.is_(True),   # 只有可购买的商品才更新库存
                Goods.deleted_at.is_(None),      # 商品没有被软删除
            )
            .values(stock=Goods.stock - item.quantity)  # 库存 = 当前库存 - 购买数量，数据库层面计算
            .returning(                           # 更新成功之后，直接把这几个字段一次性返回
                Goods.price, Goods.goods_name, Goods.header_pic, Goods.spec_values
            )
        )
        row = result.first()
        if row is None:
            raise BusException(
                CodeEnum.CONFLICT, f"商品「{item.goods_name}」库存不足或已下架"
            )
            
        # 序列解包
        price, name, pic, spec = row
        total += price * item.quantity
        # lines 是一个临时列表，item是来自购物车（goods_id, quantity），字典是从数据库查询返回的真实商品信息（snap）
        lines.append(
            (
                item,
                {
                    "goods_price": price,  
                    "goods_name": name,
                    "goods_image": pic,
                    "spec_values": spec,
                },
            )
        )

    order = Order(
        order_no=generate_order_no(),
        user_id=user.id,
        # 未支付
        status=STATUS_UNPAID,
        total_price=total,
        receiver_name=address.contact,
        receiver_phone=address.mobile,
        receiver_address=(
            f"{address.province_name}{address.city_name}{address.area_name}{address.detail}"
        ),
    )
    db.add(order)
    await db.flush()  # 拿到 order.id

    for item, snap in lines:
        db.add(
            OrderItem(                 # 关于订单明细的表
                order_id=order.id,
                goods_id=item.goods_id,
                quantity=item.quantity,
                **snap,
            )
        )
        await db.delete(item)  

    await db.flush()
    return order


async def list_orders(
    db: AsyncSession, user_id: int, *, status: int | None, page: int, size: int
) -> list[Order]:
    conds = [Order.user_id == user_id, Order.deleted_at.is_(None)]
    if status is not None:
        # 只有status不为空时，才会添加这个筛选条件
        conds.append(Order.status == status)
    return list(
        await db.scalars(
            select(Order).where(*conds).order_by(Order.id.desc()).offset((page - 1) * size).limit(size)
        )
    )


async def get_order_detail(db: AsyncSession, user_id: int, order_id: int) -> tuple[Order, list[OrderItem]]:
    order = await get_owned(db, Order, order_id, user_id, "订单不存在")
    items = list(
        await db.scalars(
            select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.id)
        )
    )
    return order, items


async def pay_order(db: AsyncSession, user_id: int, order_id: int) -> Order:
    
    
    """模拟支付成功回调。真接支付时这里是异步通知，要校验签名 + 幂等。"""
    
    
    order = await get_owned(db, Order, order_id, user_id, "订单不存在")
    if order.status != STATUS_UNPAID:
        raise BusException(CodeEnum.CONFLICT, f"订单当前状态不可支付（{order.status}）")
    # 判据必须进 WHERE：上面那个 if 读到的状态可能已经被别人改掉（重复点击的第二次支付、
    # 用户主动取消、或超时关单任务）。只靠 if 会把已关闭/已付款的订单再改一遍。
    # 并发双方会在 orders 这一行上排队，后到的醒来时 status=1 不成立 → rowcount=0。
    # （和 close_expired_orders 用的是同一个写法，这次是让 pay/cancel 也遵守它）
    paid = await db.execute(
        update(Order)
        .where(Order.id == order.id, Order.status == STATUS_UNPAID)
        .values(status=STATUS_PAID, pay_time=_now()),
        execution_options={"synchronize_session": False},
    )
    if paid.rowcount == 0:
        raise BusException(CodeEnum.CONFLICT, "订单状态已变化，请刷新后重试")
    await db.refresh(order)   # 响应体要等于库里那一行，而不是我以为写进去的那份
    return order


async def cancel_order(db: AsyncSession, user_id: int, order_id: int) -> Order:
    order = await get_owned(db, Order, order_id, user_id, "订单不存在")
    if order.status != STATUS_UNPAID:
        raise BusException(CodeEnum.CONFLICT, "只有未付款订单可以取消")
    
    # 先抢状态、再回补库存：顺序反过来时，抢不到状态的那一方已经把库存加过了。
    # _restore_stock 里的 stock + quantity 没有任何条件，它只能靠调用方保证"只被调一次"。
    closed = await db.execute(
        update(Order)
        .where(Order.id == order.id, Order.status == STATUS_UNPAID)
        .values(status=STATUS_CLOSED, close_time=_now(), close_reason="用户主动取消"),
        execution_options={"synchronize_session": False},
    )
    if closed.rowcount == 0:
        raise BusException(CodeEnum.CONFLICT, "订单状态已变化，请刷新后重试")

    # _restore_stock 是一个内部函数：恢复订单内所有商品的库存
    await _restore_stock(db, order.id)
    await db.refresh(order)
    return order


async def _restore_stock(db: AsyncSession, order_id: int) -> None:
    # 和 create_order 用同一条规则：按 goods_id 升序拿行锁。
    # 取消/关单先锁 orders 行再锁 goods 行，如果这里不排序，它就会和"正在下单的另一个事务"
    # 互相持锁等待（那边按 80→87，这边按 87→80），被回滚的是谁完全看时序。
    # goods_id 为 NULL 的行被跳过，不参与排序语义（PG 里 NULL 排最后，正好落在末尾）。
    items = list(await db.scalars(
        select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.goods_id)
    ))
    for it in items:
        if it.goods_id is None:
            continue
        await db.execute(
            update(Goods)
            .where(Goods.id == it.goods_id)
            .values(stock=Goods.stock + it.quantity)
        )
    await db.flush()


async def close_expired_orders() -> int:
    """超时关单。被 APScheduler 定时调用，自己开 session，不依赖请求上下文。"""
    deadline = _now() - timedelta(minutes=settings.order_pay_timeout_minutes)
    async with AsyncSessionLocal() as session:
        stale = list(
            await session.scalars(
                select(Order).where(
                    Order.status == STATUS_UNPAID,
                    Order.created_at < deadline,
                    Order.deleted_at.is_(None),
                )
            )
        )
        for order in stale:
            # 再判一次状态：可能用户正好在这几毫秒内付款了
            result = await session.execute(
                update(Order)
                .where(Order.id == order.id, Order.status == STATUS_UNPAID)
                .values(
                    status=STATUS_CLOSED,
                    close_time=_now(),
                    close_reason="超时未支付，系统自动关闭",
                )
            )
            # result.rowcount  ：这条 UPDATE 语句修改了多少行
            if result.rowcount:
                await _restore_stock(session, order.id)
        await session.commit()
        return len(stale)
