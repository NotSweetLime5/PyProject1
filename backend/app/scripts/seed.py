"""种子数据。跑法：python -m app.scripts.seed

必须和分词、embedding 一起写，不能提前用纯 SQL 灌 ——
search_text 要 jieba 分词、embedding 要调 API，纯 SQL 造出来的行会让阶段5 的
检索验证得到假结果（查不到东西时你分不清是分词错了还是检索错了）。
"""

import asyncio
import time

from sqlalchemy import delete, func, select

from app.core.database import AsyncSessionLocal
from app.core.embedding import embed_many_concurrent
from app.core.text import segment
from app.models.catalog import Category
from app.models.goods import Goods, GoodsImage

# (name, model_name, caption, brand, price, stock, spec_values, 三级类目 key)
# 型号来自结构化种子数据，不通过标题截取或分词猜测。
GOODS: list[tuple] = [
    ("华为Mate60 Pro 智能手机", "Mate60 Pro", "雅川青 12GB+512G 玄武架构", "华为", 6999.00, 120,
     {"颜色": "雅川青", "内存": "12GB+512G", "存储": "512G"}, "phone_huawei"),
    ("华为Mate60 智能手机", "Mate60", "白沙银 8GB+256G 卫星通话", "华为", 5499.00, 200,
     {"颜色": "白沙银", "内存": "8GB+256G"}, "phone_huawei"),
    ("华为nova12 Ultra 手机", "nova12 Ultra", "12GB+512G 前置双摄 拍照手机", "华为", 3999.00, 150,
     {"颜色": "12号色", "内存": "12GB+512G"}, "phone_huawei"),
    ("华为FreeBuds Pro 3 无线耳机", "FreeBuds Pro 3", "星闪耳机 主动降噪 长续航", "华为", 1499.00, 300,
     {"类型": "入耳式", "降噪": "主动降噪"}, "ear_huawei"),
    ("华为Watch GT4 智能手表", "Watch GT4", "46mm 蓝牙通话 血氧监测", "华为", 1288.00, 180,
     {"表盘": "46mm", "功能": "血氧监测"}, "watch_huawei"),
    ("小米14 Ultra 影像旗舰手机", "小米14 Ultra", "徕卡光学 骁龙8Gen3 16GB+1TB", "小米", 6499.00, 90,
     {"颜色": "黑色", "内存": "16GB+1TB"}, "phone_xiaomi"),
    ("小米14 智能手机", "小米14", "骁龙8Gen3 12GB+256G 小屏旗舰", "小米", 3999.00, 220,
     {"颜色": "白色", "内存": "12GB+256G"}, "phone_xiaomi"),
    ("红米K70 Pro 手机", "红米K70 Pro", "第二代骁龙8 2K直屏 120W快充", "小米", 3299.00, 260,
     {"颜色": "晴雪蓝", "内存": "16GB+512G"}, "phone_xiaomi"),
    ("小米手环9 Pro 智能穿戴", "小米手环9 Pro", "方屏大屏 血氧睡眠监测", "小米", 399.00, 500,
     {"类型": "手环"}, "watch_xiaomi"),
    ("小米Air4 SE 蓝牙耳机", "Air4 SE", "半入耳式 低延迟 长续航", "小米", 199.00, 800,
     {"类型": "半入耳式"}, "ear_xiaomi"),
    ("Apple iPhone15 Pro Max 手机", "iPhone15 Pro Max", "原色钛金属 A17 Pro 256GB", "苹果", 9999.00, 60,
     {"颜色": "原色钛金属", "内存": "256GB"}, "phone_apple"),
    ("Apple iPhone15 手机", "iPhone15", "黑色 128GB 双摄 灵动岛", "苹果", 5999.00, 140,
     {"颜色": "黑色", "内存": "128GB"}, "phone_apple"),
    ("Apple AirPods Pro 2 耳机", "AirPods Pro 2", "USB-C 版 主动降噪 空间音频", "苹果", 1899.00, 250,
     {"类型": "入耳式", "降噪": "主动降噪"}, "ear_apple"),
    ("Apple Watch Series 10 手表", "Watch Series 10", "46mm GPS 版 血氧心电图", "苹果", 3199.00, 100,
     {"表盘": "46mm"}, "watch_apple"),
    ("OPPO Find X7 Ultra 手机", "Find X7 Ultra", "哈苏四摄 16GB+512G 影像旗舰", "OPPO", 5999.00, 80,
     {"颜色": "海阔天空", "内存": "16GB+512G"}, "phone_oppo"),
    ("OPPO Reno12 Pro 手机", "Reno12 Pro", "AI 人像 12GB+256G 拍照手机", "OPPO", 3499.00, 160,
     {"颜色": "雾紫", "内存": "12GB+256G"}, "phone_oppo"),
    ("vivo X100 Pro 手机", "X100 Pro", "蔡司APO 长焦 16GB+512G", "vivo", 4999.00, 110,
     {"颜色": "落日橙", "内存": "16GB+512G"}, "phone_vivo"),
    ("荣耀Magic6 Pro 手机", "Magic6 Pro", "鹰眼相机 16GB+512G 青海湖电池", "荣耀", 5699.00, 95,
     {"颜色": "祁连雪", "内存": "16GB+512G"}, "phone_honor"),
    ("三星Galaxy S24 Ultra 手机", "Galaxy S24 Ultra", "骁龙8Gen3 AI手机 256GB", "三星", 9699.00, 70,
     {"颜色": "钛灰", "内存": "256GB"}, "phone_samsung"),
    ("联想ThinkPad X1 Carbon 笔记本", "ThinkPad X1 Carbon", "14英寸 酷睿Ultra7 32GB+1TB 商务本", "联想", 12999.00, 40,
     {"内存": "32GB", "硬盘": "1TB", "屏幕": "14英寸"}, "pc_lenovo"),
    ("华硕天选5 游戏笔记本", "天选5", "16英寸 RTX4060 酷睿i9 高刷电竞本", "华硕", 8999.00, 55,
     {"显卡": "RTX4060", "屏幕": "16英寸"}, "pc_asus"),
    ("戴尔XPS 14 轻薄本", "XPS 14", "酷睿Ultra7 32GB+512G OLED屏", "戴尔", 11999.00, 30,
     {"内存": "32GB", "屏幕": "OLED"}, "pc_dell"),
    ("索尼WH-1000XM5 头戴式耳机", "WH-1000XM5", "无线降噪耳机 长续航 Hires认证", "索尼", 2299.00, 130,
     {"类型": "头戴式", "降噪": "主动降噪"}, "ear_sony"),
    (" Bose QC45 头戴式降噪耳机", "QC45", "无线蓝牙 舒适佩戴 长途旅行", "Bose", 1999.00, 120,
     {"类型": "头戴式"}, "ear_bose"),
    ("大疆Mini 4 Pro 无人机", "Mini 4 Pro", "249g 迷你航拍机 全向避障", "大疆", 4788.00, 45,
     {"重量": "249g"}, "cam_dji"),
    ("佳能EOS R50 微单相机", "EOS R50", "APS-C画幅 4K视频 入门微单", "佳能", 5299.00, 35,
     {"画幅": "APS-C"}, "cam_canon"),
]

