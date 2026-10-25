"""完整型号不依赖标题分词，精确结果优先且遵守所有可见性/业务过滤。"""
from contextlib import asynccontextmanager
import os
import uuid

import pytest
from sqlalchemy import delete, select, text

from conftest import make_client, run
from app.core.database import AsyncSessionLocal
from app.core.product_model import MODEL_KEY_SQL, normalize_model_key
from app.core.text import segment
from app.models.goods import Goods
from app.rag.tools import build_tools
from app.services import search_service


@pytest.mark.parametrize("value, expected", [
    (None, ""), ("", ""), (" \t\n", ""),
    ("Mate60 Pro", "mate60pro"), ("MATE60\t PRO\n", "mate60pro"),
    ("Ｍａｔｅ６０　Ｐｒｏ", "mate60pro"),
    ("Mate60\u2003Pro", "mate60pro"), ("Mate60\vPro", "mate60pro"),
    ("WH-1000XM5", "wh-1000xm5"), ("WH1000XM5", "wh1000xm5"),
    ("Mate60 Pro Max", "mate60promax"), ("小米14 Ultra", "小米14ultra"),
])
def test_python_and_generated_column_normalize_identically(value, expected):
    async def main():
        async with AsyncSessionLocal() as db:
            sql = f"SELECT {MODEL_KEY_SQL} FROM (SELECT CAST(:value AS text) AS model_name) AS input"
            actual = await db.scalar(text(sql), {"value": value})
        assert normalize_model_key(value) == expected
        assert actual == (expected or None)
    run(main())


@asynccontextmanager
async def catalog():
    suffix = uuid.uuid4().hex[:12]
    model = f"Verify{suffix} Pro"
    vector = [1.0] + [0.0] * 1023
    async with AsyncSessionLocal() as db:
        category = await db.scalar(select(Goods.category1_id).where(Goods.category1_id.is_not(None)).limit(1))
        base = dict(stock=10, brand_name="型号验收品牌", is_marketable=True, category1_id=category)
        rows = [
            # 标题与全文都不含型号，必须由独立字段召回。相同型号允许多个版本。
            Goods(goods_name="型号验收标准版", model_name=model, search_text="验收 标准版", price=100, **base),
            Goods(goods_name="型号验收高配版", model_name=model, search_text="验收 高配版", price=200, **base),
            # 与 query 的字面和语义都吻合，但结构化型号不同，不能排到精确命中前。
            Goods(goods_name=model, model_name=model + " Max", search_text=segment(model),
                  embedding=vector, price=80, **base),
        ]
        db.add_all(rows)
        await db.flush()
        ids = [g.id for g in rows]
        await db.commit()
    try:
        yield model, ids, category, vector
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Goods).where(Goods.id.in_(ids)))
            await db.commit()


def test_exact_model_beats_multi_route_distractor(monkeypatch):
    async def main():
        async with catalog() as (model, ids, _, vector):
            async def embed(_):
                return vector
            monkeypatch.setattr(search_service, "embed_one", embed)
            async with AsyncSessionLocal() as db:
                hits, _, _ = await search_service.hybrid_search(db, model, limit=1)
                assert hits[0].id in ids[:2]
                assert "model_exact" in hits[0].matched_by
                assert hits[0].model_name == model
                assert hits[0].model_key == normalize_model_key(model)
    run(main())


def test_model_lookup_without_tokenizer_dictionary_or_embedding(monkeypatch):
    async def main():
        async with catalog() as (model, ids, _, _):
            monkeypatch.setattr(search_service, "segment_query", lambda _: "")
            async def no_embedding(_):
                pytest.fail("semantic=false 不应调用 embedding")
            monkeypatch.setattr(search_service, "embed_one", no_embedding)
            async with AsyncSessionLocal() as db:
                hits, _, _ = await search_service.hybrid_search(db, model.swapcase().replace(" ", "\t"),
                                                               semantic=False)
            assert {h.id for h in hits if "model_exact" in h.matched_by} == set(ids[:2])
    run(main())


def test_explicit_model_obeys_filters_and_never_returns_sibling():
    async def main():
        async with catalog() as (model, ids, category, _):
            async with make_client() as client:
                params = {"q": "手机", "model": model, "semantic": "false", "category_id": category}
                response = await client.get("/api/v1/search", params=params)
                assert response.status_code == 200, response.text
                assert {h["id"] for h in response.json()["data"]["items"]} == set(ids[:2])
                for extra in ({"brand_name": "不存在品牌"}, {"category_id": 999999},
                              {"max_price": 50}, {"model": model + " Missing"}):
                    r = await client.get("/api/v1/search", params={**params, **extra})
                    assert r.status_code == 200, r.text
                    assert r.json()["data"]["items"] == []
                r = await client.get("/api/v1/search", params={**params, "max_price": 150})
                assert [h["id"] for h in r.json()["data"]["items"]] == [ids[0]]
                for column in ("is_marketable", "deleted_at"):
                    value = "false" if column == "is_marketable" else "CURRENT_TIMESTAMP"
                    async with AsyncSessionLocal() as db:
                        await db.execute(text(f"UPDATE goods SET {column}={value} WHERE id=:id"), {"id": ids[0]})
                        await db.commit()
                    r = await client.get("/api/v1/search", params=params)
                    assert ids[0] not in [h["id"] for h in r.json()["data"]["items"]]
                    async with AsyncSessionLocal() as db:
                        restored = "true" if column == "is_marketable" else "NULL"
                        await db.execute(text(f"UPDATE goods SET {column}={restored} WHERE id=:id"), {"id": ids[0]})
                        await db.commit()
    run(main())


