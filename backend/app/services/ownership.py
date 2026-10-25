from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusException
from app.core.result import CodeEnum


async def get_owned(
    db: AsyncSession, model: type, obj_id: int, user_id: int, msg: str = "资源不存在"
):
    """按 id + 归属人双条件取行，专治越权访问（IDOR）。
    「不存在」和「存在但不是你的」返回同一个 404。
    如果区分开，攻击者就能拿着一串自增 id 逐个试，
    用响应差异探测出哪些 id 属于别人 —— 等于把主键变成了信息泄露口。
    """
    row = await db.get(model, obj_id)
    if row is None or row.user_id != user_id:
        raise BusException(CodeEnum.NOT_FOUND, msg)
    if getattr(row, "deleted_at", None) is not None:
        raise BusException(CodeEnum.NOT_FOUND, msg)
    return row


"""
getattr这个函数是在row中寻找"deleted_at"这个属性，如果有就返回，没有就返回设置的默认值None

软删除的数据，对外表现为 “不存在”

model: type：接受任意 ORM 模型类，使得这个函数可以用于 Address、Order、Payment 等所有带 user_id 字段的表

obj_id：举个例子就是GET /api/order/123 中的 123

GET /api/user?obj_id=456
obj_id=456，就是去用户表查主键 id=456 的用户
"""
