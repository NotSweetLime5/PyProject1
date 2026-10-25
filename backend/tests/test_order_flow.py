"""购物车 → 下单 → 状态机 → 超时关单。

这里覆盖的是本项目里唯一"必须有并发正确性"的部分。
每条断言都对应一个真实故障模式，不是把接口走一遍。
"""

import asyncio
import random
import uuid

from conftest import count, first_goods_id, get_stock, make_client, run, set_stock

from app.models.cart import CartItem
from app.models.order import Order, OrderItem


def _u(tag):
    return f"tx_{tag}_{uuid.uuid4().hex[:6]}"


def _phone():
    return "13" + "".join(random.choice("0123456789") for _ in range(9))


async def _signup(c, tag):
    """返回 (token, user_id)。带上 user_id 是为了让断言能限定作用域 ——
    只数 token 就没法回答"这几笔订单是谁的"。"""
    name = _u(tag)
    r = await c.post("/api/v1/auth/register", json={"username": name, "password": "Passw0rd!x", "phone": _phone()})
    assert r.status_code == 200, r.text
    uid = r.json()["data"]["id"]
    r = await c.post("/api/v1/auth/login", json={"username": name, "password": "Passw0rd!x"})
    assert r.status_code == 200, r.text
    return r.json()["data"]["access_token"], uid


async def _address(c, tok, detail="科技园 1 号"):
    r = await c.post("/api/v1/addresses", headers={"Authorization": f"Bearer {tok}"}, json={
        "contact": "张三", "mobile": "13800000002", "province_name": "广东省",
        "city_name": "深圳市", "area_name": "南山区", "detail": detail})
    assert r.status_code == 200, r.text
    return r.json()["data"]["id"]


def test_cart_upsert_accumulates():
    """同一商品重复加购是数量累加，不是新增一行 —— 靠 uq_cart_user_goods 兜底。"""
    async def main():
        gid = await first_goods_id()
        async with make_client() as c:
            tok, _ = await _signup(c, "cart")
            H = {"Authorization": f"Bearer {tok}"}
            await c.post("/api/v1/cart", headers=H, json={"goods_id": gid, "quantity": 2})
            r = await c.post("/api/v1/cart", headers=H, json={"goods_id": gid, "quantity": 3})
            lines = r.json()["data"]
            mine = [x for x in lines if x["goods_id"] == gid]
            assert len(mine) == 1, f"同一商品出现 {len(mine)} 行，唯一约束没生效"
            assert mine[0]["quantity"] == 5, mine[0]
    run(main())


def test_cart_reports_price_change_but_order_uses_current():
    """快照价用于展示，成交价必须用实时价。反过来就会被用旧低价下单。"""
    async def main():
        gid = await first_goods_id()
        from sqlalchemy import text

        from app.core.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            orig = await db.scalar(text("SELECT price FROM goods WHERE id=:i"), {"i": gid})
        async with make_client() as c:
            tok, _ = await _signup(c, "price")
            H = {"Authorization": f"Bearer {tok}"}
            await c.post("/api/v1/cart", headers=H, json={"goods_id": gid, "quantity": 1})
            async with AsyncSessionLocal() as db:
                await db.execute(text("UPDATE goods SET price=88888.88 WHERE id=:i"), {"i": gid})
                await db.commit()
            try:
                line = (await c.get("/api/v1/cart", headers=H)).json()["data"][0]
                assert line["price_changed"] is True, line
                assert float(line["goods_price"]) != float(line["current_price"]), line
                assert float(line["current_price"]) == 88888.88, line

                aid = await _address(c, tok)
                cid = line["id"]
                r = await c.post("/api/v1/orders", headers=H,
                                 json={"cart_item_ids": [cid], "address_id": aid})
                assert r.status_code == 200, r.text
                order = r.json()["data"]
                assert float(order["total_price"]) == 88888.88, order["total_price"]
                assert float(order["items"][0]["goods_price"]) == 88888.88, order["items"][0]
            finally:
                async with AsyncSessionLocal() as db:
                    await db.execute(text("UPDATE goods SET price=:p WHERE id=:i"), {"p": orig, "i": gid})
                    await db.commit()
    run(main())