def test_model_key_updates_with_model_name_and_is_not_unique():
    async def main():
        async with catalog() as (model, ids, _, _):
            async with AsyncSessionLocal() as db:
                assert list(await db.scalars(select(Goods.model_key).where(Goods.id.in_(ids[:2])))) == [
                    normalize_model_key(model), normalize_model_key(model)]
                g = await db.get(Goods, ids[0])
                g.model_name = " Ｍａｔｅ６０　Ｐｒｏ "
                await db.flush()
                await db.refresh(g)
                assert g.model_key == "mate60pro"
                g.model_name = None
                await db.flush()
                await db.refresh(g)
                assert g.model_key is None
                await db.commit()
    run(main())


def test_blank_and_injection_model_parameters():
    async def main():
        async with make_client() as c:
            r = await c.get("/api/v1/search", params={"q": "手机", "model": " \t ", "semantic": "false"})
            assert r.status_code == 400, r.text
            r = await c.get("/api/v1/search", params={"q": "手机", "model": "'; DROP TABLE goods; --", "semantic": "false"})
            assert r.status_code == 200 and r.json()["data"]["items"] == [], r.text
            assert (await c.get("/api/v1/goods")).status_code == 200
    run(main())


def test_agent_model_tool_preserves_exact_refs(monkeypatch):
    async def main():
        async with catalog() as (model, ids, _, vector):
            async def embed(_):
                return vector
            monkeypatch.setattr(search_service, "embed_one", embed)
            tool = next(t for t in build_tools(None) if t.name == "search_goods")
            assert "model" in tool.args
            content, artifact = await tool.coroutine(keyword="不存在的标题词", model=model, max_price=150)
            assert [r["id"] for r in artifact["refs"]] == [ids[0]]
            assert model in content
            assert artifact["refs"][0]["model_name"] == model
            detail = next(t for t in build_tools(None) if t.name == "get_goods_detail")
            _, detail_artifact = await detail.coroutine(goods_id=ids[0])
            # 后续详情查询会覆盖同商品的引用，必须继续保留正式型号。
            assert detail_artifact["refs"][0]["model_name"] == model
            _, missing = await tool.coroutine(keyword="手机", model=model + " Missing")
            assert missing["refs"] == []
    run(main())


def test_seed_models_are_backfilled_and_match_details():
    async def main():
        async with make_client() as c:
            r = await c.get("/api/v1/search", params={"q": "ＭＡＴＥ６０ＰＲＯ", "semantic": "false", "limit": 1})
            assert r.status_code == 200, r.text
            hit = r.json()["data"]["items"][0]
            assert hit["model_name"] == "Mate60 Pro"
            assert hit["model_key"] == "mate60pro" and "model_exact" in hit["matched_by"]
            d = await c.get(f"/api/v1/goods/{hit['id']}")
            assert d.json()["data"]["model_name"] == hit["model_name"]
    run(main())


@pytest.mark.llm
@pytest.mark.skipif(os.environ.get("RUN_LLM") != "1", reason="需要真实模型，设 RUN_LLM=1 开启")
def test_live_agent_uses_explicit_model_and_does_not_substitute(monkeypatch):
    from test_chat import _collect, _drop, _guest_ask

    calls = []
    original = search_service.hybrid_search

    async def observed(db, query, **kwargs):
        calls.append(kwargs.get("model"))
        return await original(db, query, **kwargs)

    monkeypatch.setattr(search_service, "hybrid_search", observed)

    async def main():
        async with make_client() as c:
            for model, exists in [("Mate60 Pro", True), ("Mate60 Pro Max", False)]:
                session = {}
                calls.clear()
                try:
                    r = await _guest_ask(c, f"请查询完整型号为 {model} 的手机价格和有货情况，只查这个完整型号，不需要推荐替代款。", session)
                    answer, _, refs, _, error = _collect(r.text)
                    assert error is None, error
                    assert any(normalize_model_key(v) == normalize_model_key(model) for v in calls), calls
                    goods = [ref for ref in (refs or []) if ref["type"] == "goods"]
                    if exists:
                        assert goods and all(ref.get("model_name") == model for ref in goods), (answer, goods)
                    else:
                        assert not goods, (answer, goods, calls)
                        assert any(word in answer for word in ("没", "未", "找不到", "查不到", "暂无")), answer
                finally:
                    if session.get("session_id"):
                        await _drop(session["session_id"])
    run(main())
