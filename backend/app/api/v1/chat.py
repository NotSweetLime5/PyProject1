import json
import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.api.deps import get_current_user, get_optional_user
from app.core.config import get_settings
from app.core.database import AsyncSessionLocal, get_db
from app.core.exceptions import BusException
from app.core.redis_ops import hit_rate_limit
from app.core.result import BaseResult, CodeEnum
from app.models.custcare import ChatMessage, ChatSession
from app.models.user import User
from app.schemas.chat import ChatAsk, ChatHistoryItem, ClaimIn, ClaimOut, SessionOut
from app.services import chat_service, session_store

settings = get_settings()
logger = logging.getLogger("chat.ownership")

router = APIRouter(prefix="/chat", tags=["AI客服"])

# 归属没通过时不给区分度：说"不存在"还是"secret 不对"，等于让探测者验证某个 sid
# 是否被签发过。三种情况前端要做的事是同一件 —— 丢掉本地那份重开。
_UNAVAILABLE_MSG = "会话已失效，请重新开始对话"


"""
payload: ChatAsk    前端上传的请求体
request: Request    原始请求对象
user: User | None    当前登录用户对象，游客为 None
"""

@router.post("/stream")
async def ask(
    payload: ChatAsk,
    request: Request,
    user: User | None = Depends(get_optional_user),
):
    """
    FastAPI 的 Depends 依赖，函数执行完毕就会清理资源。
    SSE 是长流： ask 主函数很快跑完，但是 event_source 生成器会长时间运行持续返回文字。
    如果在 ask 函数用 Depends(get_db) 拿到数据库 session， 
    ask 执行完 db 就被关闭，后面流式逻辑不能再用这个 db 连接。
    所以所有 DB 操作，业务代码自己手动创建 AsyncSessionLocal，用完就关闭
    """
    # 做基础分控，限流，请求校验
    await _guard(request, user)
    
    #  _resolve 函数 根据payload和当前用户user ，找到并校验对应的会话记录，返回会话对象sess
    sess = await _resolve(payload, user)
    if not sess.owner_ok and settings.chat_ownership_enforced:
        raise BusException(CodeEnum.SESSION_UNAVAILABLE, _UNAVAILABLE_MSG)

    async def event_source():
        """
        1. 执行 sess.as_frame()：把会话信息打包成字典
        2. 执行 _frame("session", 字典)：包装成浏览器认识的 SSE 消息文本
        3. yield：把这条消息推送到前端浏览器
        4. 推送完成，代码暂停，继续往下执行后面的 async for AI 流式循环
        """
        yield _frame("session", sess.as_frame())
        
        
        async for ev in chat_service.stream_answer(
            session_id=sess.session_id,
            user_id=user.id if user else None,
            message=payload.message,
        ):
            yield _frame(ev["type"], ev["data"])
        
        # ai 流式循环结束，发送 close 帧，通知前端关闭连接
        yield _frame("close", {"session_id": sess.session_id})

    return StreamingResponse(
        event_source(),
        
        # 告诉浏览器这个响应是SSE流式长连接
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",    # 防止浏览器缓存
            "Connection": "keep-alive",     # 保持长连接，避免重复建立连接
            
            # Nginx 默认会缓冲响应，加了这行才不会被它攒成一坨最后一次性吐出
            "X-Accel-Buffering": "no",
            # 同样一份签发结果在 header 里也放一次：给不走 SSE 解析的调用方
            # （curl、评测脚本）留个能拿到 sid 的口子。secret 只走帧，不进 header/日志。
            "X-Session-Id": sess.session_id,
        },
    )


async def _resolve(payload: ChatAsk, user: User | None) -> session_store.SessionContext:
    """
    核对归属 / 需要时签发。自己开 session，做完立刻关。
    commit 必须在这里完成，不能留给流的 teardown：新会话那行如果没落库，
    同一个 sid 的下一条消息就会被判成 unknown_sid。
    """
    async with AsyncSessionLocal() as db:
        sess = await session_store.ensure(
            db,
            session_id=payload.session_id,
            guest_secret=payload.guest_secret,
            user_id=user.id if user else None,
        )
        await db.commit()
    enforced = settings.chat_ownership_enforced
    if not sess.owner_ok:
        # 只记录阶段照样放行，但这条计数必须看得见 —— 否则没人知道什么时候
        # 可以翻开强制开关（翻早了现有前端全线 410，翻晚了这功能等于没做）。
        logger.warning(
            "ownership reason=%s enforced=%s allowed=%s sid=%s",
            sess.reason, enforced, not enforced, sess.session_id,
        )
    return sess


