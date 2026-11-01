# S2 验收测试报告（七条）

- 日期：2026-10-31
- 范围：`features/catalog/search/`（只读验证，未修改任何程序逻辑）
- 基线：`uv run pytest -q` → **81 passed**
- 数据：合成 fixture `backend/tests/fixtures/catalog-vacuum-demo.jsonl`（14 条）
  + 真实 catalog `data/catalog-v1.jsonl`（3700 条）
- 方法：用现有 `SearchPipeline` / `QdrantProductSearch`（`:memory:` Qdrant +
  确定性 `FakeEmbedder`）逐条实测，不联网。所有用例均为**临时脚本**，未写入仓库。
- 可视化：[output/retrieval-pipeline.html](../../output/retrieval-pipeline.html)
  （打分 → 过滤 → 排序 → 截断 → 组装 五阶段交互图，悬停商品跨阶段追踪 + 逐条过过滤门）

## 结果总览

| ID | 验收项 | 结论 | 性质 |
|---|---|---|---|
| S2-1 | 每件命中都满足全部硬约束 | **PASS** | 语义精度非问题，不加 product_type |
| S2-2 | `filtered_out` + `rejected_reason` | **PASS**（88 passed） | 已实现 |
| S2-3 | 最高分但超预算 → 不是 `hits[0]` | **PASS**（用例已补，变异检查通过） | 已实现 |
| S2-4 | canonical 去重 + 保留最低价 SKU | **PASS** | 已实现 |
| S2-5 | 不存在品类 → 空列表 + 说明不抛异常 | **PASS** | 已实现 |
| S2-6 | 缺币种按 CNY 换算后比预算 | **PASS** | 已实现 |
| S2-7 | 3 条回归测试（吸尘器/露营灯/超预算） | **PASS** | 3/3 |

**总体：7 条全部通过。**

---

## 公共输入

除特别说明外，七条用例共用以下输入：

```python
# 数据：backend/tests/fixtures/catalog-vacuum-demo.jsonl（14 条）
products = load_products("tests/fixtures/catalog-vacuum-demo.jsonl")

# 检索引擎：本地内存 Qdrant + 确定性假向量
embedder = FakeEmbedder(dim=32)
client   = QdrantClient(":memory:")
QdrantProductIndexer(client, embedder).recreate(products)
search   = QdrantProductSearch(client, embedder)

# 查询
query = "养猫 轻便 吸尘器"
spec  = ProductSearchSpec(
    category="家用电器",
    price_max_major=Decimal("2000"),
    currency="CNY",
    weight_max_kg=1.5,
    excluded_material_tags=["过敏原"],
)
```

fixture 的 14 条构成（每条对应一个测试意图）：

| product_id | 标题 | 价格 | 重量 | 币种 | 标签 | 意图 |
|---|---|---|---|---|---|---|
| V101 | 轻量无线吸尘器 猫毛专用 养猫家庭 | 899 | 0.6 | CNY | ABS | 正向命中 |
| V102 | 无线吸尘器 养猫家庭 除猫毛 轻巧 | 1299 | 1.2 | CNY | ABS | 正向命中（fixture 内最高分） |
| V103 | 轻量手持吸尘器 | 699 | 0.8 | CNY | ABS | 正向命中 |
| V104 | 旗舰吸尘器 猫毛 大吸力 | **4000** | 1.0 | CNY | 铝合金 | **超预算** |
| V105 | 强力吸尘器 猫毛 工业级 | 999 | **6.0** | CNY | 不锈钢 | **超重** |
| V106 | 吸尘器 入门款 轻便 | **2000** | 1.0 | CNY | ABS | 价格边界 |
| V107 | 吸尘器 便携款 | 500 | **1.5** | CNY | ABS | 重量边界 |
| V108 | 吸尘器 双版本 猫毛 | **2500 / 999** | 1.0 | CNY | ABS | 多 SKU 最低价 |
| V109 | 吸尘器 静音 猫毛 | 899 | 0.7 | CNY | **过敏原** | **标签排除** |
| D201 | 宠物出行包 猫包 轻便 | 500 | 0.8 | CNY | 涤纶 | 类目不符（母婴宠物） |
| D202 | 负离子吹风机 宠物吹毛 | 800 | 0.5 | CNY | ABS | 语义干扰（同类目） |
| D203 | 静音加湿器 | 300 | 1.0 | CNY | ABS | 语义干扰（同类目） |
| D204 | 吸尘器 猫毛 海外版 | 100 | 0.5 | **USD** | ABS | **币种不符** |
| D205 | 猫砂盆 养猫必备 | 200 | **3.0** | CNY | PP | 超重 + 类目不符 |

