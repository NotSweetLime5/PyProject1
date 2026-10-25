"""三路混合检索的行为测试。

这些断言的价值不在"接口能返回 200"，而在**每一路各自负责什么**：
去掉任何一路，下面都会有一条测试挂掉。那才是混合检索真正被证明了。
"""

from conftest import first_goods_id, make_client, run, set_stock


def test_fulltext_hits_brand_and_spec_tokens():
    """品牌词和规格值都进了 search_text，所以能被全文命中。"""
    async def main():
        async with make_client() as c:
            r = await c.get("/api/v1/search", params={"q": "雅川青", "semantic": "false"})
            assert r.status_code == 200, r.text
            data = r.json()["data"]
            assert data["items"], "全文+模糊两路都没命中，检查 search_text 是否包含规格值"
            assert "雅川青" in data["items"][0]["goods_name"] + str(data["items"][0]["caption"])
            assert "fulltext" in data["items"][0]["matched_by"], data["items"][0]
            # segmented 是排障用的：搜不到东西时先看它是分词错了还是检索错了
            assert "雅川青" in data["segmented"]
    run(main())


def test_semantic_rescues_wording_fulltext_misses():
    """"拍照清晰的手机" 一个字面都不匹配，只有语义这一路能救。"""
    async def main():
        async with make_client() as c:
            off = await c.get("/api/v1/search", params={"q": "拍照清晰的手机", "semantic": "false"})
            on = await c.get("/api/v1/search", params={"q": "拍照清晰的手机", "semantic": "true"})
            assert on.status_code == 200, on.text
            assert on.json()["data"]["items"], "语义这一路没起作用"
            assert len(on.json()["data"]["items"]) > len(off.json()["data"]["items"]), (
                "关掉语义反而结果更多，说明阈值或融合有问题"
            )
            routes = {s for it in on.json()["data"]["items"] for s in it["matched_by"]}
            assert "semantic" in routes, routes
    run(main())


def test_filters_and_soft_delete_respected():
    async def main():
        gid = await first_goods_id("华为")
        async with make_client() as c:
            r = await c.get("/api/v1/search", params={"q": "手机", "brand_name": "华为"})
            items = r.json()["data"]["items"]
            assert items and all(i["brand_name"] == "华为" for i in items), [i["brand_name"] for i in items]

            # 下架商品必须从所有检索路里消失（三路 SQL 都带 is_marketable 条件）
            from sqlalchemy import text

            from app.core.database import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                await db.execute(text("UPDATE goods SET is_marketable=false WHERE id=:i"), {"i": gid})
                await db.commit()
            try:
                r = await c.get("/api/v1/search", params={"q": "手机", "limit": 50})
                assert gid not in [i["id"] for i in r.json()["data"]["items"]], "下架商品仍被搜到"
            finally:
                async with AsyncSessionLocal() as db:
                    await db.execute(text("UPDATE goods SET is_marketable=true WHERE id=:i"), {"i": gid})
                    await db.commit()
    run(main())


def test_embedding_outage_degrades_instead_of_failing():
    """向量服务不可用时搜索必须还能服务，并且把降级说出来。

    静默退回词面两路是最坏的情况：线上语义检索挂了一整天都没人知道。
    """
    async def main():
        import app.services.search_service as ss

        async def boom(_text):
            raise RuntimeError("embedding provider down")

        original = ss.embed_one
        ss.embed_one = boom
        try:
            async with make_client() as c:
                r = await c.get("/api/v1/search", params={"q": "降噪耳机", "limit": 5})
                assert r.status_code == 200, f"降级失败，直接 500：{r.status_code} {r.text[:200]}"
                data = r.json()["data"]
                assert data["degraded"] is True, data["degraded"]
                assert data["items"], "降级后应该有词面两路的结果"
                routes = {s for it in data["items"] for s in it["matched_by"]}
                assert "semantic" not in routes, routes
        finally:
            ss.embed_one = original
    run(main())


def test_sql_injection_attempt_is_harmless():
    """所有用户输入都走绑定参数，注入串只会被当成一个查不到的词。"""
    async def main():
        async with make_client() as c:
            before = await c.get("/api/v1/goods", params={"size": 1})
            total = before.json()["data"]["total"]
            r = await c.get("/api/v1/search", params={"q": "'; DROP TABLE goods; --"})
            assert r.status_code == 200, r.text
            after = await c.get("/api/v1/goods", params={"size": 1})
            assert after.json()["data"]["total"] == total, "goods 表被改了"
    run(main())


def test_detail_returns_images_and_breadcrumb():
    """详情页的两个新增字段：图片列表和类目名路径。

    category_names 是后端拼好的 —— 让前端拿三个裸 id 再发三次请求查名字，
    是把服务端的活推给客户端。
    """
    async def main():
        gid = await first_goods_id()
        async with make_client() as c:
            r = await c.get(f"/api/v1/goods/{gid}")
            assert r.status_code == 200, r.text
            d = r.json()["data"]
            assert d["images"], "详情没有图片，检查 seed 是否写入 goods_images"
            assert all(im["url"].startswith("/api/v1/goods/") for im in d["images"]), d["images"]
            assert d["category_names"], "面包屑为空，检查 category_names 拼装逻辑"
            assert d["header_pic"], "列表缩略图字段为空"
            # 图片必须真取得到，否则前端就是一堆裂图
            img = await c.get(d["images"][0]["url"])
            assert img.status_code == 200, img.status_code
            assert img.headers["content-type"].startswith("image/svg+xml"), img.headers.get("content-type")
    run(main())


