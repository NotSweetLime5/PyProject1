from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusException
from app.core.result import CodeEnum
from app.core.security import hash_password, verify_password
from app.models.user import User
from app.schemas.user import UserRegister

# 结构合法的 bcrypt 串（cost=12，实测校验耗时 201ms，与真实哈希一致）。
# 「用户不存在」时也拿它跑一次 verify，把 200ms 的时序差抹平，
# 否则攻击者不看响应内容、只看响应耗时就能枚举出哪些用户名注册过。
_DUMMY_HASH = "$2b$12$t5DyFKkuWlTxbFlKFspNnePrQITEsKEX9OJ4UqdXJsGrZOF.JpInu"



"""
前端发送的JSON：{"username": "alice", "password": "MyPass123", "phone": "13800001111", "nick_name": null}
经过schemas/user.py中的class UserRegister(BaseModel)校验后，变成了data: UserRegister 对象
scalar() 方法：返回查询结果的第一个元素，或 None。
写为if taken is not None，而不是写为if taken : 是因为scalar()可能会返回0,false等
"""

async def register(db: AsyncSession, data: UserRegister) -> User:
    taken = await db.scalar(
        select(User.id).where(
            or_(User.username == data.username, User.phone == data.phone)
        )
    )
    if taken is not None:
        raise BusException(CodeEnum.CONFLICT, "用户名或手机号已被占用")

    user = User(
        username=data.username,
        password_hash=await hash_password(data.password),
        phone=data.phone,
        nick_name=data.nick_name or data.username,
    )
    db.add(user)
    try:
        await db.flush()  # 必须使用flush()，因为如果报错，使用flush会当场进行报错，使用refresh()会等下一次查询时才报错
    except IntegrityError:
        raise BusException(CodeEnum.CONFLICT, "用户名或手机号已被占用") from None

    return user


async def authenticate(db: AsyncSession, username: str, password: str) -> User:
    user = await db.scalar(select(User).where(User.username == username))
    # 如果用户存在，就用它的密码哈希，否则用 DUMMY_HASH 校验，把 200ms 的时序差抹平
    ok = await verify_password(password, user.password_hash if user else _DUMMY_HASH)
    
    if user is None or not ok:
        raise BusException(CodeEnum.UNAUTHORIZED, "用户名或密码错误")
    
    
    if user.deleted_at is not None:
        raise BusException(CodeEnum.UNAUTHORIZED, "用户名或密码错误")
    
    
    if not user.is_active:
        # 能走到这里说明对方已持有正确密码，不再是枚举通道，可以给明确原因
        raise BusException(CodeEnum.FORBIDDEN, "账号已被禁用")
    return user