---

## S2-1 每件命中都满足全部硬约束 — PASS

> **状态：结案 PASS（2026-10-31）** — 不加 `product_type` 字段（方向①已否决）；
> 「语义精度」保留意见认定**不属于问题**（D202/D203 满足全部硬约束即符合本验收项）。
> 真实数据回归已由 S2-7 用露营灯等价用例覆盖。

### 输入

```python
hits = search.search("养猫 轻便 吸尘器", spec, limit=20)
```

### 输出（8 条，逐条复核 5 项硬约束）

| # | product_id | score | 最低价 | 重量 | 类目 | 材质标签 | 5 项硬约束 |
|---|---|---|---|---|---|---|---|
| 1 | V102 | 0.8549 | 1299 | 1.2 | 家用电器 | ABS塑料 | ✅ 全过 |
| 2 | V101 | 0.8236 | 899 | 0.6 | 家用电器 | ABS塑料 | ✅ 全过 |
| 3 | V106 | 0.7275 | 2000 | 1.0 | 家用电器 | ABS塑料 | ✅ 全过（价格边界） |
| 4 | V103 | 0.7089 | 699 | 0.8 | 家用电器 | ABS塑料 | ✅ 全过 |
| 5 | V108 | 0.7004 | 999 | 1.0 | 家用电器 | ABS塑料 | ✅ 全过（多 SKU 取最低） |
| 6 | V107 | 0.6569 | 500 | 1.5 | 家用电器 | ABS塑料 | ✅ 全过（重量边界） |
| 7 | D202 | 0.5634 | 800 | 0.5 | 家用电器 | ABS塑料 | ✅ 全过 ⚠ 吹风机 |
| 8 | D203 | 0.4845 | 300 | 1.0 | 家用电器 | ABS塑料 | ✅ 全过 ⚠ 加湿器 |

判定逻辑（对每条命中执行）：

```python
assert cur == spec.currency
assert amt <= spec.price_max_major
assert p.weight_kg <= spec.weight_max_kg
assert p.category == spec.category
assert not set(spec.excluded_material_tags) & set(p.material_tags)
```

### 判定：PASS

两个保留意见：

1. **语义精度**：D202（吹风机）、D203（加湿器）作为第 7/8 名被返回。它们满足全部
   硬约束（category 同为「家用电器」），所以不算违约，但说明**类目粒度太粗**——
   一个 filter 挡不住吹风机。要真正实现「吸尘器」，需要更细的类目/品类字段或一次
   语义阈值截断。
2. **真实数据无法跑原用例**：`data/catalog-v1.jsonl` 里吸尘器 **0 条**（露营灯 18 条）。
   S2-1 只在 14 条合成 fixture 上验证过。
3. 「轻一点」被折算成 `weight_max_kg=1.5`、「养猫」只存在于 query 文本中，均未进入
   spec 结构化字段——这是既定设计，但意味着「养猫/轻」不是硬约束。

---

## S2-2 被过滤商品进 `filtered_out` 并带 `rejected_reason` — FAIL

> **实现与验证（2026-10-31）**：已通过。
>
> - `SearchResult(hits, filtered_out, reason_counts)`、`CandidateFacts`、
>   `RejectedCandidate`、`RejectReason` 已实现（`search/schemas.py`）。
> - 判定与原因统一在 `search/constraints.py`：`facts_from_product` / `matches_facts` /
>   `matches` / `reasons_for`，Qdrant 检索、参考管线、未来重算共用同一套规则。
> - Qdrant 两次查询：filtered 取命中（召回完整）/ unfiltered 取前 N 候选（可解释）。
> - payload 增加 `title`；新增配置 `search_candidate_pool_size`（默认 100）。
> - 门禁：`make test` **88 passed**；`make lint` / `make typecheck` 全绿。
>
> 实测输出（fixture 14 条，SPEC：家用电器 / ≤2000 CNY / ≤1.5kg / 排除过敏原）：
>
> ```text
> hits: V102 V101 V106 V103 V108 V107 D202 D203
>
> filtered_out:
>   V104  [price_over]         价格 4000 CNY 超过预算 2000 CNY
>   V109  [excluded_material]  材质标签命中排除项：过敏原
>   D204  [currency_mismatch]  币种 USD 与目标币种 CNY 不一致
>   V105  [weight_over]        重量 6 kg 超过上限 1.5 kg
>   D201  [category_mismatch]  类目「母婴宠物」不符合「家用电器」
>   D205  [weight_over]        重量 3 kg 超过上限 1.5 kg
>         [category_mismatch]  类目「母婴宠物」不符合「家用电器」
>
> reason_counts: price_over 1 / excluded_material 1 / currency_mismatch 1 /
>                weight_over 2 / category_mismatch 2
> ```
>
> **范围说明**：`filtered_out` 只覆盖「前 N 条语义候选」（N 默认 100）。语义排名在 N
> 之外的被拒商品仍然不会出现——这是固定窗口的既定取舍，不是遗漏。
>
> **注（S2-6 后）**：上表为 S2-6 落地前的快照。现 D204（USD 100）换算为 CNY 720 命中，
> `currency_mismatch` 拒绝原因已移除；实际 filtered_out 为 V104 / V109 / V105 / D201 / D205。

