"""定时任务：超时未付款订单关闭 + 会话滚动摘要（P3A）。

Redis 锁的意义在于多副本部署。单进程跑不需要它，但一旦起两个实例，
没有锁就是两个 worker 同时扫到同一批订单、同时回补一次库存 —— 凭空多出双倍库存。
摘要那半边同理，代价形式不同：同一批历史被摘要两遍、付两次钱。
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import get_settings
from app.core.redis_ops import release_lock, try_lock
from app.services.chat_summary import run_chat_summary
from app.services.order_service import close_expired_orders

logger = logging.getLogger("chat.scheduler")
settings = get_settings()

_scheduler: AsyncIOScheduler | None = None
JOB_LOCK = "job:order_cleanup"
JOB_SUMMARY_LOCK = "job:chat_summary"


async def run_order_cleanup() -> int:

    # JOB_LOCK是锁的名称，ttl_seconds ：锁的过期时间（TTL），order_cleanup_interval_seconds ： 订单清理任务的执行间隔（秒）
    # 设置  TTL_seconds 作用为 ：锁最多持有 TTL 时间，到期自动释放，防止锁永久卡死，任务彻底停摆
    #  这里设置为 2 个周期，比单轮预期耗时宽，又不会在进程崩掉后把锁卡死很久。
    # try_lock 去尝试获取锁，如果成功，返回锁的 token
    token = await try_lock(JOB_LOCK, ttl_seconds=settings.order_cleanup_interval_seconds * 2)
    
    
    #  token is None ：抢锁失败，代表别的服务实例已经拿到锁，正在跑关单
    #  分布式锁的作用是：多实例抢锁，只有一个实例抢到锁，其余全部跳过本轮，避免重复处理订单
    if token is None:
        logger.debug("另一个实例正在执行关单任务，本轮跳过")
        return 0
    try:
        closed = await close_expired_orders()       # closed ：关闭的订单数量
        if closed:
            logger.info("超时关单完成，关闭 %d 笔", closed)
        # return closed ：返回关闭订单数量，给定时任务调度器
        return closed
    finally:
        # 无论是否成功，都要释放锁
        # release_lock 表示释放锁的时候，需要传递锁的 token，才能成功释放
        await release_lock(JOB_LOCK, token)


async def run_chat_summary_job() -> int:

    # 会话滚动摘要 ： 聊天记录太长，每次不把全部对话传给大模型，定时把老对话压缩成摘要
    # 多副本风险 ： 如果不加锁，两个进程同时做摘要，各自调用LLM浪费token
    token = await try_lock(JOB_SUMMARY_LOCK, ttl_seconds=settings.chat_summary_interval_seconds * 2)
    if token is None:
        logger.debug("另一个实例正在做会话摘要，本轮跳过")
        return 0
    try:
        done = await run_chat_summary()
        if done:
            logger.info("会话摘要完成，%d 个会话", done)
        return done
    finally:
        await release_lock(JOB_SUMMARY_LOCK, token)


def start() -> None:
    
    # global _scheduler ：全局变量，保存调度器实例，保证整个服务只有一个调度器
    global _scheduler
    
    # 只有 调度器不存在 + 配置开启定时任务，才会创建 AsyncIOScheduler
    # 配置定时任务在本项目里理解为 ： 程序自己每 60 秒自动跑一次 close_expired_orders 去扫描超时订单
    if _scheduler is not None or not settings.scheduler_enabled:
        return
    
    # 创建一个支持异步函数的定时任务管理器，让它全部按照北京时间来计算时间，把这个管理器实例保存到全局变量 _scheduler 
    _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    
    
    # 向调度器注册一个定时任务
    _scheduler.add_job(
        # 要定时执行的函数
        run_order_cleanup,
        # 触发器类型：interval 表示固定间隔执行
        "interval",
        # interval的间隔时间
        seconds=settings.order_cleanup_interval_seconds,
        # 最大实例数：1 表示只允许一个实例在运行，避免重复处理订单
        max_instances=1,
        # coalesce=True 表示如果上一轮没跑完，就跳过这一轮，而不是排队堆积；
        # 就算漏了几轮，只要跑一次，就能查出全部超时订单，没必要重复跑多次
        coalesce=True,
        # 错过的任务仍然有效，可以补跑，由于coalesce = True，所以两次积压的任务，只跑一次
        misfire_grace_time=None,
    )
    
    
    # 会话摘要功能是可选功能，配置中为True
    if settings.chat_summary_enabled:
        # 定时任务
        _scheduler.add_job(
            run_chat_summary_job,
            "interval",
            seconds=settings.chat_summary_interval_seconds,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=None,
        )
    
    # 真正启动定时任务管理器
    _scheduler.start()
    logger.info("超时关单任务已启动，间隔 %d 秒", settings.order_cleanup_interval_seconds)

# 停止定时任务
def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        # wait=False : 不要等待正在运行中的任务执行完毕，直接关闭调度器
        _scheduler.shutdown(wait=False)
        _scheduler = None