def test_order_deducts_stock_and_clears_cart():
    async def main():
        gid = await first_goods_id()
        await set_stock(gid, 40)
        async with make_client() as c:
            tok, _ = await _signup(c, "ord")
            H = {"Authorization": f"Bearer {tok}"}
            await c.post("/api/v1/cart", headers=H, json={"goods_id": gid, "quantity": 7})
            cart = (await c.get("/api/v1/cart", headers=H)).json()["data"]
            aid = await _address(c, tok)
            r = await c.post("/api/v1/orders", headers=H,
                             json={"cart_item_ids": [cart[0]["id"]], "address_id": aid})
            assert r.status_code == 200, r.text
            order = r.json()["data"]
            assert len(order["order_no"]) == 20 and order["order_no"].isdigit(), order["order_no"]
            assert order["status"] == 1
            assert await get_stock(gid) == 33, "库存没有按数量扣减"
            # 下单后购物车行必须物理删除，否则会被重复结算
            left = (await c.get("/api/v1/cart", headers=H)).json()["data"]
            assert cart[0]["id"] not in [x["id"] for x in left], "已下单的行还在购物车里"
    run(main())


def test_receiver_info_is_snapshot_not_reference():
    """用户删掉地址后，历史订单必须仍然显示下单当时的收件信息。"""
    async def main():
        gid = await first_goods_id()
        await set_stock(gid, 40)
        async with make_client() as c:
            tok, _ = await _signup(c, "snap")
            H = {"Authorization": f"Bearer {tok}"}
            aid = await _address(c, tok, detail="体育西路 9 号")
            await c.post("/api/v1/cart", headers=H, json={"goods_id": gid, "quantity": 1})
            cid = (await c.get("/api/v1/cart", headers=H)).json()["data"][0]["id"]
            r = await c.post("/api/v1/orders", headers=H, json={"cart_item_ids": [cid], "address_id": aid})
            oid = r.json()["data"]["id"]
            assert r.json()["data"]["receiver_address"].endswith("体育西路 9 号")

            assert (await c.delete(f"/api/v1/addresses/{aid}", headers=H)).status_code == 200
            detail = await c.get(f"/api/v1/orders/{oid}", headers=H)
            assert detail.status_code == 200, detail.text
            assert detail.json()["data"]["receiver_address"].endswith("体育西路 9 号"), (
                "地址删了之后订单收件信息也丢了 —— 说明用了外键而不是快照")
    run(main())


def test_concurrent_orders_cannot_oversell():
    """N 个用户同时抢有限库存：成功数必须恰好等于库存数，且库存不为负。

    这是本项目最值得测的一条。"先 SELECT 判断库存再 UPDATE" 的写法在这里必挂，
    而它在单人手工测试下永远是对的。
    """
    async def main():
        gid = await first_goods_id()
        STOCK = 3
        BUYERS = 10
        await set_stock(gid, STOCK)
        async with make_client() as c:
            signed = [await _signup(c, f"race{i}") for i in range(BUYERS)]
            tokens = [t for t, _ in signed]
            race_uids = [u for _, u in signed]

            async def buy(tok):
                H = {"Authorization": f"Bearer {tok}"}
                r = await c.post("/api/v1/cart", headers=H, json={"goods_id": gid, "quantity": 1})
                if r.status_code != 200:
                    return r.status_code
                cid = r.json()["data"][0]["id"]
                aid = await _address(c, tok)
                rr = await c.post("/api/v1/orders", headers=H,
                                  json={"cart_item_ids": [cid], "address_id": aid})
                return rr.status_code

            results = await asyncio.gather(*(buy(t) for t in tokens))

        ok = sum(1 for s in results if s == 200)
        assert ok == STOCK, f"成功 {ok} 笔但库存只有 {STOCK}：{results}"
        assert await get_stock(gid) == 0, "库存变成负数或被重复扣减"
        # 必须按本次测试的用户作用域计数。
        # 原来写的是 count(Order) == STOCK —— 单独跑能过，放进整套里就变成 6 != 3，
        # 因为前面的测试也建了订单。全局计数断言在共享数据库里一律是错的。
        assert await count(Order, Order.user_id.in_(race_uids)) == ok == STOCK, (
            "订单数和成功数不一致")
        assert await count(OrderItem) >= STOCK
    run(main())


