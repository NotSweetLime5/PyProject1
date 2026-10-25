from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ChatAsk(BaseModel):
    
    
    # 用户输入的提问文本
    message: str = Field(min_length=1, max_length=500)
    
    
    # 游客之前匿名聊过天，拿到一个 secret；登录之后，带上这个 secret，把游客旧会话认领绑定到自己账号
    guest_secret: str | None = Field(default=None, min_length=16, max_length=128)
    session_id: str | None = Field(default=None, min_length=8, max_length=64)



# 游客匿名聊天产生会话，登录之后调用这个接口，把游客会话绑定到当前登录账号
class ClaimIn(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)
    guest_secret: str = Field(min_length=16, max_length=128)
 
    # session_id 和 guest_secret 两个参数都是必填项
    # 如果少传就报400，不会只凭 session_id 就直接把会话归属改给用户

class ClaimOut(BaseModel):
    session_id: str
    # 标记是否成功认领，认领成功，会话已经绑定到当前登录用户
    claimed: bool
    reason: str


# 用于查询对话历史接口，对应数据库ChatMessage表
class ChatHistoryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: str
    refs: list | None        # AI 回答引用的 FAQ 知识库列表
    created_at: datetime


class SessionOut(BaseModel):
    session_id: str
    last_at: str
    preview: str       # 会话预览文本
    last_role: str
