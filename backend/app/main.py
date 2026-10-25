from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.v1 import api_router
from app.core import scheduler
from app.core.config import get_settings
from app.core.database import engine
from app.core.handlers import register_exception_handlers
from app.core.redis import redis_client
from app.core.result import BaseResult

settings = get_settings()

# 只启用客服指标日志，避免把根日志级别提高到 INFO 而输出 SQL 参数。
chat_logger = logging.getLogger("chat")
chat_logger.setLevel(logging.INFO)
if not chat_logger.handlers:
    chat_handler = logging.StreamHandler()
    chat_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    chat_logger.addHandler(chat_handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()                  # 等待运行中的任务完成后，不再触发新任务 
        await engine.dispose()            # 关闭数据库连接池
        await redis_client.aclose()       # 关闭 Redis 连接池


app = FastAPI(
    title="AI客服",
    version="0.1.0",
    lifespan=lifespan,
)

register_exception_handlers(app)       # 处理全局错误的规则
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,   # cors_origins在 config.py 中定义，其中包含的是允许网址对后端进行访问
    allow_credentials=False,               # 只认JWT Token，不认 Cookie
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],      # 允许进行的操作
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],   # 前端发送给后端的请求头（Authorization 是 JWT Token，Content-Type 是 JSON，Idempotency-Key 是防重放攻击）
    expose_headers=["X-Session-Id"],  # 后端返回给前端的响应头（X-Session-Id 是会话 ID），前端要读这个来固定会话
)


@app.get("/health", response_model=BaseResult[dict])
async def health():
    async with engine.connect() as conn:
        pg_ok = (await conn.execute(text("SELECT 1"))).scalar() == 1    # 测试数据库连接是否正常
    redis_ok = await redis_client.ping()                                # 测试 Redis 连接是否正常
    return BaseResult.ok({"postgres": pg_ok, "redis": redis_ok, "env": settings.app_env})

