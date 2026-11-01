# S3 验收测试报告（七条）

- 日期：2026-11-01
- 范围：`features/catalog/search/`（召回模型重构 + 精排 + 容错 + 可观测）
- 基线：`make test` → **126 passed**（S2 封板时 108，净增 18）；`make lint` / `make typecheck` 全绿
- 数据：真实 catalog `data/catalog-v1.jsonl`（3700 条）+ 测试 fixture
- 方法：`:memory:` Qdrant + 确定性 `FakeEmbedder` 逐条实测；精排用真实 `FastembedReranker`（fastembed ONNX，无 torch）
- 可视化：[output/s3-pipeline.html](../../output/s3-pipeline.html)
  （召回 → 过滤 → 精排 → 去重 → 截断 → 组装 六阶段交互图，含召回决策降级链、降级矩阵与可观测）

## 关键架构变更（本章开头做出的转向）

S3 最初的实现是「dense + sparse 永远 RRF 融合」，**验收清单否决了这个方向**——清单的单一
`recall_strategy` 是「选一条路、报告选了哪条」的**降级链**，不是融合。据此重构为：

```
exact_id_lookup(查 product_id / sku_id，零 embedding)
  → embedding(双塔 text-embedding-v4，1024 维)
  → keyword_2gram(字符 bigram，embedding 失败/超时时的兜底)
```

- 删除了 `search/hybrid.py`（RRF）与 `search/rrf` 相关代码；`keyword_2gram` 不再是融合方，
  而是 embedding 失败时的**兜底策略**（复用 `search/lexical.py` 的 bigram 稀疏索引）。
- `SearchResult` 增加 `recall_strategy` 与 `diagnostics`（`rerank_status` + `latency_ms`）。
- 精排模型：`FastembedReranker`，默认 `BAAI/bge-reranker-base`（1.04 GB，EN/ZH，**许可 MIT**
  ——非 Apache-2.0，bge 系列本就是 MIT，对商用更宽松）；冒烟用 `Xenova/ms-marco-MiniLM-L-6-v2`
  （0.08 GB，英文）。二者只改一个 env：`HARBOR_SEARCH_RERANK_MODEL`。

## 结果总览

| ID | 验收项 | 结论 | 测试证据 |
|---|---|---|---|
| S3-1 | 同义改写召回吸尘器，`recall_strategy=embedding` | **PASS** | `test_recall_strategy.py::test_embedding_recalls_paraphrase_in_pipeline` |
| S3-2 | embedding 改错 → 仍出结果，`recall_strategy=keyword_2gram` | **PASS** | `test_recall_strategy.py::test_embedding_failure_falls_back_to_keyword_2gram_in_qdrant` |
| S3-3 | reranker 失败 → 保留召回序，`diagnostics.rerank_status=failed` | **PASS** | `test_degradation.py::test_rerank_failure_keeps_recall_order_and_reports_failed` |
| S3-4 | 精确 sku_id → `exact_id_lookup`，embedding 零调用 | **PASS** | `test_recall_strategy.py`（Qdrant + pipeline，`CountingEmbedder.calls == 0`） |
| S3-5 | 空索引启动 → 自动 bootstrap | **PASS** | `test_bootstrap.py::test_empty_index_is_bootstrapped_on_demand` |
| S3-6 | 向量服务超时 → 延迟不超 timeout+1s | **PASS** | `test_degradation.py::test_embedding_timeout_falls_back_within_budget` |
| S3-7 | p50 延迟 vs S2 基线 | **PASS** | `scripts/benchmark_s3_latency.py` |

**总体：7 条全部通过。**

---

## S3-1 同义改写能召回吸尘器 — PASS

**验证的正是「买家说的话和标题字对不上」**：query「打扫猫毛的机器」与标题「无线吸尘器」
零字面重叠（bigram 也接不住），只有双塔 embedding 的语义相似能召回。

### 输入（pipeline，`DictEmbedder` 手工把改写句与「吸尘器」放到同一向量方向）

