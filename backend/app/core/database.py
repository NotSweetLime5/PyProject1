from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession,async_sessionmaker,create_async_engine

from app.core.config import get_settings

settings = get_settings()   # 立即获取一个全局共享的、已缓存的配置对象

engine = create_async_engine(
    settings.database_url,
    echo=settings.app_env == "dev",
    pool_size=10,                    # 连接池中始终保持的空闲连接数量
    max_overflow=20,                 # 当 pool_size 中的 10 个连接全部被占用时，允许额外临时创建的最大连接数
    pool_pre_ping=True,              # 验证连接是否仍然可用
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,       # 明确指定返回类型为异步会话
    expire_on_commit=False,    # 禁用会话过期，确保每个请求都有一个独立的会话
    autoflush=False,           # 禁用自动刷新，避免在查询后自动刷新会话中的数据
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()      # 撤销数据库变更，确保数据库状态一致
            raise