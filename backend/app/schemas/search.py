from pydantic import BaseModel

from app.schemas.goods import GoodsOut


# 搜索结果实体类
# GoodsOut ：商品对外输出模型，里面包含商品基础信息（id、名称、价格、品牌、图片等）
class SearchHit(GoodsOut):
    # 搜索得分
    score: float
    # 记录这条商品是通过哪些检索策略命中的 （ model_exact / fulltext / fuzzy / semantic ）
    matched_by: list[str]


class SearchResponse(BaseModel):
    query: str
    # 对用户的查询词做分词后的字符串
    # 调试时，先看接口返回的segmented，能立刻判断是分词错了还是检索错了
    segmented: str
    total: int
    # 搜索结果列表
    items: list[SearchHit]
    # 语义向量服务挂掉，超时，不可用的时候，置为True
    degraded: bool = False