def test_detail_and_image_404_for_missing_goods():
    async def main():
        async with make_client() as c:
            r = await c.get("/api/v1/goods/999999")
            assert r.status_code == 404 and r.json()["msg"] == "商品不存在", r.text
            r = await c.get("/api/v1/goods/999999/images/0.svg")
            assert r.status_code == 404, r.text
    run(main())


def test_image_svg_is_escaped_against_xss():
    """商品名会进 SVG 文本节点。含 XML 元字符的商品名不能破坏文档结构。"""
    async def main():
        from sqlalchemy import text as sa_text

        from app.core.database import AsyncSessionLocal

        gid = await first_goods_id()
        async with AsyncSessionLocal() as db:
            orig = await db.scalar(sa_text("SELECT goods_name FROM goods WHERE id=:i"), {"i": gid})
            evil = '</text><script>alert(1)</script>'
            await db.execute(
                sa_text("UPDATE goods SET goods_name=:n WHERE id=:i"), {"n": evil, "i": gid}
            )
            await db.commit()
        try:
            async with make_client() as c:
                r = await c.get(f"/api/v1/goods/{gid}/images/0.svg")
                assert r.status_code == 200, r.status_code
                assert "<script>" not in r.text, "商品名未转义，SVG 里可注入脚本"
                assert "&lt;script&gt;" in r.text, r.text[:200]
        finally:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    sa_text("UPDATE goods SET goods_name=:n WHERE id=:i"), {"n": orig, "i": gid}
                )
                await db.commit()
    run(main())


def test_sort_whitelist_rejects_arbitrary_columns():
    async def main():
        async with make_client() as c:
            r = await c.get("/api/v1/goods", params={"sort": "price"})
            assert r.status_code == 400, r.text
            r = await c.get("/api/v1/goods", params={"sort": "'; DROP TABLE goods; --"})
            assert r.status_code == 400, r.text
            r = await c.get("/api/v1/goods", params={"sort": "price_asc", "size": 5})
            prices = [float(i["price"]) for i in r.json()["data"]["items"]]
            assert prices == sorted(prices), prices
    run(main())


def test_max_price_is_filtered_before_the_limit():
    """价格上限必须在 SQL 里、取前 N 之前生效。

    这条断言是有区分度的，不是摆设：实测 q="手机" 无上限时 top6 是
    95/82/98/81/94/86，全都 >3400。所以"先取 top6 再在 Python 里筛"
    这种做法会返回 0 条，而下推会返回 87(3299)。
    """
    async def main():
        from app.core.database import AsyncSessionLocal
        from app.services import search_service

        async with AsyncSessionLocal() as db:
            wide, _, _ = await search_service.hybrid_search(db, "手机", limit=6)
            narrow, _, _ = await search_service.hybrid_search(
                db, "手机", limit=6, max_price=3400
            )
        assert 87 not in [h.id for h in wide], "top6 已含 87，这条测试失去区分度，请换查询"
        assert narrow and all(float(h.price) <= 3400 for h in narrow), [
            (h.id, str(h.price)) for h in narrow
        ]
        assert 87 in [h.id for h in narrow], "3299 的红米被价格上限挡掉了，说明没下推成功"
    run(main())


def test_search_goods_gate_drops_noncovering_but_never_empties():
    """工具层的属性覆盖门槛：不覆盖的踢掉，全不覆盖时退回原列表。

    这是评测集 goods-01 抓出来的真缺陷——199 元的无降噪耳机被检回，
    并被模型摆进「2000 元以内的降噪耳机」标题下。
    """
    async def main():
        from app.rag.tools import build_tools

        sg = next(t for t in build_tools(None) if t.name == "search_goods")

        _, art = await sg.coroutine(keyword="降噪耳机", max_price=2000.0)
        ids = {r["id"] for r in art["refs"]}
        assert 89 not in ids, f"无降噪的 89 仍在结果里：{ids}"
        assert ids == {83, 92, 103}, ids

        # 退回行为：库里没有任何商品写着"清晰"，门槛不能因此把结果清空
        _, art2 = await sg.coroutine(keyword="拍照清晰的手机")
        assert art2["refs"], "覆盖门槛把结果清空了，这是回归"

        # 品牌走 SQL，不走模型自觉
        _, art3 = await sg.coroutine(keyword="手机", brand="华为", max_price=6000.0)
        got = {r["id"] for r in art3["refs"]}
        assert got == {81, 82}, got

        # 候选分层计数：两档之和必须恒等于送进 prompt 的条数（不裁、不重、不丢）。
        # 这一档只测仪器本身，不测文案 —— 裁撤类改动要等占比数字出来再决定。
        for art_, label in ((art, "降噪耳机"), (art2, "拍照清晰的手机"), (art3, "华为手机")):
            t = art_["hit_tiers"]
            assert t["evidenced"] + t["semantic_only"] == len(art_["refs"]), (label, t, len(art_["refs"]))
        assert art["hit_tiers"] == {"evidenced": 3, "semantic_only": 0}, art["hit_tiers"]
        # art2 是兜底路径：terms 是 AND，'清晰' 没有任何商品写着 → 全部只能算仅语义相近
        assert art2["hit_tiers"]["evidenced"] == 0, art2["hit_tiers"]
    run(main())