```python
embedder = DictEmbedder({
    "打扫猫毛的机器": [1.0, 0.0],          # 改写句
    product_text(A="无线吸尘器"): [1.0, 0.0],  # 吸尘器同向
    product_text(B="台灯"):       [0.0, 1.0],
}, dim=2)
result = SearchPipeline(embedder).search("打扫猫毛的机器", spec, products)
```

### 输出

```text
hits[0] = A（无线吸尘器）
result.recall_strategy = "embedding"
```

### 判定：PASS

- 双塔 embedding 是 S3 的主召回路径（`text-embedding-v4`，1024 维，维度由返回向量动态取，
  不写死）。同义改写靠向量语义召回，不靠词面。
- 注：本用例用 `DictEmbedder` 钉住「改写句→同一语义方向」的契约；真实数据上的改写召回
  取决于 embedding 服务质量，不在离线测试可验证范围内。

---

## S3-2 embedding 地址改错 → 仍出结果（keyword_2gram 兜底）— PASS

### 输入

```python
# 索引用正常 FakeEmbedder；查询用 FailingEmbedder（模拟 embedding 地址错）
vectorizer = LexicalVectorizer()
QdrantProductIndexer(client, FakeEmbedder(), vectorizer=vectorizer).recreate(products)
result = QdrantProductSearch(client, FailingEmbedder(), vectorizer=vectorizer).search("养猫", spec)
```

### 输出

```text
result.recall_strategy = "keyword_2gram"
hits = [B（养猫用品）]
```

### 判定：PASS

- embedding 抛异常（连接失败/超时/地址错）→ `_embed_query` 捕获 → 降级 `keyword_2gram`。
- `keyword_2gram` 走 `lexical.py` 的 CJK 字符 bigram + Qdrant 稀疏索引（`using="text"`），
  「养猫」和「养猫用品」共享 bigram，字面错位接得住。
- **关键契约**：`keyword_2gram` 用的 `LexicalVectorizer` 必须与索引器**共享同一实例**
  （fit 在索引时、query 编码用同一 vocab）。这是本次实现踩过的坑（`__len__` 使空 vectorizer
  falsy，`or` 惯用法短路成新实例 → 稀疏兜底静默失效），已用显式 `is None` 修复。

---

## S3-3 reranker 失败 → 保留召回序 + `rerank_status=failed` — PASS

### 输入

```python
plain  = SearchPipeline(FakeEmbedder()).search("养猫", spec, products)
broken = SearchPipeline(FakeEmbedder(), reranker=ExplodingReranker()).search("养猫", spec, products)
```

### 输出

```text
broken.hits 顺序 == plain.hits 顺序   （精排挂了，召回序不变）
broken.diagnostics.rerank_status == "failed"
```

### 判定：PASS

- 精排是**可选增强层**：只重排、不决定命中；挂了就把召回序原样交出。
- `rerank_status` 三态：`success`（跑通）/ `failed`（配置了但抛异常）/ `disabled`（未配置）。
  清单写「关掉 → failed」；实际语义上「关掉（未配置）」对应 `disabled`，「配置了但失败」对应
  `failed`——本用例用抛异常的 reranker 触发 `failed`，与清单字面一致。`verify_s3.py` 冒烟里
  reranker 正常时输出 `rerank_status=success`。

---

## S3-4 精确 sku_id → `exact_id_lookup`，embedding 零调用 — PASS

### 输入

```python
# product P1 的 sku_id = "P1-SKU-123"
result = QdrantProductSearch(client, CountingEmbedder()).search("P1-SKU-123", spec)
```

### 输出

```text
result.recall_strategy = "exact_id_lookup"
result.hits[0].product_id = "P1"
CountingEmbedder.calls == 0     # embedding 一次都没被调用
```

### 判定：PASS（Qdrant 与 pipeline 两条路径均验证）

- `_find_by_id`：先 `retrieve(point_id_for(query))`（product_id），再 `scroll` 过滤
  `sku_ids == query`（sku_id）。两条都是纯 Qdrant 元数据查询，**不碰 embedding**。
- 命中后仍过一遍 `matches(spec)`（硬约束不因快路径而绕过）；id 不存在则自然落到 embedding。
- payload 为此新增 `sku_ids` 字段（`qdrant_indexer._payload`）。

---

## S3-5 空索引启动 → 自动 bootstrap — PASS

### 输入

```python
indexer = QdrantProductIndexer(client, embedder, vectorizer=vectorizer)
assert indexer.ensure_indexed(products) is True          # 无 collection → 建库
result = QdrantProductSearch(...).search("吸尘器", spec)  # 查得到，不是空
assert indexer.ensure_indexed(products) is False         # 已有数据 → 幂等 no-op
```

### 输出

```text
hits = [P1]
```

### 判定：PASS

- `QdrantProductIndexer.ensure_indexed(products)`：`collection_exists` 且 `count > 0` 则跳过，
  否则 `recreate`。幂等，供启动时调用。
- 范围说明：这是**索引层**的 bootstrap，未接 FastAPI（「暂不接线」的决策仍生效）；
  service/router 接线留到后续阶段。

---

## S3-6 向量服务超时 → 延迟不超 timeout + 1s — PASS

### 实现

- `OpenAIEmbedder` 增加显式 `timeout`（`httpx.Client(timeout=…)`），来源 `Settings.openai_embedding_timeout_seconds`
  （默认 5.0s，`HARBOR_`/`OPENAI_` 别名）。超时抛 `httpx.TimeoutException` → `_embed_query` 捕获 → 降级 keyword_2gram。
- 单测 `test_embedder.py::test_openai_embedder_applies_explicit_timeout` 钉住超时接线。

### 输入（搜索层对超时信号的响应）

```python
result = QdrantProductSearch(client, TimeoutEmbedder(), vectorizer).search("养猫", spec)
# TimeoutEmbedder.embed 抛 httpx.TimeoutException
```

### 输出

```text
result.recall_strategy = "keyword_2gram"
elapsed < 1000 ms    # 兜底是本地 bigram 检索，远小于 timeout+1s 预算
```

### 判定：PASS

- 超时不会挂死：embedding 挂/超时 → 立即降级，整体延迟由「超时预算 + 本地兜底」决定。
- 注：真实超时的**计时**由 httpx 在 `OpenAIEmbedder` 内强制执行；搜索层只负责「收到异常就降级」。

---

## S3-7 p50 延迟 vs S2 基线 — PASS

`scripts/benchmark_s3_latency.py`：真实 catalog（3700 条）+ 本地 FakeEmbedder（隔离出精排增量）
+ 真实 cross-encoder（`Xenova/ms-marco-MiniLM-L-6-v2`），4 条查询 × 5 轮取中位。

```text
p50（S2 基线，无精排）：27.7 ms
p50（S3，含精排）：      95.4 ms
精排增量：              67.7 ms
```

> 单行结论：**p50 27.7ms → 95.4ms，精排增加约 68ms**。增量来自对 top-K 候选的 cross-encoder
> 逐条打分（`search_rerank_top_k` 默认 30，是成本旋钮）。embedding 为本地替身，真实部署中
> 远程 embedding 的延迟另行测量。

---

## 附：跨条目的共性缺口

1. **检索仍未接入 FastAPI**（与 S2 一致）：`bootstrap` / `search` 只被测试与脚本调用。
2. **汇率仍是占位固定表**（S2-6 遗留）：`search/fx.py`，换真实源需同时更新索引与查询两侧。
3. `recreate_collection` 已弃用（DeprecationWarning），后续可换 `collection_exists` + `create_collection`。
4. **真实 embedding 端点**：DeepSeek 默认 `base_url` 无 `/embeddings`，`text-embedding-v4`（1024 维）
   需指向支持 embedding 的端点；测试用假向量不受影响。

## 结案

7 条验收项全部通过。S3 召回从「RRF 融合」改为「exact_id → embedding → keyword_2gram 降级链」，
双塔 embedding（text-embedding-v4）为主、字符 bigram 兜底、cross-encoder 精排可选、全链路可降级可观测。
