"""会话归属：sid 由服务端签发，"谁能读这个会话"只看 chat_sessions。

为什么要把签发权收回来：chat_messages 的取数键就是 session_id，而它过去由浏览器
生成、服务端照单全收 —— 换一串字符就能读到别人的对话历史。归属信息必须在服务端，
所以签发也必须在服务端；客户端只可能带一个我们发过的 sid。

游客凭什么证明归属：靠 secret，不靠 user_id（游客本来就没有可依赖的身份）。
明文只在签发那一次通过 SSE 的 session 帧给出去，库里只留 sha256 摘要，
浏览器此后每次带原文过来比对。compare_digest 而不是 ==：摘要长度固定，
但相等比较仍会在第一个不匹配字节就返回，没必要留这个时序面。

认领后 guest_secret_hash 置 NULL：凭据一次性有效。之后就算 secret 泄漏在日志里，
也换不到任何读权限。
"""

import hashlib
import logging
import secrets
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.custcare import ChatMessage, ChatSession

logger = logging.getLogger("chat.ownership")

_SID_BYTES = 16
_SECRET_BYTES = 32

"""
frozen=True  : 冻结类，实例一旦创建，属性不能修改（只读）

只有【刚创建会话 + 当前是游客】这个场景，guest_secret才有值，其他全部是None
"""

@dataclass(frozen=True)     
class SessionContext:
    session_id: str
    created: bool
    # 明文只在 created=True 且是游客时非空；其余情况恒为 None
    guest_secret: str | None
    owner_ok: bool
    reason: str

    # 把会话上下文对象，转成前端 SSE 首帧要用的 JSON 字典。前端拿到这条首帧消息，完成会话初始化、本地存储更新、权限判断
    def as_frame(self) -> dict:
        """SSE session 帧的内容 —— 首帧，浏览器据此决定要不要覆盖本地那份。"""
        return {
            "session_id": self.session_id,
            "guest_secret": self.guest_secret,
            "created": self.created,
            "owner_ok": self.owner_ok,
            "reason": self.reason,
        }



"""
下划线开头代表内部私有函数，项目外部不要直接调用
secret 就是 guest_secret，游客的认领密钥明文（一串随机字符串）

函数作用  ： 输入明文 secret，计算它的 sha256 哈希摘要字符串并返回
"""

def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


async def ensure(
    db: AsyncSession, *, session_id: str | None, guest_secret: str | None, user_id: int | None
) -> SessionContext:
    """没有 sid 就签发一个；有 sid 就核对归属。不抛异常 —— 拒绝由调用方决定。"""
    if not session_id:
        return await _issue(db, user_id=user_id)

    row = await db.scalar(
        select(ChatSession).where(
            ChatSession.session_id == session_id, ChatSession.deleted_at.is_(None)
        )
    )
    if row is None:
        # 旧版前端存在 localStorage 里自造的 sid；也是换库/清库后的失配
        return _deny(session_id, "unknown_sid")
    ok, reason = _verify(row, guest_secret=guest_secret, user_id=user_id)
    if ok:
        return SessionContext(session_id, False, None, True, reason)
    return _deny(session_id, reason)


def _deny(session_id: str, reason: str) -> SessionContext:
    # 不对外暴露多余的数据库信息，仅用 reason 标记原因
    return SessionContext(session_id, False, None, False, reason)


async def _issue(db: AsyncSession, *, user_id: int | None) -> SessionContext:
    sid = secrets.token_hex(_SID_BYTES)
    # 登录用户不需要 secret：归属直接挂在 user_id 上，少一个能泄漏的东西
    secret = None if user_id is not None else secrets.token_urlsafe(_SECRET_BYTES)
    db.add(
        ChatSession(
            session_id=sid,
            user_id=user_id,
            guest_secret_hash=_digest(secret) if secret else None,
            created_by="user" if user_id is not None else "guest",
        )
    )
    await db.flush()
    return SessionContext(sid, True, secret, True, "issued")


# user_id  ： 本地登录用户的ID
# row.user_id ： 从数据库中查出，这个会话属于哪个用户ID
def _verify(row: ChatSession, *, guest_secret: str | None, user_id: int | None) -> tuple[bool, str]:
    if row.user_id is not None:
        if user_id is not None and user_id == row.user_id:
            return True, "owner"
        return False, "owner_mismatch"
    if user_id is not None:
        # 登录用户拿着一个还没认领的游客会话：先 claim，而不是服务端悄悄改写归属
        return False, "unclaimed_guest"
    if not guest_secret:
        return False, "secret_missing"
    if not row.guest_secret_hash:
        return False, "secret_revoked"
    if not secrets.compare_digest(_digest(guest_secret), row.guest_secret_hash):
        return False, "secret_mismatch"
    return True, "guest_secret"


async def claim(
    db: AsyncSession, *, session_id: str, guest_secret: str, user_id: int
) -> tuple[bool, str]:
    """把游客会话归到登录用户名下。幂等：重复调用结果一致，第二次返回 already_claimed。"""
    row = await db.scalar(
        select(ChatSession).where(
            ChatSession.session_id == session_id, ChatSession.deleted_at.is_(None)
        )
    )
    if row is None:
        return False, "unknown_sid"
    if row.user_id == user_id:
        return True, "already_claimed"
    if row.user_id is not None:
        # 会话已属于别人。secret 只证明"我是这个游客会话的持有人"，
        # 不能证明"我是认领它的那个登录用户"，所以这里不看 secret。
        return False, "claimed_by_other"
    if not row.guest_secret_hash or not secrets.compare_digest(
        _digest(guest_secret), row.guest_secret_hash
    ):
        return False, "secret_mismatch"

    row.user_id = user_id
    row.guest_secret_hash = None
    # 游客阶段的发言 user_id 是 NULL。不回填的话，登录后按 user 过滤的历史
    # 会把认领前的对话整段吞掉 —— 而 load_history 在归属校验强制开启前仍是兜底。
    await db.execute(
        update(ChatMessage)
        .where(ChatMessage.session_id == session_id, ChatMessage.user_id.is_(None))
        .values(user_id=user_id)
    )
    await db.flush()
    logger.info("session claimed session=%s user=%s", session_id, user_id)
    return True, "claimed"
