"""
agent 的工具集。

两个刻意的写法：

1. 每个工具自己开 session，不复用请求的那个。FastAPI 的  Depends(get_db)
   在流式响应下会在响应体开始推送之前就执行完 teardown，
   所以 SSE 生成器里再用那个已关闭的 session 会炸。工具自己拿连接是唯一稳的。

2. 工具是工厂函数造出来的闭包，把 user_id 关进去。
   这样"查我的订单"这类工具天然只能看到当前登录用户的数据，
   不需要在提示词里恳求模型"请不要查别人的订单" —— 它拿不到别人的 id 会话。
"""

from langchain_core.tools import tool

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.core.text import segment_query
from app.services import faq_service, goods_service, order_service, search_service

settings = get_settings()

def build_tools(user_id: int | None) -> list:
    @tool(response_format="content_and_artifact")
    async def search_goods(
        keyword: str, brand: str | None = None, max_price: float | None = None,
        model: str | None = None,
    ) -> tuple[str, dict]:
        """
        按关键词搜索当前在售的商品，返回商品名、价格、库存。

        keyword 只放商品词（如"降噪耳机""手机""512G"），别把"2000元以内"整句塞进来
        —— 价格走 max_price 参数。
        brand 是品牌名（如"华为""小米"），会下推到 SQL 里做等值过滤。
        max_price 是价格上限，同样下推，在排序之前就生效，不是先取十条再筛。
        model 是用户明确指定的完整型号（如"Mate60 Pro"），独立于分词，严格匹配。
        不要把类别"手机"、系列简称或猜测的型号放进 model；未知型号不能用相似型号替代。
        """
        
        # 工具自己新建独立数据库 session，不依赖外层接口 Depends 的 db，适配 SSE 流式，防止 session 提前关闭报错
        # search_service.hybrid_search 这个函数会返回 商品列表，分词文本，降级处理；
        # 而 hits, _, _ 表明，只将 hits = 商品列表
        async with AsyncSessionLocal() as db:
            hits, _, _ = await search_service.hybrid_search(
                db, keyword, limit=6, brand_name=brand, max_price=max_price,
                model=model, semantic=True
            )
        
        # 把前面`segment_query`生成的`词1 & 词2`拆开，得到用户搜索的关键词列表
        terms = [t for t in segment_query(keyword).split(" & ") if t]
        
        
        # covered ：筛选全部关键词都出现在商品名称 / 简介 / 品牌文字里面的商品
        covered = [
            h for h in hits
            if all(t in f"{h.goods_name} {h.caption or ''} {h.brand_name or ''}".lower() for t in terms)
        ]
        
        # 从检索结果里挑出精确型号匹配的商品
        # exact : 精确匹配结果列表
        exact = [h for h in hits if "model_exact" in h.matched_by]
        
        
        # exact 优先级最高 ，covered 次之，hits 最后选
        picked = (exact + [h for h in covered if h not in exact]) if exact else (covered or hits)
        if not picked:
            return "没有找到匹配的商品。", {"refs": []}
        # 分层计数：有词面/型号依据的 vs 仅语义相近的。这一轮只统计、不改文案也不裁候选，
        # 判据用的是现有的 covered/exact（含它们已知的假阴性），目的是先把线上占比测出来。
        evidenced_ids = {h.id for h in exact} | {h.id for h in covered}
        hit_tiers = {
            "evidenced": sum(1 for h in picked if h.id in evidenced_ids),
            "semantic_only": sum(1 for h in picked if h.id not in evidenced_ids),
        }
        lines = [
            # 塞入prompt，方便大模型阅读
            # - id=12 华为Mate60 Pro手机｜型号Mate60Pro｜¥5499｜库存23｜5G卫星通信
            f"- id={h.id} {h.goods_name}｜型号{h.model_name or '未知'}｜¥{h.price}｜库存{h.stock}｜{h.caption or ''}".rstrip("｜")
            # 循环遍历 picked 里面每一个商品对象 h（SearchHit）
            for h in picked
        ]
        refs = [
            # 转换为字典，返回给前端 / 保存到日志 / 作为结构化引用数据
            {"type": "goods", "id": h.id, "name": h.goods_name, "price": str(h.price), "model_name": h.model_name}
            for h in picked
        ]
        return "\n".join(lines), {"refs": refs, "hit_tiers": hit_tiers}

    @tool(response_format="content_and_artifact")
    async def get_goods_detail(goods_id: int) -> tuple[str, dict]:
        """查看某个商品的详细信息：规格参数、库存、类目、图文介绍。需要先有商品 id。"""
        async with AsyncSessionLocal() as db:
            try:
                d = await goods_service.get_goods_detail(db, goods_id)
            except Exception as e:
                return f"查不到该商品：{e}", {"refs": []}
            
        if not d.is_marketable:   # is_marketable 是一个布尔值，表示商品是否在售
            # {"refs": []} 阻止大模型返回商品的规格参数、库存、类目、图文介绍等信息
            return f"该商品（id={goods_id}）已下架，不可购买。", {"refs": []}
        
        
        # for k, v in (d.spec_values or {}).items() 取出商品的规格参数，格式为：键:值
        spec = "、".join(f"{k}:{v}" for k, v in (d.spec_values or {}).items()) or "无"
        intro = (d.introduction or "")[:300]
        content = (
            f"id={d.id} {d.goods_name}\n型号 {d.model_name or '未知'}\n价格 ¥{d.price}\n库存 {d.stock}\n"
            f"品牌 {d.brand_name or '未知'}\n规格 {spec}\n简介 {intro}"
        )
        return content, {
            "refs": [{"type": "goods", "id": d.id, "name": d.goods_name,
                      "price": str(d.price), "model_name": d.model_name}]
        }

    @tool(response_format="content_and_artifact")
    async def search_faq(question: str) -> tuple[str, dict]:
        """
        查询平台规则类问题的标准答案：运费、退换货、发票、保修、发货时效、售后流程等。

        把用户的原始问题整句传进来，不要拆成关键词。
        """
        async with AsyncSessionLocal() as db:
            hits = await faq_service.search_faqs(db, question, limit=3)
            # search_faqs 里那条 use_count += 1 从来没生效过：这条 session 之前
            # 没人提交，async with 退出时直接回滚，统计字段从上线起就是死的。
            # 提交放在工具层而不是 service 里，因为 session 的生命周期归工具管。
            await db.commit()
        if not hits:
            # 明确告诉模型"没查到"，它才会走兜底话术而不是自己编一条规则
            return "知识库中没有找到相关规则，不要编造答案，请引导用户联系人工客服。", {"refs": []}
        # 这句放在工具返回值里而不是系统提示词里：模型读工具结果时的权重远高于
        # 几十行外的系统指令，而"从清单的沉默推断平台政策"正是在这里发生的
        # （实测：问「支持货到付款吗」，它答"清单里没有货到付款，所以本商城暂不支持"）。
        header = (
            "以下是检索到的平台规则条目，只能回答它们各自写明的内容。"
            "条目里没有写到的情况一律视为未知，禁止据此推断平台不支持、不提供或不允许。\n\n"
        )
        
        # 从项目配置文件读取一个相似度阈值，赋值给变量 low，用来判断检索出来的 FAQ 是不是可信度低
        low = settings.faq_low_confidence
        
        
        lines = [
            f"[{h.similarity:.2f}] Q:{h.question}"
            f"{'（相关度较低，未必适用于用户的提问）' if h.similarity < low else ''}\nA:{h.answer}"
            for h in hits
        ]
        refs = [
            {"type": "faq", "id": h.id, "question": h.question, "similarity": round(h.similarity, 3)}
            for h in hits
        ]
        return header + "\n\n".join(lines), {
            "refs": refs,
            # 原文只给回答侧护栏核对用：不进 refs、不落库、不给模型多看一分
            "evidence": [f"{h.question} {h.answer}" for h in hits],
        }

    @tool(response_format="content_and_artifact")
    async def list_my_orders(status: int | None = None) -> tuple[str, dict]:
        """列出当前登录用户的订单。status 可选：1未付款 2已付款 3已发货 4已收货 5完成 6退款 7关闭。"""
        if user_id is None:
            return "用户未登录，无法查询订单。请让用户先登录。", {"refs": []}
        async with AsyncSessionLocal() as db:
            rows = await order_service.list_orders(db, user_id, status=status, page=1, size=5)
        if not rows:
            return "该用户没有符合条件的订单。", {"refs": []}
        lines = [
            f"- id={o.id} 单号{o.order_no} 状态{o.status} 金额¥{o.total_price} 下单{o.created_at:%m-%d %H:%M}"
            for o in rows
        ]
        return "\n".join(lines), {
            "refs": [{"type": "order", "id": o.id, "order_no": o.order_no} for o in rows]
        }

    @tool(response_format="content_and_artifact")
    async def get_order_detail(order_id: int) -> tuple[str, dict]:
        """查看某个订单的详细状态和明细。需要先用 list_my_orders 拿到订单 id。"""
        if user_id is None:
            return "用户未登录，无法查询订单。", {"refs": []}
        async with AsyncSessionLocal() as db:
            try:
                order, items = await order_service.get_order_detail(db, user_id, order_id)
            except Exception:
                # 归属校验失败也是这里被吞掉，统一回"查不到"，不暴露是谁的订单
                return "查不到该订单，或该订单不属于当前用户。", {"refs": []}
        lines = [
            f"单号 {order.order_no}｜状态 {order.status}｜金额 ¥{order.total_price}",
            f"收货 {order.receiver_name} {order.receiver_phone} {order.receiver_address}",
            "明细：" + "；".join(f"{i.goods_name} x{i.quantity} @¥{i.goods_price}" for i in items),
        ]
        if order.close_reason:
            lines.append(f"关闭原因：{order.close_reason}")
        return "\n".join(lines), {"refs": [{"type": "order", "id": order.id, "order_no": order.order_no}]}

    return [search_goods, get_goods_detail, search_faq, list_my_orders, get_order_detail]
