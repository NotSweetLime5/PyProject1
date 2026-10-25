from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.exceptions import BusException
from app.core.redis_ops import hit_rate_limit
from app.core.result import BaseResult, CodeEnum
from app.core.security import create_access_token
from app.schemas.user import TokenOut, UserLogin, UserOut, UserRegister
from app.services import user_service

settings = get_settings()

router = APIRouter(prefix="/auth", tags=["认证"])



"""
await _guard("register", request, settings.register_rate_limit)
用来限流注册接口


UserOut.model_validate(user)的输出是
UserOut(id=42, username="alice", email="a@b.com")

BaseResult.ok输出是
BaseResult(code=0, message="ok", data=UserOut(...))

"""
@router.post("/register", response_model=BaseResult[UserOut])
async def register(request: Request, data: UserRegister, db: AsyncSession = Depends(get_db)):
    await _guard("register", request, settings.register_rate_limit)
    user = await user_service.register(db, data)   
    return BaseResult.ok(UserOut.model_validate(user))


@router.post("/login", response_model=BaseResult[TokenOut])
async def login(request: Request, data: UserLogin, db: AsyncSession = Depends(get_db)):
    await _guard("login", request, settings.login_rate_limit)
    user = await user_service.authenticate(db, data.username, data.password)
    return BaseResult.ok(                                  # 组装为BaseResult(code=0, message="ok", data=TokenOut(...))    
        TokenOut(                                          # 组装为TokenOut(access_token="eyJ...", expires_in=1800, user=UserOut(...))
            access_token=create_access_token(user.id),     #使用user.id创建JWT
            expires_in=settings.jwt_expire_minutes * 60,   # 返回剩余秒数，不返回绝对时刻
            user=UserOut.model_validate(user),             # 剔除 password 等敏感字段
        )
    )


async def _guard(action: str, request: Request, limit: int) -> None:
    ip = request.client.host if request.client else "unknown"
    try:
        limited = await hit_rate_limit(
            f"{action}:{ip}", limit, settings.rate_limit_window_seconds
        )
    except Exception:
        limited = False   # Redis 故障时放行
    if limited:
        raise BusException(CodeEnum.RATE_LIMITED, "操作过于频繁，请稍后再试")