### 输入

同 S2-1。

### 输出（调用方实际拿到的）

```python
>>> hits = search.search("养猫 轻便 吸尘器", spec, limit=20)
>>> [h.product.product_id for h in hits]
['V102', 'V101', 'V106', 'V103', 'V108', 'V107', 'D202', 'D203']
>>> list(type(hits[0]).model_fields)
['product', 'score']          # ← 只有这两个字段，没有 filtered_out / rejected_reason
```

### 实际被丢弃的 6 条（生产路径看不到，这里用 `matches()` 判官还原）

| product_id | 原始 score | 被拒原因（当前只在/不在代码里） |
|---|---|---|
| V104 | 0.8249 | 价格 4000 > 2000 |
| V109 | 0.7778 | 材质标签含「过敏原」 |
| V105 | 0.7303 | 重量 6.0 > 1.5 |
| D204 | 0.7303 | 币种 USD ≠ CNY |
| D201 | 0.5341 | 类目「母婴宠物」≠「家用电器」 |
| D205 | 0.5164 | 重量 3.0 > 1.5；类目「母婴宠物」 |

### 判定：FAIL

- 全仓 grep `filtered_out` / `rejected_reason` / `rejected` → **NOT FOUND**。
- 14 条候选 → 8 条命中，另外 6 条**直接消失**，调用方无法知道谁被筛掉、为什么。
- 根因：过滤发生在 Qdrant 内部（`query_filter`），被过滤点在检索阶段就被丢弃，
  Python 侧拿不到——这是「过滤下沉到 Qdrant」的固有代价。要满足此条，需要额外一次
  查询（不过滤地取回候选 + 本地判定）或改用 `SearchPipeline` 这类可观测路径。

---

## S2-3 最高分但超预算的商品不是 `hits[0]` — PASS（用例已补）

### 用例 A：现有 fixture（**未覆盖**该场景）

**输入**：同 S2-1，按原始分排序。

**输出**：

| 排名 | product_id | score | 价格 | 是否超预算 |
|---|---|---|---|---|
| 1 | **V102** | 0.8549 | 1299 | 否 |
| 2 | V104 | 0.8249 | 4000 | **是** |
| 3 | V101 | 0.8236 | 899 | 否 |

`hits[0]` = **V102**。

→ 超预算的 V104 只排第 2，**最高分本来就是个合规商品**。所以这个场景即使结果正确，
也**证明不了**「先过滤后排序」——过滤与否，第一名都不变。

### 用例 B：手工构造「最高分但超预算」商品

**输入**：在 fixture 基础上追加两个内存商品：

```python
over  = Product(product_id="X-OVER",  title="养猫 轻便 吸尘器 猫毛 轻量 无线 吸尘器",
                price=9999, weight_kg=1.0, category="家用电器", material_tags=["ABS塑料"])
cheap = Product(product_id="X-CHEAP", title="普通收纳盒", price=100, ...)
allp  = products + [over, cheap]
```

**输出**：

| 排名 | product_id | score | 价格 | 是否超预算 |
|---|---|---|---|---|
| 1 | **X-OVER** | **0.9078** | **9999** | **是** |
| 2 | V102 | 0.8549 | 1299 | 否 |
| 3 | V104 | 0.8249 | 4000 | 是 |

```text
SearchPipeline      hits[0] = V102   ✅
QdrantProductSearch hits[0] = V102   ✅
```

### 判定：PASS（用例已补）

代码行为本来就正确（两条路径都把 X-OVER 过滤掉了，第一名是 V102）。缺的是把这一局面
固化进测试，现已补上 `backend/tests/test_filter_before_sort.py`：

