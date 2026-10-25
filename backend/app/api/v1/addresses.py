from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.core.result import BaseResult
from app.models.user import User
from app.schemas.address import AddressIn, AddressOut
from app.services import address_service

router = APIRouter(prefix="/addresses", tags=["收货地址"])


@router.get("", response_model=BaseResult[list[AddressOut]])
async def list_addresses(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await address_service.list_addresses(db, user.id)
    return BaseResult.ok([AddressOut.model_validate(r) for r in rows])
# AddressOut.model_validate(r)：Pydantic 把数据库 ORM 实例 `Address` → 转成 `AddressOut` 模型
# 过滤字段、脱敏
# BaseResult.ok(xxx)：包装成项目统一成功返回体

@router.post("", response_model=BaseResult[AddressOut])
async def create_address(
    data: AddressIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    address = await address_service.create_address(db, user.id, data)
    return BaseResult.ok(AddressOut.model_validate(address))


@router.put("/{address_id}", response_model=BaseResult[AddressOut])
async def update_address(
    address_id: int,
    data: AddressIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    address = await address_service.update_address(db, user.id, address_id, data)
    return BaseResult.ok(AddressOut.model_validate(address))


@router.post("/{address_id}/default", response_model=BaseResult[AddressOut])
async def set_default(
    address_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    address = await address_service.set_default(db, user.id, address_id)
    return BaseResult.ok(AddressOut.model_validate(address))


@router.delete("/{address_id}", response_model=BaseResult[None])
async def delete_address(
    address_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await address_service.delete_address(db, user.id, address_id)
    return BaseResult.ok(None, "已删除")
