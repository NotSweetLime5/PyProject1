"""一轮问答持有会话租约，多标签页/多实例也不会交错写入。"""
import asyncio
from contextlib import asynccontextmanager, suppress

from app.core.redis_ops import release_lock, renew_lock, try_lock

#  ChatBusy  ：业务异常，获取锁失败时抛出，前端收到提示：会话正在回答，等上一轮完成
class ChatBusy(RuntimeError):
    pass


# 同一个会话 session_id，同一时间只能跑一轮问答。
# 防止用户多标签页、多前端实例同时发消息，导致多条回答并发、聊天消息交错乱序、重复写入数据库
"""
 try_lock    ：尝试拿锁，成功返回唯一 token；失败返回 None
 renew_lock  ：续期锁
release_lock ：释放锁（需要传入拿到锁时返回的 token，防止释放别人的锁）
"""



@asynccontextmanager
async def session_lease(session_id: str, *, ttl: int = 180):
    
    
    # 一个会话对应一个锁，锁名是 chat_turn:会话ID
    name = f"chat_turn:{session_id}"
    
    token = await try_lock(name, ttl_seconds=ttl)
    if token is None:
        raise ChatBusy("这个会话正在回答，请等上一轮完成后再发送")
    owner = asyncio.current_task()
    lost = False

    async def keep_alive():
        nonlocal lost
        try:
            while True:
                # 每过 ttl/3 秒主动通知 Redis：“我还在干活，这把锁别过期，再给我续 ttl 秒”
                # 提前多次续，留网络缓冲时间
                await asyncio.sleep(ttl / 3)
                if not await renew_lock(name, token, ttl):
                    raise RuntimeError("会话租约失效")
        except asyncio.CancelledError:
            raise
        except Exception:
            lost = True
            owner.cancel()

    # 启动续期后台任务
    renewal = asyncio.create_task(keep_alive())
    try:
        yield
    except asyncio.CancelledError:
        if lost:
            raise ChatBusy("会话连接暂时中断，请重试") from None
        raise
    finally:
        renewal.cancel()
        with suppress(asyncio.CancelledError):
            await renewal
        with suppress(Exception):
            await asyncio.shield(release_lock(name, token))
