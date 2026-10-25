from pydantic import BaseModel, ConfigDict, Field, computed_field



"""
contact: 联系人姓名
"""

class AddressIn(BaseModel):
    contact: str = Field(min_length=1, max_length=50)
    mobile: str = Field(pattern=r"^1[3-9]\d{9}$")
    province_name: str = Field(min_length=1, max_length=50)
    city_name: str = Field(min_length=1, max_length=50)
    area_name: str = Field(min_length=1, max_length=50)
    detail: str = Field(min_length=2, max_length=255)
    zip_code: str | None = Field(default=None, max_length=10)
    is_default: bool = False


class AddressOut(BaseModel):
    # 允许 Pydantic 直接从 SQLAlchemy ORM 对象的属性中读取数据，而不是只从字典中读取
    model_config = ConfigDict(from_attributes=True)

    id: int
    contact: str
    mobile: str
    province_name: str
    city_name: str
    area_name: str
    detail: str
    zip_code: str | None
    is_default: bool
    
    
    
    
    # @computed_field  :    让一个动态计算的属性，在序列化输出时“显式”出来
    @computed_field
    @property
    def full_address(self) -> str:
        return f"{self.province_name}{self.city_name}{self.area_name}{self.detail}"
