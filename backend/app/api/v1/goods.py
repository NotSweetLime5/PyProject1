from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import BusException
from app.core.result import BaseResult, CodeEnum
from app.models.goods import Goods
from app.schemas.common import Page
from app.schemas.goods import CategoryNode, GoodsDetailOut, GoodsOut
from app.services import goods_service

router = APIRouter(tags=["商品"])


@router.get("/categories/tree", response_model=BaseResult[list[CategoryNode]])
async def category_tree(db: AsyncSession = Depends(get_db)):
    return BaseResult.ok(await goods_service.category_tree(db))


@router.get("/goods", response_model=BaseResult[Page[GoodsOut]])
async def list_goods(
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=50),
    category_id: int | None = Query(None),
    brand_name: str | None = Query(None, max_length=100),
    min_price: Decimal | None = Query(None, ge=0),
    max_price: Decimal | None = Query(None, ge=0),
    sort: str = Query("default", pattern="^(default|price_asc|price_desc|newest)$"),
):
    """列表页。sort 用 pattern 锁死取值，而不是在 service 里 if 判断。"""
    data = await goods_service.list_goods(
        db,
        page=page,
        size=size,
        category_id=category_id,
        brand_name=brand_name,
        min_price=min_price,
        max_price=max_price,
        sort=sort,
    )
    return BaseResult.ok(data)


@router.get("/goods/{goods_id}", response_model=BaseResult[GoodsDetailOut])
async def get_goods(goods_id: int, db: AsyncSession = Depends(get_db)):
    return BaseResult.ok(await goods_service.get_goods_detail(db, goods_id))


@router.get(
    "/goods/{goods_id}/images/{index}.svg",
    response_class=Response,
    responses={200: {"content": {"image/svg+xml": {}}}},
)
async def get_goods_image(goods_id: int, index: int, db: AsyncSession = Depends(get_db)):
    """商品图。故意不鉴权：<img src> 发不出 Authorization 头。

    商品图本身是公开信息（列表页已经在传 header_pic），这里只是把同一份数据
    换成按需生成的 SVG，所以公开不扩大任何暴露面。
    """
    goods = await db.get(Goods, goods_id)
    if goods is None or goods.deleted_at is not None:
        raise BusException(CodeEnum.NOT_FOUND, "商品不存在")
    svg = goods_service.render_goods_image(goods_id, goods.goods_name, goods.brand_name, index)
    return Response(
        content=svg,
        media_type="image/svg+xml",
        # 占位图内容只由 (id, index) 决定，可以让浏览器和 CDN 长期缓存
        headers={"Cache-Control": "public, max-age=86400"},
    )
