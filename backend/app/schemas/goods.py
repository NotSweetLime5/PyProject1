from decimal import Decimal

from pydantic import BaseModel, ConfigDict

"""序列化：将数据库中的orm对象转换为JSON格式"""


"""
分类树节点
其中
children: list["CategoryNode"] = []  ： 
将输入的orm对象转换为
node = CategoryNode(
    id=1, name="电子产品", level=1, parent_id=None,
    children=[                          
        CategoryNode(
            id=2, name="手机", level=2, parent_id=1,
这样前端收到的JSON就是树形结构
{
  "id": 1,
  "name": "电子产品",
  "level": 1,
  "parent_id": null,
  "children": [
    {
      "id": 2,
      "name": "手机",
写到 children: list[...] 这一行时，CategoryNode 类还在定义中，
Python 解释器执行到这行代码时内存里还没有 CategoryNode 这个名字。
不加引号会直接报 NameError，程序根本跑不起来。加引号告诉 Python："先当字符串存着，别现在解析"      
引号的本质作用是：延迟求值


CategoryNode.model_rebuild()  ： 重新归类，将 children 中的字符串转换为 CategoryNode 类型



model_config = ConfigDict(from_attributes=True)  ： Pydantic 默认只接受字典作为输入，
                                                    从数据库中查出orm对象需要先转换为字典，
                                                    使用这行代码，Pydantic 会自动去读 category.id、category.name 这些属性
"""

class CategoryNode(BaseModel):
    model_config = ConfigDict(from_attributes=True) 

    id: int
    name: str
    level: int
    parent_id: int | None
    children: list["CategoryNode"] = []


CategoryNode.model_rebuild()


class GoodsImageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str
    sort: int


class GoodsOut(BaseModel):

    model_config = ConfigDict(from_attributes=True)

    id: int
    goods_name: str
    model_name: str | None = None
    model_key: str | None = None
    caption: str | None
    price: Decimal
    stock: int
    brand_name: str | None
    header_pic: str | None
    is_marketable: bool

# 用于点进去查看商品详情
class GoodsDetailOut(GoodsOut):
    introduction: str | None
    spec_values: dict | None
    category1_id: int | None
    category2_id: int | None
    category3_id: int | None
    images: list[GoodsImageOut] = []
    # 面包屑用类目名，不是三个裸 id —— 否则前端要为了显示文字再发三次请求
    category_names: list[str] = []
