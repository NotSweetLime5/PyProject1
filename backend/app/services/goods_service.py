from decimal import Decimal

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusException
from app.core.result import CodeEnum
from app.models.catalog import Category
from app.models.goods import Goods, GoodsImage
from app.schemas.common import Page
from app.schemas.goods import CategoryNode, GoodsDetailOut, GoodsOut


# 把前端传来的字符串翻译成数据库排序指令，如（"price_asc" ->  按"price" 升序）
_SORTS: dict[str, tuple[str, bool]] = {
    "default": ("id", False),
    "price_asc": ("price", True),
    "price_desc": ("price", False),
    "newest": ("created_at", False),
}
# 恶意用户可以请求 GET /goods?page_size=999999，导致数据库一次性扫描并序列化百万行数据，造成内存溢出或接口超时
MAX_PAGE_SIZE = 50


def _visible_conditions(
    category_id: int | None,
    brand_name: str | None,
    min_price: Decimal | None,
    max_price: Decimal | None,
) -> list:
    # 只查没被删的、只查已上架的商品
    conds = [Goods.deleted_at.is_(None), Goods.is_marketable.is_(True)]
    if category_id is not None:
        conds.append(
            or_(                                    # 商品挂三级类目，任一级命中即算属于该类目
                Goods.category1_id == category_id,
                Goods.category2_id == category_id,
                Goods.category3_id == category_id,
            )
        )
    if brand_name:
        conds.append(Goods.brand_name == brand_name)
    if min_price is not None:
        conds.append(Goods.price >= min_price)
    if max_price is not None:
        conds.append(Goods.price <= max_price)
    return conds