def test_idempotency_key_replays_same_order():
    async def main():
        gid = await first_goods_id()
        await set_stock(gid, 40)
        async with make_client() as c:
            tok, uid = await _signup(c, "idem")
            H = {"Authorization": f"Bearer {tok}"}
            aid = await _address(c, tok)
            await c.post("/api/v1/cart", headers=H, json={"goods_id": gid, "quantity": 2})
            cid = (await c.get("/api/v1/cart", headers=H)).json()["data"][0]["id"]
            key = str(uuid.uuid4())
            body = {"cart_item_ids": [cid], "address_id": aid}

            first = await c.post("/api/v1/orders", headers={**H, "Idempotency-Key": key}, json=body)
            assert first.status_code == 200, first.text
            order1 = first.json()["data"]
            stock_after_first = await get_stock(gid)

            # 重放时故意带一个不同的 body（模拟用户又点了别的东西）：
            # 命中幂等必须返回第一次的结果，而不是按新 body 再下一单。
            # 这里刻意真的换一个 body —— 上一版这条用例传的是同一个 body，
            # 注释承诺的性质其实没被测到（"文档强于代码"在本项目已登记过两次）。
            await c.post("/api/v1/cart", headers=H, json={"goods_id": gid, "quantity": 3})
            cid2 = (await c.get("/api/v1/cart", headers=H)).json()["data"][0]["id"]
            assert cid2 != cid, "没拿到另一条购物车行，这条用例没有区分度"

            again = await c.post("/api/v1/orders", headers={**H, "Idempotency-Key": key},
                                 json={"cart_item_ids": [cid2], "address_id": aid})
            assert again.status_code == 200, again.text
            data = again.json()["data"]
            assert data["id"] == order1["id"], "幂等没返回同一笔订单"
            assert float(data["total_price"]) == float(order1["total_price"]), "返回的订单被新 body 改写了"
            assert await get_stock(gid) == stock_after_first, "库存被扣了第二次"
            # 最直接的证据：这个用户名下只能有一张订单，且新 body 那行购物车没被结算掉
            assert await count(Order, Order.user_id == uid) == 1, "新 body 被当成新订单执行了"
            left = (await c.get("/api/v1/cart", headers=H)).json()["data"]
            assert cid2 in [x["id"] for x in left], "重放请求把未结算的购物车行清掉了"
    run(main())


def test_order_state_machine_and_cancel_restores_stock():
    async def main():
        gid = await first_goods_id()
        await set_stock(gid, 40)
        async with make_client() as c:
            owner, _ = await _signup(c, "own")
            other, _ = await _signup(c, "oth")
            HO = {"Authorization": f"Bearer {owner}"}
            HX = {"Authorization": f"Bearer {other}"}
            aid = await _address(c, owner)
            await c.post("/api/v1/cart", headers=HO, json={"goods_id": gid, "quantity": 5})
            cid = (await c.get("/api/v1/cart", headers=HO)).json()["data"][0]["id"]
            r = await c.post("/api/v1/orders", headers=HO, json={"cart_item_ids": [cid], "address_id": aid})
            oid = r.json()["data"]["id"]
            assert await get_stock(gid) == 35

            # 越权：别人的订单必须 404，且不能真的改动它
            assert (await c.post(f"/api/v1/orders/{oid}/pay", headers=HX)).status_code == 404
            assert (await c.get(f"/api/v1/orders/{oid}", headers=HX)).status_code == 404
            assert (await c.get(f"/api/v1/orders/{oid}", headers=HO)).json()["data"]["status"] == 1

            paid = await c.post(f"/api/v1/orders/{oid}/pay", headers=HO)
            assert paid.status_code == 200 and paid.json()["data"]["pay_time"], paid.text
            assert (await c.post(f"/api/v1/orders/{oid}/pay", headers=HO)).status_code == 409
            assert (await c.post(f"/api/v1/orders/{oid}/cancel", headers=HO)).status_code == 409, "已付款不该能取消"

            # 另一笔未付款订单：取消要回补库存
            await c.post("/api/v1/cart", headers=HO, json={"goods_id": gid, "quantity": 4})
            cid2 = [x for x in (await c.get("/api/v1/cart", headers=HO)).json()["data"] if x["goods_id"] == gid][0]["id"]
            r2 = await c.post("/api/v1/orders", headers=HO, json={"cart_item_ids": [cid2], "address_id": aid})
            oid2 = r2.json()["data"]["id"]
            assert await get_stock(gid) == 31
            cancel = await c.post(f"/api/v1/orders/{oid2}/cancel", headers=HO)
            assert cancel.status_code == 200 and cancel.json()["data"]["status"] == 7
            assert await get_stock(gid) == 35, "取消后库存没有回补"
    run(main())


