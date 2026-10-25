from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.result import BaseResult
from app.schemas.search import SearchResponse
from app.services import search_service

router = APIRouter(tags=["搜索"])


@router.get("/search", response_model=BaseResult[SearchResponse])
async def search(
    db: AsyncSession = Depends(get_db),
    q: str = Query(..., min_length=1, max_length=50, description="搜索关键词"),
    limit: int = Query(20, ge=1, le=50),
    brand_name: str | None = Query(None, max_length=100),
    category_id: int | None = Query(None),
    model: str | None = Query(None, min_length=1, max_length=120, description="完整型号，严格匹配"),
    max_price: Decimal | None = Query(None, ge=0),
    semantic: bool = Query(True, description="关掉语义检索，保留型号精确、全文和模糊检索"),
):
    hits, segmented, degraded = await search_service.hybrid_search(
        db, q, limit=limit, brand_name=brand_name, category_id=category_id,
        model=model, max_price=max_price, semantic=semantic
    )
    return BaseResult.ok(
        SearchResponse(
            query=q, segmented=segmented, total=len(hits), items=hits,
            degraded=degraded,
        )
    )