"""
max(size, 1) / max(page, 1)  防止用户传入0或负数，避免数据库报错或查出全表数据
min(size, MAX_PAGE_SIZE)限制单页最大条数，防止恶意请求导致内存溢出或数据库慢查询拖垮服务

total = await db.scalar(select(func.count()).select_from(Goods).where(*conds)) or 0
这段嵌套中，最内层的函数表示从Goods这张表中，根据解包conds的条件挑选出符合条件的商品数量，在数据库中进行
or 0 是为了防止数据库返回 None，导致后续计算出错


col, asc = _SORTS.get(sort, _SORTS["default"])
这段代码中从前端传来的sort参数，根据_SORTS字典，判断是按哪个字段排序，以及是升序还是降序
如果前端传入的sort参数不在_SORTS字典中，使用"default"



order = getattr(Goods, col).asc() if asc else getattr(Goods, col).desc()
getattr 的本质：它把 “运行时才能确定的字符串” 转换成了 “代码中的属性访问”
limit(size) 限制每页查询的条数，防止数据库一次性扫描百万行数据，造成内存溢出或接口超时
offset((page - 1) * size) 跳过 (page - 1) 页的 size 条数据，从第 page 页开始查询


stmt: Select = (select(Goods)
                    .where(*conds)
                    .order_by(order, Goods.id)
                    .offset((page - 1) * size)
                    .limit(size)
                )
表示stmt是select类型，并进行赋值
"""
async def list_goods(
    
    db: AsyncSession,
    *,
    page: int = 1,
    size: int = 20,
    category_id: int | None = None,
    brand_name: str | None = None,
    min_price: Decimal | None = None,
    max_price: Decimal | None = None,
    sort: str = "default",
) -> Page[GoodsOut]:
    size = min(max(size, 1), MAX_PAGE_SIZE)
    page = max(page, 1)
    conds = _visible_conditions(category_id, brand_name, min_price, max_price)

    total = await db.scalar(select(func.count()).select_from(Goods).where(*conds)) or 0

    col, asc = _SORTS.get(sort, _SORTS["default"])
    order = getattr(Goods, col).asc() if asc else getattr(Goods, col).desc()
    stmt: Select = (select(Goods)
                    .where(*conds)
                    .order_by(order, Goods.id)
                    .offset((page - 1) * size)
                    .limit(size)
                )
    rows = list(await db.scalars(stmt))
    return Page(
        items=[GoodsOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        size=size,
    )


async def get_goods_detail(db: AsyncSession, goods_id: int) -> GoodsDetailOut:
    goods = await db.get(Goods, goods_id)
    if goods is None or goods.deleted_at is not None:
        raise BusException(CodeEnum.NOT_FOUND, "商品不存在")
    images = await db.scalars(
        select(GoodsImage)
        .where(GoodsImage.goods_id == goods_id)
        .order_by(GoodsImage.sort, GoodsImage.id)
    )
    detail = GoodsDetailOut.model_validate(goods)
    detail.images = list(images)
    # cat_ids是有序的
    cat_ids = [c for c in (goods.category1_id, goods.category2_id, goods.category3_id) if c]
    if cat_ids:
        names = dict(
            (
                await db.execute(
                    select(Category.id, Category.name).where(Category.id.in_(cat_ids))
                )
            ).all()
        )
        detail.category_names = [names[c] for c in cat_ids if c in names]
    return detail


_PALETTE = [
    ("#1e3a8a", "#3b82f6"),
    ("#134e4a", "#14b8a6"),
    ("#4c1d95", "#8b5cf6"),
    ("#7c2d12", "#f97316"),
    ("#831843", "#ec4899"),
    ("#0c4a6e", "#0ea5e9"),
]
_SHOTS = ("主图", "细节", "场景", "配件")


def render_goods_image(goods_id: int, goods_name: str, brand: str | None, index: int) -> str:
    """生成占位商品图（SVG）。

    为什么在后端生成而不是放一堆图片文件：
    种子数据需要真实可访问的 URL，但往仓库里塞几十张二进制图既臃肿又难维护，
    而且换一批商品就得重做。SVG 由 (商品 id, 序号) 决定，稳定、零字节存储、
    文字能直接显示中文商品名，演示时比"图裂了"有说服力得多。
    """
    c1, c2 = _PALETTE[(goods_id + index) % len(_PALETTE)]
    shot = _SHOTS[index % len(_SHOTS)]
    title = goods_name if len(goods_name) <= 16 else goods_name[:15] + "…"
    # SVG 里出现 </data> 会破坏文档，实体转义是必须的
    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="640" viewBox="0 0 640 640">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{c1}"/>
      <stop offset="100%" stop-color="{c2}"/>
    </linearGradient>
  </defs>
  <rect width="640" height="640" fill="url(#g)"/>
  <circle cx="520" cy="120" r="150" fill="#ffffff" opacity="0.08"/>
  <rect x="150" y="200" width="340" height="240" rx="26" fill="#ffffff" opacity="0.16"/>
  <text x="320" y="470" font-family="Microsoft YaHei, PingFang SC, sans-serif" font-size="34"
        font-weight="700" fill="#ffffff" text-anchor="middle">{esc(title)}</text>
  <text x="320" y="516" font-family="Microsoft YaHei, PingFang SC, sans-serif" font-size="22"
        fill="#ffffff" opacity="0.85" text-anchor="middle">{esc(brand or "")} · {shot}</text>
  <text x="24" y="44" font-family="monospace" font-size="18" fill="#ffffff" opacity="0.7">#{goods_id}-{index}</text>
</svg>"""


async def category_tree(db: AsyncSession) -> list[CategoryNode]:
    """一次取全部类目在内存里拼树。
    类目总量是几十条，全表读比递归查库便宜得多。
    真到几千个类目才需要 nested set 或 recursive CTE。
    """
    rows = list(
        await db.scalars(
            select(Category).where(Category.deleted_at.is_(None)).order_by(Category.sort, Category.id)
        )
    )
    nodes = {c.id: CategoryNode.model_validate(c) for c in rows}
    roots: list[CategoryNode] = []
    for c in rows:
        node = nodes[c.id]
        if c.parent_id is None:
            roots.append(node)
        else:
            parent = nodes.get(c.parent_id)
            if parent is not None:
                parent.children.append(node)
            else:
                roots.append(node)  
    return roots


"""
for用来构建树状结构
如果父节点是None，就把它放到根节点列表里
如果父节点存在，就把它放到父节点的children列表里
假设取出的节点有1级节点和3级节点，那么触发else分支，将3级节点加入根节点列表里
"""
