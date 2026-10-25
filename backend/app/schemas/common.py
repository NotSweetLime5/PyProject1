from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """分页容器。和 BaseResult 一样是泛型，这样 Swagger 能渲染出
    BaseResult[Page[GoodsOut]] 的完整结构，而不是一个空 object。"""

    items: list[T]
    total: int
    page: int
    size: int

    @property
    def pages(self) -> int:
        return -(-self.total // self.size) if self.size else 0
