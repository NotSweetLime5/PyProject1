from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.services.memory_lessons import GUIDANCE, TITLES


class LessonReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["active", "dismissed"]
    revision: int = Field(ge=1)


class LessonOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    rule_code: str
    status: Literal["candidate", "active", "dismissed"]
    revision: int
    occurrences: int
    source_session_id: str
    source_message_id: int
    updated_at: datetime

    @computed_field
    @property
    def title(self) -> str:
        return TITLES[self.rule_code]

    @computed_field
    @property
    def guidance(self) -> str:
        return GUIDANCE[self.rule_code]
