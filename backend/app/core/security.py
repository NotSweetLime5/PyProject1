from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi.concurrency import run_in_threadpool
from pwdlib import PasswordHash
from pwdlib.hashers.bcrypt import BcryptHasher

from app.core.config import get_settings
from app.core.exceptions import BusException
from app.core.result import CodeEnum

settings = get_settings()

# bcrypt cost=12：单次约 200ms。降到 10 快 4 倍，但离线爆破成本也降 4 倍。
_password_hash = PasswordHash((BcryptHasher(),))


"""
plain: 明文密码
 hash_password 将明文密码哈希化
 verify_password 验证明文密码是否与哈希密码匹配
"""
async def hash_password(plain: str) -> str:
    return await run_in_threadpool(_password_hash.hash, plain) #丢到后台线程池


async def verify_password(plain: str, hashed: str) -> bool:
    # pwdlib 是 verify(password, hash)，与 passlib 顺序相反
    return await run_in_threadpool(_password_hash.verify, plain, hashed)


# 输入user_id,extra是可选的额外信息
def create_access_token(user_id: int, extra: dict[str, Any] | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),      # 必须转字符串：PyJWT 的 encode 容忍 int，decode 会直接拒绝
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    if extra:
        payload.update(extra)
    
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    # 将密钥和算法把字典加密成一串字符

# 客户端传入的JWT字符串
def decode_access_token(token: str) -> int:
    
    """
    解出 user_id
    任何无效/过期统一 401,不告诉调用方是哪一种
    """
    
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],      
            options={"require": ["exp", "sub"]},      
        )
    except jwt.ExpiredSignatureError:
        raise BusException(CodeEnum.UNAUTHORIZED, "登录已过期，请重新登录") from None
    
    except jwt.InvalidTokenError:
        raise BusException(CodeEnum.UNAUTHORIZED, "登录凭证无效") from None
    
    try:
        return int(payload["sub"])
    except (KeyError, ValueError, TypeError):    # 如果sub不存在或不是数字
        raise BusException(CodeEnum.UNAUTHORIZED, "登录凭证无效") from None