# key -> (三级类目名, 二级类目名, 一级类目名)
CAT_MAP: dict[str, tuple[str, str, str]] = {
    "phone_huawei": ("智能手机", "手机通讯", "数码电器"),
    "phone_xiaomi": ("智能手机", "手机通讯", "数码电器"),
    "phone_apple": ("智能手机", "手机通讯", "数码电器"),
    "phone_oppo": ("智能手机", "手机通讯", "数码电器"),
    "phone_vivo": ("智能手机", "手机通讯", "数码电器"),
    "phone_honor": ("智能手机", "手机通讯", "数码电器"),
    "phone_samsung": ("智能手机", "手机通讯", "数码电器"),
    "ear_huawei": ("蓝牙耳机", "影音娱乐", "数码电器"),
    "ear_xiaomi": ("蓝牙耳机", "影音娱乐", "数码电器"),
    "ear_apple": ("蓝牙耳机", "影音娱乐", "数码电器"),
    "ear_sony": ("蓝牙耳机", "影音娱乐", "数码电器"),
    "ear_bose": ("蓝牙耳机", "影音娱乐", "数码电器"),
    "watch_huawei": ("智能穿戴", "影音娱乐", "数码电器"),
    "watch_xiaomi": ("智能穿戴", "影音娱乐", "数码电器"),
    "watch_apple": ("智能穿戴", "影音娱乐", "数码电器"),
    "pc_lenovo": ("笔记本电脑", "电脑办公", "数码电器"),
    "pc_asus": ("笔记本电脑", "电脑办公", "数码电器"),
    "pc_dell": ("笔记本电脑", "电脑办公", "数码电器"),
    "cam_dji": ("无人机", "摄影摄像", "数码电器"),
    "cam_canon": ("微单相机", "摄影摄像", "数码电器"),
}


