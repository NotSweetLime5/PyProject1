"""独立商品型号与 B-tree 精确检索。

Revision ID: g7b8model01
Revises: f6a7memory01
"""
from alembic import op
import sqlalchemy as sa

revision = "g7b8model01"
down_revision = "f6a7memory01"
branch_labels = None
depends_on = None

# 冻结的已知种子商品映射。只匹配完整标题，不猜测其他历史商品的型号。
_KNOWN_MODELS = (
    ("华为Mate60 Pro 智能手机", "Mate60 Pro"),
    ("华为Mate60 智能手机", "Mate60"),
    ("华为nova12 Ultra 手机", "nova12 Ultra"),
    ("华为FreeBuds Pro 3 无线耳机", "FreeBuds Pro 3"),
    ("华为Watch GT4 智能手表", "Watch GT4"),
    ("小米14 Ultra 影像旗舰手机", "小米14 Ultra"),
    ("小米14 智能手机", "小米14"),
    ("红米K70 Pro 手机", "红米K70 Pro"),
    ("小米手环9 Pro 智能穿戴", "小米手环9 Pro"),
    ("小米Air4 SE 蓝牙耳机", "Air4 SE"),
    ("Apple iPhone15 Pro Max 手机", "iPhone15 Pro Max"),
    ("Apple iPhone15 手机", "iPhone15"),
    ("Apple AirPods Pro 2 耳机", "AirPods Pro 2"),
    ("Apple Watch Series 10 手表", "Watch Series 10"),
    ("OPPO Find X7 Ultra 手机", "Find X7 Ultra"),
    ("OPPO Reno12 Pro 手机", "Reno12 Pro"),
    ("vivo X100 Pro 手机", "X100 Pro"),
    ("荣耀Magic6 Pro 手机", "Magic6 Pro"),
    ("三星Galaxy S24 Ultra 手机", "Galaxy S24 Ultra"),
    ("联想ThinkPad X1 Carbon 笔记本", "ThinkPad X1 Carbon"),
    ("华硕天选5 游戏笔记本", "天选5"),
    ("戴尔XPS 14 轻薄本", "XPS 14"),
    ("索尼WH-1000XM5 头戴式耳机", "WH-1000XM5"),
    (" Bose QC45 头戴式降噪耳机", "QC45"),
    ("大疆Mini 4 Pro 无人机", "Mini 4 Pro"),
    ("佳能EOS R50 微单相机", "EOS R50"),
)


def upgrade():
    op.add_column("goods", sa.Column("model_name", sa.String(120), nullable=True))
    op.add_column("goods", sa.Column(
        "model_key", sa.Text(),
        sa.Computed(r"NULLIF(lower(translate(normalize(model_name, NFKC), E' \t\n\r\f\013', '')), '')", persisted=True),
        nullable=True,
    ))
    op.create_index("ix_goods_model_key", "goods", ["model_key"], unique=False)
    goods = sa.table("goods", sa.column("goods_name"), sa.column("model_name"))
    for title, model in _KNOWN_MODELS:
        op.execute(goods.update().where(goods.c.goods_name == title).values(model_name=model))


def downgrade():
    op.drop_index("ix_goods_model_key", table_name="goods")
    op.drop_column("goods", "model_key")
    op.drop_column("goods", "model_name")
