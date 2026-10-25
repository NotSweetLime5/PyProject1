from fastapi import APIRouter

from app.api.v1.addresses import router as addresses_router
from app.api.v1.auth import router as auth_router
from app.api.v1.cart import router as cart_router
from app.api.v1.chat import router as chat_router
from app.api.v1.goods import router as goods_router
from app.api.v1.orders import router as orders_router
from app.api.v1.search import router as search_router
from app.api.v1.users import router as users_router
from app.api.v1.memories import router as memories_router

api_router = APIRouter(prefix="/api/v1")      # prefix="/api/v1" 是给这个路由器下所有路由自动添加的 URL 前缀
api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(goods_router)
api_router.include_router(search_router)
api_router.include_router(addresses_router)
api_router.include_router(cart_router)
api_router.include_router(orders_router)
api_router.include_router(chat_router)
api_router.include_router(memories_router)
