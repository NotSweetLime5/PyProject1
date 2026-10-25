from sqlalchemy import BigInteger, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, IdMixin, TimestampMixin


class User(Base, IdMixin, TimestampMixin):
       
       
       __tablename__ = "users"
       
       username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
       password_hash: Mapped[str] = mapped_column(String(100))
       phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
       nick_name: Mapped[str | None] = mapped_column(String(50))
       head_pic: Mapped[str | None] = mapped_column(String(255))
       sex: Mapped[str | None] = mapped_column(String(10))
       is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    
    
class Address(Base, IdMixin, TimestampMixin):
       __tablename__ = "addresses"
       
       __table_args__ = (
              Index(
                     "uq_address_one_default",            # 在数据库中创建的索引名
                     "user_id",                           # 索引包含的列
                     unique=True,                         # 索引唯一
                     postgresql_where=text("is_default AND deleted_at IS NULL"),   # 部分索引条件
              ),
       )
       
       user_id: Mapped[int] = mapped_column(
                      BigInteger,
                      ForeignKey("users.id"),
                      nullable=False,
                      index=True,
              )
       
       contact: Mapped[str] = mapped_column(String(50), nullable=False)
       mobile: Mapped[str] = mapped_column(String(20), nullable=False)
       province_name: Mapped[str] = mapped_column(String(50), nullable=False)
       city_name: Mapped[str] = mapped_column(String(50), nullable=False)
       area_name: Mapped[str] = mapped_column(String(50), nullable=False)
       detail: Mapped[str] = mapped_column(String(255), nullable=False)
       zip_code: Mapped[str] = mapped_column(String(10), nullable=True)
       is_default: Mapped[bool] = mapped_column(default=False, nullable=False)
       