@router.post("/session/claim", response_model=BaseResult[ClaimOut])
async def claim_session(
    payload: ClaimIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """游客会话登录后认领到自己名下。幂等：重复调用不会改判成失败。

    401 只留给 JWT 问题（get_current_user）；缺 secret 由 schema 挡成 400；
    sid 不存在 / secret 不对 / 会话已属他人，一律 410，前端清掉本地那对值重开。
    """
    ok, reason = await session_store.claim(
        db,
        session_id=payload.session_id,
        guest_secret=payload.guest_secret,
        user_id=user.id,
    )
    if not ok:
        await db.rollback()
        raise BusException(CodeEnum.SESSION_UNAVAILABLE, _UNAVAILABLE_MSG)
    await db.commit()
    return BaseResult.ok(ClaimOut(session_id=payload.session_id, claimed=True, reason=reason))


@router.get("/history", response_model=BaseResult[list[ChatHistoryItem]])
async def history(
    session_id: str = Query(..., min_length=8, max_length=64),
    limit: int = Query(50, ge=1, le=200),
    guest_secret: str | None = Query(default=None, min_length=16, max_length=128),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """读自己的会话。游客凭 secret，登录用户凭 JWT，两者都没有就按老规矩办。

    secret 走查询串是有意识的取舍：它只在同一个标签页的 sessionStorage 里，
    生命周期以分钟计；换成自定义头会让每个历史请求多一次 CORS 预检。
    """
    sess = await session_store.ensure(
        db, session_id=session_id, guest_secret=guest_secret, user_id=user.id if user else None
    )
    if sess.owner_ok:
        conds = [ChatMessage.session_id == session_id, ChatMessage.deleted_at.is_(None)]
        if user is not None and not await _is_claimed(db, session_id, user.id):
            # 在【会话归属强制校验功能还没正式打开之前】，登录用户读取聊天历史，是依靠 chat_messages 表里每条消息上的 user_id 做兜底过滤。
            # 但是已经认领完成的会话，不能用这个兜底规则过滤消息！
            # 原因：认领之前游客发的消息，user_id 本来是 NULL。
            # 如果继续按消息的user_id过滤，登录前的所有聊天记录会直接查不到（被过滤丢掉）。
            # 所以认领的时候，我们提前把旧消息的user_id批量回填；这样读取历史就不再依靠消息user_id，改用session_id直接读取整条会话所有消息。
            conds.append(ChatMessage.user_id == user.id)
    elif settings.chat_ownership_enforced:
        raise BusException(CodeEnum.SESSION_UNAVAILABLE, _UNAVAILABLE_MSG)
    else:
        # 只记录阶段保持现有行为，一个字节都不改：游客本来就读不到历史，
        # 登录用户继续用 message 级的 user_id 过滤。
        # 这一行日志是"什么时候可以翻开强制开关"的唯一依据，不能省。
        logger.warning("history ownership reason=%s enforced=False sid=%s", sess.reason, session_id)
        if user is None:
            raise BusException(CodeEnum.UNAUTHORIZED, "请先登录后再查看历史会话")
        conds = [
            ChatMessage.session_id == session_id,
            ChatMessage.deleted_at.is_(None),
            ChatMessage.user_id == user.id,
        ]
    rows = list(
        await db.scalars(
            select(ChatMessage).where(*conds).order_by(ChatMessage.id.desc()).limit(limit)
        )
    )
    return BaseResult.ok([ChatHistoryItem.model_validate(r) for r in reversed(rows)])


async def _is_claimed(db: AsyncSession, session_id: str, user_id: int) -> bool:
    row = await db.scalar(
        select(ChatSession.user_id).where(ChatSession.session_id == session_id)
    )
    return row == user_id


@router.get("/sessions", response_model=BaseResult[list[SessionOut]])
async def sessions(
    user: User = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
    # offset 是给分页用的；不传就跟以前一样取最近 20 个。
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    if user is None:
        raise BusException(CodeEnum.UNAUTHORIZED, "请先登录")
    rows = await chat_service.list_sessions(db, user.id, limit=limit, offset=offset)
    return BaseResult.ok([SessionOut(**r) for r in rows])


def _frame(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _guard(request: Request, user: User | None) -> None:
    key = f"chat:{user.id if user else (request.client.host if request.client else 'anon')}"
    try:
        limited = await hit_rate_limit(key, settings.chat_rate_limit, settings.rate_limit_window_seconds)
    except Exception:
        limited = False
    if limited:
        raise BusException(CodeEnum.RATE_LIMITED, "提问过于频繁，请稍后再试")
