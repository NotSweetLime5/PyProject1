"""测试夹具。

为什么不用 pytest-asyncio：本项目所有异步测试都可以用 `asyncio.run` 包一层，
少一个依赖，也少一处"事件循环被上一个测试污染"的经典坑。

为什么要有 goods_snapshot：B 批的临时脚本把 goods.price 改成 1、stock 改成 3，
跑完不还原，导致前端搜索页出现"¥1.00 的华为 Mate60 Pro"。
**测试改共享数据却不还原，比没有测试更糟** —— 它会让下一次运行和人工验收都看到假数据。
"""

import asyncio
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import delete, func, select, text

from app.core.database import AsyncSessionLocal, engine
from app.core.redis import redis_client
from app.main import app
from app.models.catalog import Category
from app.models.cart import CartItem
from app.models.custcare import ChatMessage, ChatSession
from app.models.goods import Goods, GoodsImage
from app.models.order import Order, OrderItem
from app.models.user import Address, User

BASE = "http://testserver"

# 全会话共用一个事件循环。
# 模块级的 engine / redis_client 会把连接绑定到"第一次使用它们的那个循环"，
# 如果每个测试各开一个 asyncio.run，第二个测试就会撞上 Event loop is closed。
# 一次性脚本里不会遇到，是因为整个脚本只有一次 asyncio.run。
_LOOP: asyncio.AbstractEventLoop | None = None


def run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="session", autouse=True)
def app_lifespan():
    """跑一次 FastAPI 生命周期，覆盖 startup/shutdown 代码路径。

    不能每个测试跑一次：lifespan 的 shutdown 会 engine.dispose() 和 redis.aclose()，
    把后面所有测试依赖的连接池拆了。
    """
    ctx = app.router.lifespan_context(app)
    run(ctx.__aenter__())
    yield
    run(ctx.__aexit__(None, None, None))
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


@asynccontextmanager
async def make_client():
    """给测试用的进程内客户端。

    不做成 fixture 是因为测试主体是同步函数 + run()，
    异步客户端必须在那个事件循环内部创建和关闭。
    lifespan 由 app_lifespan 统一负责，这里不重复跑。
    """
    transport = httpx.ASGITransport(app=app, client=("10.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url=BASE, timeout=120) as c:
        yield c


@pytest.fixture(scope="session", autouse=True)
def goods_snapshot():
    """会话级快照：记录所有商品的价格与库存，结束时还原。"""
    async def capture():
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(Goods.id, Goods.price, Goods.stock))).all()
            return {r.id: (r.price, r.stock) for r in rows}

    original = run(capture())
    if not original:
        pytest.skip("goods 表为空，请先跑 python -m app.scripts.seed")
    yield original
    run(_restore(original))


async def _restore(original):
    async with AsyncSessionLocal() as db:
        for gid, (price, stock) in original.items():
            await db.execute(
                text("UPDATE goods SET price=:p, stock=:s WHERE id=:i"),
                {"p": price, "s": stock, "i": gid},
            )
        await db.commit()


@pytest.fixture(scope="session", autouse=True)
def generous_rate_limits():
    """会话级放宽限流。

    限流本身有专门的测试去验证，不该让每个建 10 个用户的测试都撞上去 ——
    那会让"失败原因"和"被测行为"对不上号。
    """
    from app.core.config import get_settings

    s = get_settings()
    saved = (s.login_rate_limit, s.register_rate_limit, s.chat_rate_limit)
    s.login_rate_limit = s.register_rate_limit = s.chat_rate_limit = 100_000
    yield
    s.login_rate_limit, s.register_rate_limit, s.chat_rate_limit = saved


@pytest.fixture(autouse=True)
def clean_rate_limits():
    """每个测试前清掉限流计数。

    不清的话，前面几个测试会把同一个 IP 的配额用光，
    后面的测试拿到 701 却以为是自己的断言错了 —— 这是最难查的一类假失败。
    """
    run(_flush_prefix("rl:"))
    yield
    run(_flush_prefix("rl:"))


async def _flush_prefix(prefix: str):
    async for key in redis_client.scan_iter(f"{prefix}*"):
        await redis_client.delete(key)




@pytest.fixture(scope="session", autouse=True)
def cleanup_users():
    """会话结束时删掉所有测试用户及其下游数据。

    用户名带 tx_ 前缀，所以这条 DELETE 不会碰到种子数据或人工造的账号。
    """
    yield
    run(_purge())


async def _purge():
    async with AsyncSessionLocal() as db:
        ids = list(await db.scalars(select(User.id).where(User.username.like("tx_%"))))
        if not ids:
            return
        await db.execute(
            delete(OrderItem).where(
                OrderItem.order_id.in_(select(Order.id).where(Order.user_id.in_(ids)))
            )
        )
        await db.execute(delete(Order).where(Order.user_id.in_(ids)))
        await db.execute(delete(CartItem).where(CartItem.user_id.in_(ids)))
        await db.execute(delete(Address).where(Address.user_id.in_(ids)))
        # 客服消息和会话归属：chat_messages.user_id 有外键，不删就卡在删用户那步。
        # 按 sid 一起删，否则游客期间建的会话行会永远留在库里。
        sids = list(await db.scalars(
            select(ChatSession.session_id).where(ChatSession.user_id.in_(ids))
        ))
        if sids:
            await db.execute(delete(ChatMessage).where(ChatMessage.session_id.in_(sids)))
        await db.execute(delete(ChatMessage).where(ChatMessage.user_id.in_(ids)))
        await db.execute(delete(ChatSession).where(ChatSession.user_id.in_(ids)))
        await db.execute(delete(User).where(User.id.in_(ids)))
        await db.commit()


async def first_goods_id(brand: str | None = None) -> int:
    async with AsyncSessionLocal() as db:
        stmt = select(Goods.id).where(Goods.deleted_at.is_(None))
        if brand:
            stmt = stmt.where(Goods.brand_name == brand)
        gid = await db.scalar(stmt.order_by(Goods.id).limit(1))
    assert gid is not None, "没有商品数据，请先跑种子脚本"
    return int(gid)


async def count(table, *conds) -> int:
    """按条件数行。

    刻意支持 conds：测试里凡是"整张表应该有 N 行"的断言都是错的，
    因为别的测试也在往同一张表写。必须按本测试创建的数据作用域来数。
    """
    async with AsyncSessionLocal() as db:
        stmt = select(func.count()).select_from(table)
        if conds:
            stmt = stmt.where(*conds)
        return int(await db.scalar(stmt) or 0)


async def set_stock(goods_id: int, stock: int, price=None) -> None:
    async with AsyncSessionLocal() as db:
        sql = "UPDATE goods SET stock=:s" + (", price=:p" if price is not None else "") + " WHERE id=:i"
        args = {"s": stock, "i": goods_id}
        if price is not None:
            args["p"] = price
        await db.execute(text(sql), args)
        await db.commit()


async def get_stock(goods_id: int) -> int:
    async with AsyncSessionLocal() as db:
        return int(await db.scalar(text("SELECT stock FROM goods WHERE id=:i"), {"i": goods_id}))


__all__ = [
    "BASE", "run", "make_client", "first_goods_id", "count", "set_stock", "get_stock",
    "Goods", "GoodsImage", "Category", "Order", "OrderItem", "CartItem", "Address", "User",
]
