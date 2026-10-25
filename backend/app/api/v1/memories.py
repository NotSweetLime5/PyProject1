from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.core.result import BaseResult
from app.models.user import User
from app.schemas.memory import MemoryIn, MemoryKey, MemoryOut
from app.services import user_memory

router = APIRouter(prefix="/chat/memories", tags=["用户记忆"])


@router.get("", response_model=BaseResult[list[MemoryOut]])
async def list_own(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return BaseResult.ok([MemoryOut.model_validate(r) for r in await user_memory.list_memories(db, user.id)])


@router.put("/{key}", response_model=BaseResult[MemoryOut])
async def save_own(key: MemoryKey, payload: MemoryIn, user: User = Depends(get_current_user),
                   db: AsyncSession = Depends(get_db)):
    row = await user_memory.set_memory(db, user.id, key, payload.value)
    return BaseResult.ok(MemoryOut.model_validate(row))


@router.delete("/{key}", response_model=BaseResult[dict])
async def delete_own(key: MemoryKey, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await user_memory.set_memory(db, user.id, key, None)
    return BaseResult.ok({"deleted": True})
