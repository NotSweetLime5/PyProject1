from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.core.result import BaseResult
from app.models.user import User
from app.schemas.memory_lesson import LessonOut, LessonReview
from app.services import memory_lessons

router = APIRouter(prefix="/chat/lessons", tags=["记忆纠错教训"])


@router.get("", response_model=BaseResult[list[LessonOut]])
async def list_own(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return BaseResult.ok([LessonOut.model_validate(r) for r in await memory_lessons.list_lessons(db, user.id)])


@router.put("/{lesson_id}", response_model=BaseResult[LessonOut])
async def review_own(lesson_id: int, payload: LessonReview,
                     user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    row = await memory_lessons.review_lesson(db, user.id, lesson_id,
                                             status=payload.status, revision=payload.revision)
    return BaseResult.ok(LessonOut.model_validate(row))