async def seed(reset: bool = True) -> None:
    async with AsyncSessionLocal() as db:
        existing = await db.scalar(select(func.count()).select_from(Goods))
        if existing and not reset:
            print(f"goods 已有 {existing} 行，跳过（要重置用 --reset）")
            return

        print("清空旧数据…")
        await db.execute(delete(GoodsImage))
        await db.execute(delete(Goods))
        await db.execute(delete(Category))

        # 拼出三级类目的去重路径，父先子后插入，好拿到 parent_id
        paths: list[tuple[str, str, str]] = []
        for p in CAT_MAP.values():
            if p not in paths:
                paths.append(p)
        paths.sort()

        cat_id: dict[str, int] = {}
        level_of: dict[str, int] = {}
        parent_of: dict[str, int | None] = {}
        for l3, l2, l1 in paths:
            for name, level, parent in ((l1, 1, None), (l2, 2, l1), (l3, 3, l2)):
                if name in cat_id:
                    continue
                node = Category(name=name, level=level, parent_id=parent_of.get(parent))
                db.add(node)
                await db.flush()
                cat_id[name] = node.id
                level_of[name] = level
                parent_of[name] = node.id

        print(f"类目 {len(cat_id)} 个（一级/二级/三级）")

        rows = []
        for name, model_name, caption, brand, price, stock, spec, cat_key in GOODS:
            l3, l2, l1 = CAT_MAP[cat_key]
            # 喂 tsvector 的是分词串：把能被检索的字段全部并进去。
            # 品牌、规格值不拼进去，用户搜"雅川青"就搜不到这台手机。
            haystack = " ".join(
                [name, caption, brand, *(str(v) for v in spec.values())]
            )
            rows.append(
                {
                    "goods_name": name,
                    "model_name": model_name,
                    "caption": caption,
                    "brand_name": brand,
                    "price": price,
                    "stock": stock,
                    "spec_values": spec,
                    "category1_id": cat_id[l1],
                    "category2_id": cat_id[l2],
                    "category3_id": cat_id[l3],
                    # header_pic 要用商品 id 拼 URL，而 id 要插入之后才有，
                    # 所以这里先留空，flush 之后再回填
                    "header_pic": None,
                    "search_text": segment(haystack),
                    # 喂 embedding 的是自然语言原句，不是分词串：
                    # 模型是在自然文本上训练的，"智能 能手 手机"这种碎片会拉低向量质量
                    "embed_text": haystack,
                }
            )

        t = time.perf_counter()
        vectors = await embed_many_concurrent([r["embed_text"] for r in rows])
        print(f"embedding {len(vectors)} 条，耗时 {time.perf_counter() - t:.1f}s")

        created: list[Goods] = []
        for r, vec in zip(rows, vectors, strict=True):
            r.pop("embed_text")
            r["embedding"] = vec
            g = Goods(**r)
            db.add(g)
            created.append(g)
        await db.flush()  # 拿到自增 id，图片 URL 要用

        # 每个商品 3 张图（主图 + 细节 + 场景），URL 指向后端按需生成的 SVG。
        # 不往仓库塞几十张二进制占位图：换一批商品就得重做，而且它们毫无信息量。
        for g in created:
            g.header_pic = f"/api/v1/goods/{g.id}/images/0.svg"
            for idx in range(3):
                db.add(
                    GoodsImage(
                        goods_id=g.id,
                        url=f"/api/v1/goods/{g.id}/images/{idx}.svg",
                        sort=idx,
                    )
                )
        await db.flush()
        print(f"商品 {len(created)} 行 + 图片 {len(created) * 3} 行已写入")
        await db.commit()


