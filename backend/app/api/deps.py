"""依赖注入：数据库会话、当前登录用户。"""

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import BusException
from app.core.result import CodeEnum
from app.core.security import decode_access_token
from app.models.user import User


_bearer = HTTPBearer(auto_error=False)

"""
Depends(_bearer) 的值赋给 credentials
credentials是HTTPAuthorizationCredentials类型
HTTPAuthorizationCredentials的值是HTTPAuthorizationCredentials(scheme="Bearer", credentials="eyJ...")
"""
async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),     
    db: AsyncSession = Depends(get_db),
) -> User:
    
    
    if credentials is None:
        raise BusException(CodeEnum.UNAUTHORIZED, "请先登录")

    user_id = decode_access_token(credentials.credentials)
    user = await db.get(User, user_id)

    if user is None or user.deleted_at is not None:
        raise BusException(CodeEnum.UNAUTHORIZED, "登录凭证无效")
    if not user.is_active:
        raise BusException(CodeEnum.FORBIDDEN, "账号已被禁用")
    return user


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    
    if credentials is None:
        return None
    user_id = decode_access_token(credentials.credentials)
    user = await db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise BusException(CodeEnum.UNAUTHORIZED, "登录凭证无效")
    if not user.is_active:
        raise BusException(CodeEnum.FORBIDDEN, "账号已被禁用")
    return user