- `test_over_budget_top_scorer_is_not_hits_zero`：先断言 X-OVER 确实是原始分第一，
  再断言 `limit=1` 时 `hits[0]==V102`，且 X-OVER 出现在 `filtered_out`
  并带 `price_over`。
- `test_qdrant_also_filters_before_truncating`：Qdrant 路径同样成立。
- `test_scenario_discriminates_the_two_orderings`：显式证明「先截断再过滤」在
  `limit=1` 时会返回空列表——说明这组数据确实能区分两种顺序。

**变异检查**：临时把 `SearchPipeline.search` 改成先截断再过滤，上述 2 条测试立刻变红；
恢复后全绿。说明用例具备区分能力，而不是单纯的「描述现状」。

---

## S2-4 同款不同 SKU 只出现一次（canonical 去重）+ 保留最低价 SKU — PASS

> **实现（2026-10-31）**：
> - 新增 `search/dedupe.py`：`dedupe_by_canonical(scored, spec)`——按 `canonical_product_id`
>   分组，每组保留**换算到目标币种后最低价**的那条（并列按 score、再按 product_id）。
> - `SearchHit` 增加 `selected_sku: Sku`：命中暴露该商品的最低价 SKU。
> - 参考管线新增第 4 阶段「去重」：打分→过滤→排序→**去重**→截断→组装。
> - Qdrant：query 1 取候选窗口（`max(candidate_pool_size, limit)`），Python 侧去重后再截断到 `limit`。
> - 门禁：`make test` **103 passed**；`make lint` / `make typecheck` 全绿。
> - 新增 `tests/test_canonical_dedupe.py`（5 条）；手工脚本 `backend/scripts/verify_s2_4.py`。

### 用例 A：两个 Product 共用同一个 canonical_product_id（已修复）

**输入**：

```python
a = Product(product_id="A1", canonical_product_id="CAN-SAME", title="吸尘器 猫毛 标准版", price=1200)
b = Product(product_id="A2", canonical_product_id="CAN-SAME", title="吸尘器 猫毛 促销版", price=800)
search.search("吸尘器 猫毛", ProductSearchSpec(category="家用电器", currency="CNY"))
```

**输出**：

```python
# 修复前：两条都返回
[('A1', 'CAN-SAME'), ('A2', 'CAN-SAME')]
# 修复后：只留最低价那条（800 < 1200）
[('A2', 'CAN-SAME')]
```

### 用例 B：同一 Product 内多个 SKU（V108）

**输入**：`search.search("养猫 轻便 吸尘器", spec, limit=20)`，取 V108。

**输出**：

```python
hit.selected_sku  # Sku(sku_id="V108-S2", spec="促销版", price=999)  ← 最低价 SKU 已标出
```

### 判定：PASS

- 跨 Product 的 canonical 去重已实现（`dedupe_by_canonical`），保留**换算到目标币种后
  最低价**的那条；并列按 score、再按 product_id。
- `SearchHit.selected_sku` 暴露最低价 SKU，供采购 Agent 直接取「该买哪个 SKU」。
- 去重发生在排序之后、截断之前，不会浪费 `limit` 名额在重复款上。
- 跨币种比价复用 S2-6 的 `convert()`，225 个多件组（全跨币种）可正确比较。
- 数据侧（CROSS 已拆成单件）使「按 canonical id 分组」本身即安全，无需逐组再校验契约。

---

## S2-5 查不存在的品类 → 空列表 + 明确说明，不抛异常 — PASS

> **实现（2026-10-31）**：
> - `SearchResult` 增加 `empty_reason: str | None`；`constraints.empty_reason_for(spec,
>   category_exists)` 生成说明——品类不存在 →「品类「X」不存在」，品类存在但无匹配 →
>   「没有商品满足全部条件」。
> - Pipeline 直接用 `products` 判断品类是否存在；Qdrant 用 `client.count(count_filter=…)`。
> - 不新增 service/router 层：说明直接挂在 `SearchResult` 上。
> - 门禁：`make test` **108 passed**；新增 `tests/test_empty_result.py`（4 条）。

### 输入

```python
spec_missing = ProductSearchSpec(category="不存在的品类", currency="CNY")
search.search("吸尘器", spec_missing, limit=10)
```

### 输出

```python
SearchResult(hits=[], empty_reason="品类「不存在的品类」不存在")
```

### 判定：PASS

- 空列表 + 不抛异常：保持。
- 「明确说明」：`empty_reason` 区分「品类不存在」与「没有匹配商品」两种空态。

---

