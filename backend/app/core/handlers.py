import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import BusException
from app.core.result import BaseResult

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    
    
    @app.exception_handler(BusException)
    async def _(request: Request, exc: BusException):
        return JSONResponse(
            status_code=exc.code,              # HTTP 状态码 = 业务码
            content=BaseResult.fail(exc.code, exc.msg).model_dump(),
        )



    @app.exception_handler(RequestValidationError)
    async def _(request: Request, exc: RequestValidationError):
        first = exc.errors()[0]
        loc = ".".join(str(part) for part in first["loc"][1:])
        return JSONResponse(
            status_code=400,
            content=BaseResult.fail(400, f"参数校验失败: {loc} {first['msg']}").model_dump(),
        )




    @app.exception_handler(SQLAlchemyError)
    async def _(request: Request, exc: SQLAlchemyError):
        logger.exception("数据库异常: %s", exc)
        return JSONResponse(
            status_code=500,
            content=BaseResult.fail(500, "数据库异常").model_dump(),
        )



    @app.exception_handler(Exception)
    async def _(request: Request, exc: Exception):
        logger.exception("未处理异常: %s", exc)
        return JSONResponse(
            status_code=500,
            content=BaseResult.fail(500, "服务器内部错误").model_dump(),
        )