def test_timeout_close_restores_stock_and_is_idempotent():
    async def main():
        from sqlalchemy import text

        from app.core.database import AsyncSessionLocal
        from app.services.order_service import close_expired_orders

        gid = await first_goods_id()
        await set_stock(gid, 40)
        async with make_client() as c:
            tok, _ = await _signup(c, "late")
            H = {"Authorization": f"Bearer {tok}"}
            aid = await _address(c, tok)
            await c.post("/api/v1/cart", headers=H, json={"goods_id": gid, "quantity": 6})
            cid = (await c.get("/api/v1/cart", headers=H)).json()["data"][0]["id"]
            r = await c.post("/api/v1/orders", headers=H, json={"cart_item_ids": [cid], "address_id": aid})
            oid = r.json()["data"]["id"]

        async with AsyncSessionLocal() as db:
            await db.execute(
                text("UPDATE orders SET created_at = now() - interval '2 hours' WHERE id=:i"), {"i": oid}
            )
            await db.commit()

        closed = await close_expired_orders()
        assert closed >= 1

        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                text("SELECT status, close_reason FROM orders WHERE id=:i"), {"i": oid}
            )).first()
        assert row[0] == 7, row
        assert row[1] == "超时未支付，系统自动关闭", row
        assert await get_stock(gid) == 40, "关单后库存没回补"

        # 再跑一次不能重复回补（否则库存凭空变多）
        await close_expired_orders()
        assert await get_stock(gid) == 40, "重复执行关单任务导致库存多加了一次"

        async with make_client() as c:
            tok, _ = await _signup(c, "late2")
            H = {"Authorization": f"Bearer {tok}"}
            r = await c.post(f"/api/v1/orders/{oid}/pay", headers=H)
            assert r.status_code in (404, 409), r.text
    run(main())


def test_one_default_address_enforced():
    async def main():
        async with make_client() as c:
            tok, _ = await _signup(c, "addr")
            H = {"Authorization": f"Bearer {tok}"}
            ids = []
            for i in range(3):
                r = await c.post("/api/v1/addresses", headers=H, json={
                    "contact": f"收件人{i}", "mobile": "13800000001", "province_name": "广东省",
                    "city_name": "深圳市", "area_name": "南山区", "detail": f"路 {i} 号",
                    "is_default": i == 0})
                ids.append(r.json()["data"]["id"])
            await c.post(f"/api/v1/addresses/{ids[2]}/default", headers=H)
            rows = (await c.get("/api/v1/addresses", headers=H)).json()["data"]
            assert [a["id"] for a in rows if a["is_default"]] == [ids[2]], "默认地址不止一个"
            assert rows[0]["is_default"] is True, "默认地址没排在最前"

            # 绕过应用层直接写库，部分唯一索引必须拦住第二个默认
            from sqlalchemy import exc as sa_exc
            from sqlalchemy import text

            from app.core.database import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                try:
                    await db.execute(text("UPDATE addresses SET is_default=true WHERE id=:i"), {"i": ids[1]})
                    await db.commit()
                    raised = False
                except sa_exc.IntegrityError:
                    await db.rollback()
                    raised = True
            assert raised, "uq_address_one_default 没拦住第二个默认地址"
    run(main())


def test_concurrent_cancel_restores_stock_exactly_once():
    """同一笔订单被并发取消两次：只能一次生效，库存只能加一次。

    修前的形状是「SELECT 判状态 → 无条件 stock+qty → 无条件改状态」：两个事务都通过那个 if，
    后到的那个醒来时它的 UPDATE 里没有任何条件，照样又把库存加了一遍。
    这条是 4.6 那个故障 A 的回归用例。
    """
    async def main():
        gid = await first_goods_id()
        await set_stock(gid, 40)
        async with make_client() as c:
            tok, _ = await _signup(c, "dblcancel")
            H = {"Authorization": f"Bearer {tok}"}
            aid = await _address(c, tok)
            await c.post("/api/v1/cart", headers=H, json={"goods_id": gid, "quantity": 5})
            cid = (await c.get("/api/v1/cart", headers=H)).json()["data"][0]["id"]
            r = await c.post("/api/v1/orders", headers=H, json={"cart_item_ids": [cid], "address_id": aid})
            oid = r.json()["data"]["id"]
            assert await get_stock(gid) == 35, "下单没扣够库存，后面测不出回补次数"

            codes = [x.status_code for x in await asyncio.gather(
                *(c.post(f"/api/v1/orders/{oid}/cancel", headers=H) for _ in range(2)))]
            assert sorted(codes) == [200, 409], codes
            assert await get_stock(gid) == 40, f"库存回补次数不对：{codes}"
            assert await count(OrderItem, OrderItem.order_id == oid) == 1
    run(main())


