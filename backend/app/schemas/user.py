from pydantic import BaseModel, ConfigDict, Field

# bcrypt 只吃 72 字节。Pydantic 的 max_length 按字符算，中文最坏 3 字节/字符，
# 所以 24 字符 = 72 字节是最坏情况下安全的上限，不是 72。
_PASSWORD_MAX_CHARS = 24


class UserRegister(BaseModel):
    username: str = Field(min_length=4, max_length=50, pattern=r"^[A-Za-z0-9_]+$")
    password: str = Field(min_length=8, max_length=_PASSWORD_MAX_CHARS)
    phone: str = Field(pattern=r"^1[3-9]\d{9}$")
    nick_name: str | None = Field(default=None, max_length=50)


class UserLogin(BaseModel):
    # 登录不校验长度：密码策略是写入规则，不是读取规则
    username: str
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)    # 允许从 ORM 对象读取属性

    id: int
    username: str
    nick_name: str | None
    head_pic: str | None
    is_active: bool


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut
