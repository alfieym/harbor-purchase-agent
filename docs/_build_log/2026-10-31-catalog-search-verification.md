# Catalog 语义检索 + 硬约束过滤 验收记录

- 日期：2026-10-31
- 范围：`features/catalog/search/`（embedding、Qdrant 索引、硬约束过滤检索）
- 数据：`data/catalog-v1.jsonl`，共 3700 条
- 决策：本地 Qdrant（`:memory:`）、Product 层检索 + 最低 SKU 价、复用 `OPENAI_*`
  远程 embedding（测试用假向量，不联网）

## 结论

| ID | 验收项 | 结果 |
|---|---|---|
| S2-1 | 新增 search 单测全绿，不依赖真实 embedding API / 外部 Qdrant server | PASS |
| S2-2 | `OpenAIEmbedder` 用 `httpx.MockTransport` 验证请求形状与按 index 排序 | PASS |
| S2-3 | 端到端等价用例：命中商品的每一项都满足全部硬约束 | PASS |

质量门禁：

```text
make test       77 passed (原 72 + 新增 5)
make lint       All checks passed；39 files already formatted
make typecheck  mypy Success: 26 source files；tsc 无错误
```

## S2-1：search 单测（本地 Qdrant + 假向量）

`tests/test_qdrant_search.py` 用一个确定性的 `FakeEmbedder`（字符频率向量）和
`:memory:` 模式 Qdrant，构造少量 seed，验证：

- 硬约束过滤：类目 / 价格 / 重量不符的商品被排除，命中的每一项都满足约束。
- `excluded_material_tags` 通过 `must_not` 排除。
- 命中带 `score`，`product` 由 payload 快照重建。

```bash
uv run --directory backend pytest -q tests/test_qdrant_search.py
```

实际输出：

```text
3 passed
```

## S2-2：embedder 单测（MockTransport，不联网）

`tests/test_embedder.py` 用 `httpx.MockTransport` 拦截请求，验证：

- 请求 URL 以 `/embeddings` 结尾，`Authorization: Bearer <key>` 正确。
- body 的 `model` / `input` 正确。
- 响应故意乱序（index 1 在前），embedder 按 `index` 排序还原。

```bash
uv run --directory backend pytest -q tests/test_embedder.py
```

实际输出：

```text
2 passed
```

## S2-3：端到端等价用例

原始用例「2000 元以内、养猫、轻一点的吸尘器」因 catalog 中吸尘器为 0 条无法跑通，
改用等价用例验收检索链路（数据缺口见下）：

```python
spec = ProductSearchSpec(
    category="母婴宠物",
    price_max_major=Decimal("2000"),
    currency="CNY",
    weight_max_kg=1.0,
)
hits = search.search("养猫 宠物 轻便 出行", spec, limit=20)
assert hits
for hit in hits:
    p = hit.product
    cheapest = min(sku.price.amount for sku in p.skus)
    assert p.category == "母婴宠物"
    assert p.weight_kg <= 1.0
    assert cheapest <= Decimal("2000")
```

实际输出（3700 条 → 索引 → 20 条命中）：

```text
loaded 3700 products
indexed
hits: 20
OK: every hit satisfies all hard constraints
```

## 已核实的数据 / 模型缺口

- 「吸尘器 / vacuum / 扫地」在 catalog 中 **0 条** → 原始用例无法端到端跑通。
- 「养猫」无结构化字段（`material_tags` 是材质不是场景）→ 靠 title/description 的
  向量语义匹配。
- 「轻一点」无绝对阈值 → 引入可配置的 `weight_max_kg` 参数。

## 设计要点（供学习参考）

- **语义与硬约束分离**：向量相似度决定「排序」，Qdrant `filter` 决定「是否命中」。
  硬约束在检索时由 Qdrant 强制执行，命中即满足约束，检索后无需再手写 Python 过滤。
- **Product 层检索 + 最低价**：索引时预计算同币种最低 SKU 价 `min_price_major`，
  价格硬约束落在 Product 层；payload 存完整 `product` 快照，检索后无需回查 repo
  （catalog 只读，快照不会过期）。
- **本地 Qdrant 的 UUID 限制**：`:memory:` / 本地目录模式只接受 UUID point id，
  因此用 `uuid5(namespace, product_id)` 生成稳定 id，原始 `product_id` 存 payload 兜底。

## 追加：五阶段可视化 + 覆盖性测试数据

为原始用例「2000 元以内、养猫、轻一点的吸尘器」造了合成测试数据与五阶段参考管线，
验收项 S2-4 / S2-5：

| ID | 验收项 | 结果 |
|---|---|---|
| S2-4 | 五阶段管线（打分→过滤→排序→截断→组装）逐段可观测、可断言 | PASS |
| S2-5 | `SearchPipeline` 与 `QdrantProductSearch` 同一输入结果一致 | PASS |

- 测试数据：`backend/tests/fixtures/catalog-vacuum-demo.jsonl`（14 条），覆盖类目错
  （D201/D205）、价格超（V104=4000）、重量超（V105=6.0kg）、币种错（D204=USD）、
  材质排除（V109=过敏原）、边界值（V106=2000、V107=1.5kg）、多 SKU 最低价
  （V108=2500/999）、语义干扰项（D201「猫包」语义高但类目错）。
- 参考管线：`search/pipeline.py` 的 `SearchPipeline`，`matches()` 是 `build_filter`
  的纯 Python 镜像；五阶段各为一个可单独调用的方法。
- 质量门禁：`make test` 81 passed（+4），`make lint`/`make typecheck` 全绿。

```bash
uv run --directory backend pytest -q tests/test_search_pipeline.py
# 4 passed
```