# (question, answer, category)  category: 0商品 1物流 2售后 3活动 99其他
FAQS: list[tuple[str, str, int]] = [
    ("下单之后多久发货？", "正常情况下，工作日 16:00 前完成的订单当天从深圳仓发出，16:00 后次日发出。"
     "大促期间发货时效延长至 48 小时。发出后可在「我的订单」查看物流单号。", 1),
    ("运费怎么算？多少金额免运费？", "全场单笔实付满 99 元免基础运费，未满 99 元收取 8 元运费。"
     "新疆、西藏、内蒙古等偏远地区满 199 元免运费，否则加收 12 元。大件商品（笔记本、显示器）单独计重。", 1),
    ("支持七天无理由退货吗？", "支持。商品签收之日起 7 天内，在不影响二次销售（未激活、配件赠品齐全、"
     "防伪标未撕毁）的前提下可申请无理由退货。手机类商品一旦联网激活即不支持无理由退货，质量问题除外。"
     "退货运费由承担方视责任判定：无理由由您承担，质量问题由平台承担。", 2),
    ("手机保修多久？", "手机、平板、笔记本整机保修 1 年，配件（充电器、数据线）保修 6 个月，"
     "耳机保修 3 个月，均自签收之日起算。保修期内非人为故障可在品牌官方售后点凭订单号维修。", 2),
    ("可以开发票吗？怎么开？", "可以，全部商品默认开具电子普通发票，下单时在结算页填写发票信息即可。"
     "如需增值税专用发票，请在下单后联系人工客服提供开票资质，一般在 3 个工作日内开出并发送到您填写的邮箱。", 2),
    ("订单没付款会自动关闭吗？", "会。未付款订单在创建后 30 分钟内未支付将自动关闭，"
     "占用的库存会同步释放回商品。关闭后您可以重新下单。", 0),
    ("怎么修改收货地址？", "订单未付款时可在「我的订单」里直接修改收货地址；"
     "已付款但未发货的订单，请联系人工客服协助修改；已经发货的订单无法修改地址，"
     "可以联系快递公司申请改派或拒收。", 1),
    ("优惠券怎么使用？可以和满减叠加吗？", "在结算页的「优惠」栏勾选可用优惠券，"
     "一笔订单只能使用一张平台券。平台券与店铺满减可以叠加，与限时直降不可叠加。"
     "优惠券有门槛和有效期，以券面说明为准。", 3),
    ("商品是正品吗？", "平台所售均为品牌授权渠道货源，支持品牌官方验真。"
     "如您对商品真伪存疑，可保留商品与包装并联系人工客服，由平台送检后按结果处理。", 0),
    ("刚买就降价了，能退差价吗？", "支持价格保护的商品在签收后 7 天内，若同一商品出现降价，"
     "可在订单详情申请一次差价补偿，差价以平台优惠券形式返还。"
     "限时秒杀、优惠券、赠品折算不计入比价范围。", 3),
    ("怎么联系人工客服？工作时间是？", "在客服对话窗口输入「转人工」即可排队接入人工客服。"
     "在线服务时间为每天 9:00-21:00；非服务时间可留言，客服会在次日 9:30 前回复。", 99),
    ("支持哪些支付方式？", "支持支付宝、微信支付、银联云闪付以及绑定的储蓄卡/信用卡。"
     "部分高单价商品支持花呗分期，分期期数和手续费以结算页显示为准。", 0),
    ("退货的钱多久到账？", "退货商品签收入库并质检通过后，1-3 个工作日内按原支付渠道退款。"
     "银行卡退款到账时间以发卡行为准，一般为 3-7 个工作日。优惠券抵扣部分不予返还现金。", 2),
    ("发货可以送到港澳台或者海外吗？", "目前仅支持中国大陆地区配送（不含港澳台及海外）。"
     "港澳台及海外订单暂不受理，给您带来不便请谅解。", 1),
    ("签收时需要注意什么？", "大件商品（笔记本、平板、显示器）请务必当面开箱验货，"
     "如发现外箱严重变形或商品破损，可直接拒收并在订单页申请售后。"
     "小件商品签收后 48 小时内反馈外观问题，平台一并受理。", 1),
]


