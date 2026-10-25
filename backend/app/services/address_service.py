from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusException
from app.core.result import CodeEnum
from app.models.user import Address
from app.schemas.address import AddressIn
from app.services.ownership import get_owned

MAX_ADDRESS_PER_USER = 20


async def list_addresses(db: AsyncSession, user_id: int) -> list[Address]:
    rows = await db.scalars(
        select(Address)
        .where(Address.user_id == user_id, Address.deleted_at.is_(None))
        .order_by(Address.is_default.desc(), Address.id.desc())
    )
    return list(rows)


async def create_address(db: AsyncSession, user_id: int, data: AddressIn) -> Address:
    count = await db.scalar(
        select(func.count())
        .select_from(Address)
        .where(Address.user_id == user_id, Address.deleted_at.is_(None))
    )
    if (count or 0) >= MAX_ADDRESS_PER_USER:
        raise BusException(CodeEnum.BAD_REQUEST, f"最多保存 {MAX_ADDRESS_PER_USER} 个收货地址")

    # 如果这次新建的地址设置为了默认地址，先清除旧的默认地址
    if data.is_default:
        await _clear_default(db, user_id)

    
    # data.model_dump() 将 Pydantic 的 AddressIn 模型转为字典，通过 ** 解包赋值
    address = Address(user_id=user_id, **data.model_dump())
    db.add(address)
    await db.flush()
    return address


async def update_address(db: AsyncSession, user_id: int, address_id: int, data: AddressIn) -> Address:
    
    
    
    # 确保地址存在，并且是当前用户自己的地址
    address = await get_owned(db, Address, address_id, user_id, "地址不存在")
    
    
    
    # 如果这次更新的地址设置为了默认地址，且修改的地址不是当前默认地址，先清除旧的默认地址
    if data.is_default and not address.is_default:
        await _clear_default(db, user_id)
    
        
    
    # data.model_dump() 将 Pydantic 实例，转成一个字典 dict
    # `.items()`，遍历字典，每次循环拿到一组 `(key, value)`  
    # setattr(对象, 属性名, 值) 将字典中的键值对，赋值给对象的属性
    for field, value in data.model_dump().items():
        setattr(address, field, value)
    await db.flush()
    return address


async def set_default(db: AsyncSession, user_id: int, address_id: int) -> Address:
    address = await get_owned(db, Address, address_id, user_id, "地址不存在")
    await _clear_default(db, user_id)
    address.is_default = True
    await db.flush()
    return address


async def delete_address(db: AsyncSession, user_id: int, address_id: int) -> None:
    address = await get_owned(db, Address, address_id, user_id, "地址不存在")
    address.deleted_at = datetime.now(timezone.utc)
    await db.flush()


async def _clear_default(db: AsyncSession, user_id: int) -> None:
    """把该用户其余地址的 is_default 全部置 false，必须在写入新默认之前调用。

    两条 UPDATE 在同一个事务里，所以中间不会有"零个默认"的可见状态。
    但并发下仍可能同时提交两个默认 —— 兜底的是 addresses 上那个部分唯一索引
    uq_address_one_default，它会让后提交的那个直接 IntegrityError。
    """
    await db.execute(
        update(Address)
        .where(
            Address.user_id == user_id,
            Address.is_default.is_(True),
            Address.deleted_at.is_(None),
        )
        .values(is_default=False)
    )
    await db.flush()

# _clear_default这个函数是将这个用户名下，所有正常未删除、标记为默认的收货地址，一次性改成非默认