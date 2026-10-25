from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, false, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.models.base import Base, IdMixin, TimestampMixin

settings = get_settings()


# 用来存储常见问题 FAQ（智能问答知识库），是项目里向量检索用到的知识库表
class Faq(Base, IdMixin, TimestampMixin):
       __tablename__ = "faqs"

       # 0 商品 1 物流 2 售后 3 活动 99 其他（分类写死在 Python 枚举，无对应表）
       category_id: Mapped[int] = mapped_column(nullable=False, default=0, index=True)
       question: Mapped[str] = mapped_column(String(500), nullable=False)
       answer: Mapped[str] = mapped_column(Text, nullable=False)
       status: Mapped[int] = mapped_column(nullable=False, default=1)
       # 每次用户提问命中这条 FAQ，use_count +=1,统计哪些问题用户问的最多，运营可以优先优化高频 FAQ
       use_count: Mapped[int] = mapped_column(nullable=False, default=0)
       embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dim), nullable=True)
       #  用户输入问题 → 把用户问题转为向量 → 在 embedding 字段做向量相似度检索，找到知识库里面最相似的 FAQ，返回对应的 answer。也就是你项目里 AI 智能客服的知识库


# AI 对话的内容安全过滤
class SensitiveWord(Base, IdMixin, TimestampMixin):
       __tablename__ = "sensitive_words"

       word: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
       
       
       # in 拦用户输入 / out 拦 AI 输出（脱敏）/ allow 白名单短语
       word_type: Mapped[str] = mapped_column(String(10), nullable=False, default="in")
       remark: Mapped[str | None] = mapped_column(String(255), nullable=True)


# 用来保存 AI 对话里每一条消息
class ChatMessage(Base, IdMixin, TimestampMixin):
       __tablename__ = "chat_messages"

       __table_args__ = (
              Index("ix_chat_session_created", "session_id", "created_at"),
              Index("ix_chat_message_session_id", "session_id", "id", postgresql_where=text("deleted_at IS NULL")),
       )

       
       # ChatMessage 的 user_id 只是记录 “这条消息是谁发的”，不能用来判断用户有没有权限查看这个会话
       user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True, index=True)
       session_id: Mapped[str] = mapped_column(String(64), nullable=False)
       role: Mapped[str] = mapped_column(String(16), nullable=False)
       content: Mapped[str] = mapped_column(Text, nullable=False)
       refs: Mapped[list[dict] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)



# 会话的元信息、归属人、会话摘要状态、会话指针
# 会话归属权限，以 ChatSession.user_id 为准
class ChatSession(Base, IdMixin, TimestampMixin):
       
       __tablename__ = "chat_sessions"
       __table_args__ = (
              # summary_eligible = true：会话具备摘要资格
              # deleted_at IS NULL 没有软删除
              Index("ix_chat_summary_check", "summary_checked_at", "id",
                    postgresql_where=text("summary_eligible AND deleted_at IS NULL")),
       )
     
       session_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
       # ChatSession 的 user_id 可以进行权限判断
       user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True, index=True)
       
       
       """
       guest_secret_hash 是一次性游客认领会话凭证的 SHA256 哈希：
       游客进入聊天，后端生成一段随机原始字符串 secret（明文密钥），
       数据库只保存它的 sha256 哈希值 guest_secret_hash，原始 secret 不入库；
       把明文 secret 返回给前端（浏览器 / 客户端）。
       当游客要把这个临时聊天会话认领绑定到正式账号时，
       前端把明文 secret 传给服务端；服务端计算它的 sha256，
       和数据库里存的 guest_secret_hash 比对，匹配就认领成功，
       认领成功后立刻把数据库里这个 hash 字段改成 NULL，凭证作废，不能再用。
       """
       
       guest_secret_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
       
       # 标记会话的创建者：guest 或 user
       created_by: Mapped[str] = mapped_column(String(16), nullable=False, default="guest")

       # 当前会话的滚动摘要（只属于这一次对话）
       summary: Mapped[str | None] = mapped_column(Text, nullable=True)
       
       
       """
       summary_through_message_id表示：
       消息 ID 1~summary_through_message_id 的所有 ChatMessage，全部摘要完成并且保存进数据库
       下一次摘要任务，从 message_id = summary_through_message_id + 1 开始处理
       """
        
       summary_through_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
       last_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
       last_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
       
       # 这个会话成功生成摘要的次数。每成功生成一次 + 1
       """
       如果我修改了摘要的 prompt 模板，schema_version 升级。
       系统可以识别：旧摘要使用旧版本 prompt，需要全部重新生成。
       如果合并成一个字段，无法区分 “摘要生成次数” 和 “摘要模板版本”
       """
       
       summary_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
       summary_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
       
       # 只有用户真实发送消息新建会话时，才会标记 summary_eligible=True(允许定时任务生成摘要)
       summary_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
       summary_checked_through_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
       summary_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserMemory(Base, IdMixin, TimestampMixin):
       __tablename__ = "user_memories"
       __table_args__ = (
              # 同一个用户，同一个 key 只能有一条记录
              UniqueConstraint("user_id", "key", name="uq_user_memory_key"),
              CheckConstraint("key IN ('brand_preference', 'usual_use', 'answer_style')", name="ck_memory_key"),
       )

       user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True)
       key: Mapped[str] = mapped_column(String(32), nullable=False)
       value: Mapped[str] = mapped_column(String(120), nullable=False)
       source_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
       source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
       revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)


"""
ChatSession.summary：单次对话内的临时摘要，对话结束就不再更新；只服务当前这一个 session

UserMemory：跨多个会话、永久保存的用户个人偏好记忆，用户每次聊天 AI 都可以读取，长期记住用户信息



用户会话 A 聊天：“我只喜欢国产商品，回答简单一点”

ChatSession.summary：总结会话 A 的聊天内容
UserMemory 新增两条：
key=brand_preference, value = 偏爱国产商品，来源 session = 会话 A
key=answer_style, value = 回答尽量简短

用户过两天打开全新会话 B（新 session_id），AI 读取 UserMemory，自动沿用这些偏好，不需要重复告诉 AI。
"""
