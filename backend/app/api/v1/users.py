from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.core.result import BaseResult
from app.models.user import User
from app.schemas.user import UserOut

router = APIRouter(prefix="/users", tags=["用户"])


@router.get("/me", response_model=BaseResult[UserOut])
async def me(user: User = Depends(get_current_user)):
    return BaseResult.ok(UserOut.model_validate(user))
