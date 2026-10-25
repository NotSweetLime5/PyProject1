import redis.asyncio as aioredis

from app.core.config import get_settings

settings = get_settings()

redis_client = aioredis.from_url(
    settings.redis_url,                 # Redis 连接地址
    encoding="utf-8",                   # 指定字符串编解码格式
    decode_responses=True,              # 自动将 bytes 转为 str
)