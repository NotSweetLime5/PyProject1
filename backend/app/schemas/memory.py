from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# 代码和接口层面限制记忆只能存这三类，防止随意写乱七八糟的 key，统一管理用户长期偏好
MemoryKey = Literal["brand_preference", "usual_use", "answer_style"]


class MemoryIn(BaseModel):
    value: str = Field(min_length=1, max_length=120)

    @field_validator("value")
    @classmethod
    def clean_value(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("偏好必须是非空的单行文本")
        return value


class MemoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    key: MemoryKey
    value: str
    revision: int
    updated_at: datetime
