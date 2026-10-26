"""用户隔离的纠错教训。正文由服务端规则模板提供，表中仅保存类型与事件引用。"""
from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TimestampMixin


class MemoryLesson(Base, IdMixin, TimestampMixin):
    __tablename__ = "memory_lessons"
    __table_args__ = (
        UniqueConstraint("user_id", "rule_code", name="uq_memory_lesson_user_rule"),
        CheckConstraint("rule_code IN ('preference_instruction', 'policy_evidence_gap')", name="ck_lesson_rule"),
        CheckConstraint("status IN ('candidate', 'active', 'dismissed')", name="ck_lesson_status"),
        CheckConstraint("revision >= 1 AND occurrences >= 1", name="ck_lesson_counters"),
    )

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    rule_code: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="candidate", default="candidate")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
    # 原始内容仍在 chat_messages，教训表不复制用户文本、模型原文或联系方式。
    source_session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