## S2-6 缺币种时按目标币种（CNY）换算后再比较预算 — PASS

> **实现（2026-10-31）**：币种从「硬排除」改为「换算后比较预算」。
>
> - 新增 `search/fx.py`：固定汇率表 `CNY_PER_UNIT`（CNY 本位，占位值：USD 7.2 /
>   EUR 7.8 / JPY 0.048 / SGD 5.3），`to_cny()` / `convert()`。
> - `constraints.matches_facts` / `reasons_for`：不再因 `currency != spec.currency` 拒绝，
>   而是 `convert()` 到目标币种后比较预算；`currency_mismatch` 拒绝原因已移除。
> - Qdrant：索引 payload 预计算 `min_price_cny`；`build_filter` 把预算换算成 CNY，
>   过滤 `min_price_cny <= budget_cny`（不再有 `currency == spec.currency` 的 must）。
> - 命中仍返回原始 `Product`（原币种 SKU），换算只用于过滤比较。
> - 门禁：`make test` **98 passed**；`make lint` / `make typecheck` 全绿。
> - 新增 `tests/test_currency_conversion.py`（7 条）+ `backend/scripts/verify_s2_6.py`。
> - 语义连锁：D204（USD 100）由「币种不符被过滤」变为「换算 CNY 720 ≤ 2000 → 命中」，
>   `test_search_pipeline` / `test_rejected_reasons` 的期望已同步。

### 输入

```python
spec = ProductSearchSpec(category="家用电器", price_max_major=Decimal("2000"), currency="CNY")
# U1 = 100 USD（720 CNY，命中）；U2 = 300 USD（2160 CNY，超预算）
search.search("吸尘器", spec, limit=10)
```

### 输出

```text
hits:        U1 (100 USD → 720 CNY)
filtered_out:
  U2  [price_over]  价格 2160 CNY（换算自 300 USD）超过预算 2000 CNY
```

### 判定：PASS

- 币种不再是硬 `must` 条件；非 CNY 商品换算后预算内则命中、超预算则 `price_over`。
- 真实 catalog 币种分布 CNY 1469 / USD 1412 / EUR 1408 / JPY 1408 / SGD 1408 不变，
  但这些非 CNY 商品现在都能参与 CNY 预算的比较（此前约 60% 永远查不到）。
- 占位说明：汇率是固定表、非实时。换真实汇率源时要同时更新索引（`min_price_cny`
  已固化进 payload）与查询（`build_filter`）两侧，两者必须用同一张表。

---

## S2-7 3 条回归测试钉住（吸尘器 / 露营灯 / 超预算） — PASS

> **实现（2026-10-31）**：补露营灯回归 `tests/test_camping_lantern.py`——对真实 catalog
> （`data/catalog-v1.jsonl`，含 `CAN-GX-13-*` / `CAN-ML-09-*` 露营灯跨币种/跨语言副本）
> 搜索「露营灯」，断言命中非空、硬约束全满足、canonical 去重唯一、至少一条标题含「露营灯」。

### 对照三条要求

| 要求场景 | 现状 |
|---|---|
| 吸尘器 | ✅ 4 条用例基于吸尘器 fixture |
| 露营灯 | ✅ `test_camping_lantern.py`（真实数据回归） |
| 超预算 | ✅ `test_filter_before_sort.py` 钉住「最高分超预算 → 不是 hits[0]」 |

### 判定：PASS（3/3）

---

## 附：跨条目的共性缺口

1. **检索尚未接入 FastAPI**：`ProductSearchSpec` 在 `src/` 中于测试之外**从未被构造**，
   `recreate()` / `search()` 也只被测试调用。S2-2 / S2-5 的说明与审计已通过扩展
   `SearchResult` 解决，无需 service 层；但把检索暴露成 API 仍需要一个 router。
2. ~~被过滤信息在 Qdrant 侧不可见~~ **已解决（S2-2）**：加了一条不过滤的候选召回。
3. ~~币种是硬约束而非可换算量~~ **已解决（S2-6）**：`search/fx.py` 提供固定汇率表（CNY 本位），
   索引时预计算 `min_price_cny`，检索时把预算换算成 CNY 比较。
4. **类目粒度**：S2-1 暴露的精度问题（吹风机/加湿器）不是过滤 bug，是数据模型问题。
5. 小问题：`recreate_collection` 已弃用（测试里有 DeprecationWarning），后续可换
   `collection_exists` + `create_collection`。

## 结案

7 条验收项全部通过（S2-1 语义精度认定非问题、不加 `product_type`）。
