# 控制限流，幂等去重，分布式锁的redis操作



import secrets

from app.core.redis import redis_client

_LUA_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

# 基于Redis的限流器
async def hit_rate_limit(bucket: str, limit: int, window_seconds: int) -> bool:
    key = f"rl:{bucket}"                                         # 生成唯一计数器key,
    count = await redis_client.incr(key)                         # 计数器自增
    if count == 1:                                               
        await redis_client.expire(key, window_seconds)           # 第一次访问，设置过期时间
    elif count > limit and await redis_client.ttl(key) < 0:      # 如果计数已经超限，但是TTL丢失了，重新设置过期时间
        await redis_client.expire(key, window_seconds)
    return count > limit                                         # 如果计数超过限制，返回 True（被限流）




"""
防止同一个用户（owner_id）对同一个业务操作（scope+key）进行重复提交/重复处理
new 表示是新提交，processing 表示正在处理中,其他值表示已处理

full = f"idem:{scope}:{owner_id}:{key}"
生成全局唯一的幂等键，如 idem:pay:user123:order456
scope: 业务域隔离（防止不同业务的 key 冲突）
owner_id: 用户维度隔离（A用户的订单不影响B用户）
key: 具体业务标识（订单号、请求ID等）


nx=True → 仅当 Key 不存在时才写入（原子抢占）
ex=ttl → 设置过期时间，防止异常导致永久占用
"""
async def acquire_idempotency(scope: str, key: str, owner_id: int, ttl: int = 300) -> str | None:
    full = f"idem:{scope}:{owner_id}:{key}"
    ok = await redis_client.set(full, "PROCESSING", nx=True, ex=ttl)
    if ok:
        return "NEW"
    return await redis_client.get(full) or "PROCESSING"



"""
幂等性机制的“结果写入”环节，用于在业务处理完成后，将 "PROCESSING" 替换为最终结果（如 "SUCCESS"、订单号等）

scope: 隔离不同的业务场景
key:  具体业务操作的唯一标识（订单号、请求ID等）
owner_id: 用户维度隔离（A用户的订单不影响B用户）
value: 业务处理结果（如 "SUCCESS"、订单号等）
ttl: 过期时间（默认600秒）

Redis I/O 是网络操作，必须用 async/await 避免阻塞事件循环

"""
async def save_idempotent_result(
    scope: str, key: str, owner_id: int, value: str, ttl: int = 600
) -> None:
    full = f"idem:{scope}:{owner_id}:{key}"                                    # 拼接完整的 Redis Key,如 idem:pay:12345:ord_abc
    await redis_client.set(full, value, ex=ttl)                                # 将最终结果写入缓存


#  无论键当前值是 "PROCESSING"、"SUCCESS"、还是键已过期不存在，都会直接删除
#  幂等机制中防止“死锁”导致业务永久瘫痪的兜底策略
async def release_idempotency(scope: str, key: str, owner_id: int) -> None:
    await redis_client.delete(f"idem:{scope}:{owner_id}:{key}")






"""
在多个服务器节点同时竞争同一个资源时，保证只有一个节点抢到锁

nx = true保证了只有第一个到达的请求能写入成功，后续请求全部失败
token表示生成不可伪造的身份凭证，
lock:{name} 表示锁的key，用于存储锁的状态
ex=ttl_seconds 表示锁的过期时间，防止异常导致永久占用锁


ok这一行表示上锁成功，返回token，后续请求需要携带token才能解锁，并设置超时自动开门

"""

async def try_lock(name: str, ttl_seconds: int) -> str | None:
    token = secrets.token_urlsafe(16)
    ok = await redis_client.set(f"lock:{name}", token, nx=True, ex=ttl_seconds)
    return token if ok else None




# return True：令牌匹配解锁成功
# return False：令牌不匹配解锁失败
async def release_lock(name: str, token: str) -> bool:
    return bool(await redis_client.eval(_LUA_RELEASE, 1, f"lock:{name}", token))


async def renew_lock(name: str, token: str, ttl_seconds: int) -> bool:
    return bool(await redis_client.eval(
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end",
        1, f"lock:{name}", token, ttl_seconds,
    ))






"""
幂等机制：    防止因为网络抖动、用户手抖重复点击、消息队列重复投递等原因，导致业务被意外执行多次
简要讲述是幂等会给每一次业务操作发一张一次性通行证
第一次请求会持有有效通行证 --->  执行业务  --->  销毁/标记通行证  --->  返回成功
后续重复请求：   发现通行证已失效   ---> 直接返回之前的成功结果，绝不执行执行业务逻辑
"""
