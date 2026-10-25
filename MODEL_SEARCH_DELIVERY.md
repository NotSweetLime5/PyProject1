# 独立商品型号与精确检索

## 使用

搜索页直接输入 `Mate60Pro`、`Mate60 Pro`、`ＭＡＴＥ６０ＰＲＯ`，完整型号命中排在其他相关商品前，显示“型号精确匹配”。普通关键词和自然语言搜索保留。

接口支持严格型号过滤：

```text
GET /api/v1/search?q=手机&model=Mate60%20Pro&semantic=false
GET /api/v1/search?q=手机&model=Mate60%20Pro&max_price=7000&brand_name=华为&semantic=false
```

`model` 为完整型号，未找到时返回空结果，不返回相近型号。品牌、类目、价格、上架状态和软删除过滤在取候选之前执行。

不传 `model` 时，会将整个搜索词尝试作为型号键，精确命中优先，其余位置可补充全文、模糊或语义相关结果。这与严格型号过滤不同。

Agent 的 `search_goods` 增加可选 `model` 参数。用户明确指定完整型号时，Agent 应传入这个参数；工具返回真实型号，并在商品引用里记录 `model_name`。工具层不会用标题词覆盖门槛排除精确型号命中。

## 数据设计

- `model_name varchar(120)`：正式型号，可为空，独立于商品标题。
- `model_key text`：PostgreSQL 的 STORED 生成列，不分词，随 `model_name` 的 INSERT/UPDATE 自动生成。
- 规范化：Unicode NFKC、转小写、移除空格/制表符/换行等常用空白。保留连字符及 Pro/Max/Ultra 等后缀。
- `ix_goods_model_key`：普通非唯一 B-tree 索引。同一型号允许多个容量、颜色版本，也可能在不同品牌下重复。
- 型号信息在商品列表、详情及搜索结果中返回，搜索与详情页显示正式型号。

修改型号时只写 `model_name`，不要手工写生成列 `model_key`。标题全文和 embedding 的维护责任仍然存在；本轮没有增加商品管理后台。

种子脚本为 26 个商品提供显式型号。迁移 `g7b8model01`（父版本 `f6a7memory01`）仅根据冻结的完整种子标题映射回填已知商品；其他商品保持 NULL，不从标题猜型号。

标题全文仍使用 jieba 和历史自定义词典辅助召回；新增型号精确查询不依赖该词典，不需要逐个追加新型号词条。

## 排序与边界

型号等值查询是一条独立召回路径，命中来源为 `model_exact`。排序先比较是否精确命中，再比较 RRF 分数和商品 ID，所以不同优先级的结果不保证按 score 单独递减。

当前没有型号别名表，不会将 `WH1000XM5` 自动当作 `WH-1000XM5`，也不会将 `Mate60` 当作 `Mate60 Pro`。自然语言整句不会在搜索 API 内自动抽取型号；Agent 可通过独立参数表达用户的完整型号要求。

B-tree 支持这里的等值查询，不保证小表查询一定选择索引；实际查询计划由 PostgreSQL 决定。仍须安装 pgvector，无须增加中文分词数据库扩展。

## 验证与部署

专项确定性测试：19 passed。覆盖 Python/数据库型号键一致性、完整型号不依赖分词、精确优先、同型号多版本、名称更新自动生成键、严格过滤、下架/软删除、注入参数、工具引用和已知商品回填。前端生产构建通过。

完整回归后补齐详情工具的型号引用，避免同商品后续查询覆盖引用时丢失型号。最终型号专项（包含真实 Agent 用例）：20 passed。

开启真实模型的完整回归：154 passed、2 xfailed（66.48 秒），没有跳过用例。两项 xfail 为既有输出护栏局限，本轮未修改其语义。

新增真实 Agent 用例确认完整型号参数实际传入工具：Mate60 Pro 返回对应商品；不存在的 Mate60 Pro Max 不用其他型号替代。

迁移已在独立测试库完成 upgrade → downgrade → upgrade；未重新运行会重置商品的 seed。

业务库迁移前备份：`backend/backups/shopping_before_model_20261009.dump`。测试使用独立 PostgreSQL 副本 `shopping_model_test_20261009` 与 Redis 14 号数据库。

已在业务库应用 `g7b8model01`，26 个商品均回填独立型号，原有 3520 条聊天消息保留。8200 后端已启动，健康检查正常，5274 前端代理已验证：`Mate60Pro` 首条返回型号精确匹配；严格查询不存在的 `Mate60 Pro Max` 返回空；Mate60 Pro 加 6000 元价格上限返回空。独立测试数据库已清理。

主要阅读顺序：

1. `backend/app/core/product_model.py`
2. `backend/app/models/goods.py` 与 `backend/alembic/versions/g7b8model01_product_model.py`
3. `backend/app/services/search_service.py`
4. `backend/app/api/v1/search.py` 与 `backend/app/rag/tools.py`
5. `backend/tests/test_product_model.py`
