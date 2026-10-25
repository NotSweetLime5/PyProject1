from datetime import datetime

from sqlalchemy import BigInteger, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class IdMixin:                         #把数据库表中重复出现的公共字段（如主键、时间戳、软删除标记）抽离成独立的类
    id: Mapped[int] = mapped_column(
              BigInteger, 
              primary_key=True, 
              autoincrement=True
       )


class TimestampMixin:
       created_at: Mapped[datetime] = mapped_column(
              DateTime(timezone=True), 
              server_default=func.now(), 
              nullable=False
              )
       
       
       updated_at: Mapped[datetime] = mapped_column(
              DateTime(timezone=True), 
              server_default=func.now(), 
              onupdate=func.now(), 
              nullable=False
       )
       
       
       deleted_at: Mapped[datetime | None] = mapped_column(
              DateTime(timezone=True), 
              nullable=True
              )