def test_pay_and_cancel_race_leaves_status_and_stock_consistent():
    """付款和取消同时进来：谁赢都行，但状态和库存必须自洽（4.6 的故障 B）。

    这里刻意断**不变量**而不是赢家 —— 并发本身没有"应该谁赢"这回事，
    把赢家写进断言的用例会随机翻绿翻红，那是判据的缺陷，不是被测代码的。
    """
    async def main():
        from sqlalchemy import text

        from app.core.database import AsyncSessionLocal

        gid = await first_goods_id()
        await set_stock(gid, 40)
        async with make_client() as c:
            tok, _ = await _signup(c, "racepay")
            H = {"Authorization": f"Bearer {tok}"}
            aid = await _address(c, tok)
            await c.post("/api/v1/cart", headers=H, json={"goods_id": gid, "quantity": 6})
            cid = (await c.get("/api/v1/cart", headers=H)).json()["data"][0]["id"]
            r = await c.post("/api/v1/orders", headers=H, json={"cart_item_ids": [cid], "address_id": aid})
            oid = r.json()["data"]["id"]
            assert await get_stock(gid) == 34

            codes = sorted(x.status_code for x in await asyncio.gather(
                c.post(f"/api/v1/orders/{oid}/pay", headers=H),
                c.post(f"/api/v1/orders/{oid}/cancel", headers=H)))
            assert codes == [200, 409], codes

            async with AsyncSessionLocal() as db:
                row = (await db.execute(
                    text("SELECT status, pay_time, close_reason FROM orders WHERE id=:i"), {"i": oid}
                )).first()
        status, pay_time, close_reason = row
        after = await get_stock(gid)
        assert status in (2, 7), status
        if status == 2:
            assert pay_time is not None and after == 34, "判成已付款，库存却被回补了"
        else:
            assert close_reason == "用户主动取消" and after == 40, "已取消却没把库存还回去"
    run(main())


def test_cart_items_are_locked_in_ascending_goods_order():
    """扣库存循环的加锁顺序必须按 goods_id 升序。

    死锁的成因是"两个事务按不同顺序拿同一批行锁"，所以这条测的是**顺序本身**而不是结果 ——
    结果（库存对不对）在并发下由 4.3 那条竞态测试管。
    做法：先加购 goods_id 大的那件、再加购小的那件，让购物车行的插入顺序与 goods 顺序相反。
    订单明细跟着循环顺序拿到自增 id，于是"按 OrderItem.id 排序读出的 goods_id 序列"
    就是当时循环的实际顺序：修之前它等于购物车插入顺序（降序），修之后必须是升序。

    它没有测到真实死锁（那需要精确控制交错时序，成本高得多）；它测的是那条规则确实生效 ——
    有人把 order_by 删掉，这条立刻变红。
    """
    async def main():
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal
        from app.models.goods import Goods

        async with AsyncSessionLocal() as db:
            ids = list(await db.scalars(
                select(Goods.id)
                .where(Goods.deleted_at.is_(None), Goods.is_marketable.is_(True))
                .order_by(Goods.id).limit(2)
            ))
        assert len(ids) == 2, "至少要两件在售商品才能测顺序"
        small, big = ids                      # small < big
        await set_stock(small, 40)
        await set_stock(big, 40)

        async with make_client() as c:
            tok, _ = await _signup(c, "lockorder")
            H = {"Authorization": f"Bearer {tok}"}
            await c.post("/api/v1/cart", headers=H, json={"goods_id": big, "quantity": 1})
            await c.post("/api/v1/cart", headers=H, json={"goods_id": small, "quantity": 1})
            cart = (await c.get("/api/v1/cart", headers=H)).json()["data"]
            by_goods = {x["goods_id"]: x["id"] for x in cart}
            aid = await _address(c, tok)
            # 提交的 cart_item_ids 也刻意先大后小，和购物车插入顺序保持一致
            r = await c.post("/api/v1/orders", headers=H,
                             json={"cart_item_ids": [by_goods[big], by_goods[small]], "address_id": aid})
            assert r.status_code == 200, r.text
            oid = r.json()["data"]["id"]

        async with AsyncSessionLocal() as db:
            seq = list(await db.scalars(
                select(OrderItem.goods_id).where(OrderItem.order_id == oid).order_by(OrderItem.id)
            ))
        assert seq == [small, big], f"加锁顺序没按 goods_id 升序：{seq}（small={small}, big={big}）"
    run(main())