async def seed_faqs(reset: bool = True) -> None:
    from app.core.embedding import embed_many_concurrent
    from app.models.custcare import Faq

    async with AsyncSessionLocal() as db:
        existing = await db.scalar(select(func.count()).select_from(Faq))
        if existing and not reset:
            print(f"faqs 已有 {existing} 行，跳过")
            return
        await db.execute(delete(Faq))

        # 逐条嵌会串行打 15 次请求；这里一次并发批量拿向量再落库
        vectors = await embed_many_concurrent([f"{q}。{a}" for q, a, _ in FAQS])
        for (q, a, cat), vec in zip(FAQS, vectors, strict=True):
            db.add(Faq(question=q, answer=a, category_id=cat, embedding=vec, status=1))
        await db.flush()
        await db.commit()
        print(f"FAQ {len(FAQS)} 条已写入（含向量）")


# in：绝不能进模型的 —— 只放人身攻击和违法诉求。
# 注意这里**没有**「骗子」「垃圾平台」「投诉你」：那是愤怒的客户，
# 拦下来等于赶客，正确做法是让 agent 按提示词安抚后回到问题本身。
# 也没有「假货」「恶意差评」：它们是正常业务名词，用户问"是不是假货"完全合理。
SENSITIVE_IN = [
    "傻逼", "操你妈", "信用卡套现", "代付套现", "帮刷单", "雇水军",
]
# out：不该由客服说出口的承诺。命中后脱敏，不整条撤回。
SENSITIVE_OUT = [
    "保证退款", "肯定赔偿", "绝对正品", "无条件全额退", "一定给您退全款", "官方已确认您是骗子",
]
# allow：白名单短语，用于在 in 命中后放行正常咨询
SENSITIVE_ALLOW = ["花呗套现是什么意思", "信用卡套现怎么防范"]


async def seed_sensitive() -> None:
    from app.core.sensitive import bump_version
    from app.models.custcare import SensitiveWord

    async with AsyncSessionLocal() as db:
        await db.execute(delete(SensitiveWord))
        for w in SENSITIVE_IN:
            db.add(SensitiveWord(word=w, word_type="in", remark="种子规则"))
        for w in SENSITIVE_OUT:
            db.add(SensitiveWord(word=w, word_type="out", remark="客服禁语"))
        for w in SENSITIVE_ALLOW:
            db.add(SensitiveWord(word=w, word_type="allow", remark="白名单"))
        await db.flush()
        await db.commit()
    await bump_version()
    print(f"敏感词 {len(SENSITIVE_IN)} in + {len(SENSITIVE_OUT)} out + {len(SENSITIVE_ALLOW)} allow")


if __name__ == "__main__":
    import sys

    if "--faqs" in sys.argv:
        asyncio.run(seed_faqs())
    elif "--sensitive" in sys.argv:
        asyncio.run(seed_sensitive())
    else:
        asyncio.run(seed(reset="--keep" not in sys.argv))
        asyncio.run(seed_faqs())
        asyncio.run(seed_sensitive())
