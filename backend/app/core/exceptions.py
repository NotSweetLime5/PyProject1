class BusException(Exception):
    """业务异常：抛出即代表"这次请求按业务规则不该成功"。"""

    def __init__(self, code: int, msg: str):
        self.code = code
        self.msg = msg
        super().__init__(msg)