# S5 验收测试报告（五条）

- 日期：2026-11-02
- 范围：`features/chat/`（CLI 会话 → HTTP 请求）+ `agents/factory.py` 共享装配 + health 扩展
- 基线：`make test` → **156 passed**（S4 封板时 136，净增 20）；`make lint` / `make typecheck` 全绿
- 数据：真实 catalog `data/catalog-v1.jsonl`（3700 条，**多币种** CNY/USD/EUR/JPY/SGD）+ 测试 fixture
- 方法：离线用确定性 `FakeAgent` / `FakeEmbedder` 逐条实测会话/路由/工具；真模型走 DashScope `qwen-plus`
  （`HARBOR_MODEL_PROVIDER=dashscope`），一键冒烟 `scripts/verify_s5.py`
- 可交互入口：`make dev-api`（`python -m harbor`，127.0.0.1:8010）
- 设计问答：[s5-http-chat-design-notes.md](../s5-http-chat-design-notes.md)
- 可视化：无独立 pipeline 图（S5 是接口层）；会话生命周期见
  [chat/README.md](../../backend/src/harbor/features/chat/README.md)

## 关键架构变更（本章开头做出的转向）

S5 把 CLI 的一次对话改成 HTTP 的一次请求。过程中**按验收清单对齐了对外契约**——早期命名
`/api/chat` + `session_id`/`message`/`summary` 与清单的 `/commerce/intents` +
`buyer_id`/`shopping_session_id`/`raw_query`/`final_text` 不一致，已全量对齐。三条关键转向：

1. **共享装配抽工厂**：把 `cli.py` 里昂贵的只读装配（products + Qdrant 盘上索引 + reranker +
   model client）抽到 `agents/factory.py`，`lifespan` 建一次、每会话发一个带状态的 `Agent`——
   贵的只读件共享，带状态的 `Agent` 按会话隔离。
2. **会话键改 `(buyer_id, shopping_session_id)`**：多租户隔离 + create-or-resume（清单总是传两个
   id，所以没有「未知会话 → 404」，首见即建）。health 增加 `model` + `dependencies`，并在
   `/health` 与 `/api/health` 双挂载（后者不破坏现有前端）。
3. **并发正确性（S5-4 / S5-5）**：读 AgentScope 源码确认 `FunctionTool.call` 对**同步**函数不丢
   线程池（直接 `self._func(**kwargs)`），所以把 `service.search`（Qdrant + embedding HTTP）用
   `asyncio.to_thread` 卸载到工作线程，否则同步检索阻塞事件循环、跨会话全部串行。同会话则用
   per-session `asyncio.Lock` 串行——`Agent.reply` 不可重入。

**S5-2 的坑（本章最重要的发现）**：AgentScope 的 `Toolkit.call_tool` 会把工具抛出的异常转成
`ToolResultState.ERROR` 的结果**塞回给模型，而不是向上抛**。因此「工具异常 → 5xx」不能靠
`try/except`——而且这个 ERROR tool_result **不在最终 `reply` 返回的 Msg 上**（`reply` 只带最后的
文字答案），而是落在会话的 `agent.state.context` 里（AgentScope 自己的 `_get_repeated_tool_error`
也是读 `_get_last_msg()` 的 context）。所以 `run_turn` 在解析结构化输出**之前**扫描本轮新增的
context 消息里的 `tool_result`，是 `ERROR` 则转 502——否则模型会在失败检索后「硬编」一个答案返回
200，漏掉这个错误。

## 结果总览

| ID | 验收项 | 结论 | 测试证据 |
|---|---|---|---|
| S5-1 | `POST /commerce/intents` 传 `buyer_id`/`shopping_session_id`/`raw_query` → `final_text` + 商品数据 | **PASS** | `test_intents_returns_final_text_and_cards` + `verify_s5.py` ①② |
| S5-2 | 模型超时 / 工具异常 → 结构化 `error` + 5xx，无堆栈 | **PASS** | `test_intents_model_timeout_returns_504` / `test_intents_tool_failure_returns_502` / `test_run_turn_raises_tool_failure_error_on_error_tool_result` + `verify_s5.py` ⑧⑨ |
| S5-3 | `/health` 返回 model 名 + 依赖状态 | **PASS** | `test_health_reports_model_and_dependencies` + `verify_s5.py` ⑤ |
| S5-4 | 两个不同 session 并发互不阻塞（总耗时 ≈ 单个） | **PASS** | `test_tool_offloads_search_off_the_event_loop` + `verify_s5.py` ⑥ |
| S5-5 | 同一 session 并发两个请求 → 串行（第二个等第一个） | **PASS** | `test_same_session_concurrent_turns_serialize` + `verify_s5.py` ⑦ |

**总体：5 条全部通过。**

---

## S5-1 一次性请求返回 final_text + 商品数据 — PASS

清单原文：`curl -X POST .../commerce/intents -d '{"buyer_id":"u1","shopping_session_id":"s1",
"raw_query":"2000 以内的吸尘器"}'` → 返回 `final_text` + 商品数据。

**数据缺口**：真实 3700 条目录里「吸尘器」**0 条**（S3 已确认）。所以清单原文
`"2000 以内的吸尘器"` 会**正常返回 200**，只是 `cards == []`、`final_text` 说明无命中。

> **「0 条 → 空结果」也算通过。** S5-1 验收的是「请求 → `final_text` + 商品数据结构」这条
> 链路，不是「必须命中某个具体商品」。空结果响应仍完整携带 `final_text` 字段和 `cards: []`
> （商品数据结构本身），所以清单原文的吸尘器查询会得到一个**有效的空结果响应**，判定 PASS；
> 要看到非空 `cards`，换目录里真实存在的类目（下面用「猫饮水器」）。

### 请求 / 响应形状

```bash
# 等价命中（猫饮水器在目录里真实存在）
curl -s -X POST http://127.0.0.1:8010/commerce/intents -H 'Content-Type: application/json' \
  -d '{"buyer_id":"u1","shopping_session_id":"s2","raw_query":"预算300元，给猫买个饮水器"}'
```

```jsonc
// 200 —— verify_s5.py ① 真模型（qwen-plus）实际采集的输出（2026-11-02）
{
  "final_text": "命中35条，其中2款为饮水器（均在300元预算内），其余33款因价格超预算被过滤",
  "cards": [
    { "sku_id": "P3468-S1", "title": "Roamix 宠物饮水器 GX-30-2 1.5L 单层过滤",
      "brand": "Roamix", "category": "母婴宠物", "price_major": 297.024, "currency": "CNY",
      "weight_kg": 1.17, "material_tags": ["金属"] },
    { "sku_id": "P3464-S1", "title": "Roamix 宠物饮水器 GX-30-1 1L 无过滤",
      "brand": "Roamix", "category": "母婴宠物", "price_major": 179.496, "currency": "CNY",
      "weight_kg": 1.14, "material_tags": ["金属"] }
  ]
}
```

### 判定：PASS

- 契约字段名与清单一致（`buyer_id`/`shopping_session_id`/`raw_query` 入，`final_text`/`cards` 出）。
- `run_turn` 把 `Agent.reply(..., structured_schema=ProductCardList)` 的 `Msg.structured_output`
  解析为 `ProductCardList`，`summary` → `final_text`、`cards` → `cards`。
- 会话复用：同一 `(buyer_id, shopping_session_id)` 再发一轮复用同一个 Agent（`test_intents_resumes_same_buyer_session`）。

---

## S5-2 超时 / 工具异常 → 结构化 error + 5xx — PASS

清单原文：模型超时 / 工具异常 → 结构化 `{"error": ...}` + HTTP 5xx，无堆栈。

### 超时（504）

`run_turn` 用 `asyncio.wait_for(agent.reply(...), timeout=turn_timeout_seconds)` 包住整轮
（默认 120s，`config.turn_timeout_seconds`）。超时抛 `ModelTimeoutError` → router 转 504。

```python
# test_intents_model_timeout_returns_504：FakeAgent.reply sleep 0.5s，timeout 0.01s
resp.status_code == 504
resp.json()["error"]["code"] == "http_504"
```

### 工具异常（502）

AgentScope 把工具异常转成 `ToolResultState.ERROR` 塞回模型（不向上抛），且该 ERROR 落在
`agent.state.context`（最终 `reply` 不带 tool_result 块）。所以 `run_turn` 在解析前扫描本轮新增
context 消息的 tool_result：

```python
# run_turn —— 快照本轮起点，只查本轮新增的 tool_result（跨会话复用不误伤历史失败）
context_start = len(session.agent.state.context)
reply = await asyncio.wait_for(session.agent.reply(...), timeout=...)
for msg in session.agent.state.context[context_start:]:
    for block in msg.get_content_blocks("tool_result"):
        if block.state == ToolResultState.ERROR:
            raise ToolFailureError()   # → router 502 http_502
```

### 判定：PASS

- 两个故障都落在统一信封 `{"error": {"code", "message"}}`（`install_error_handlers` 兜底），
  响应体无堆栈。
- **离线**：用确定性 `FakeAgent` stub 逐条覆盖 504/502（`test_intents_*` + `test_run_turn_*`）。
- **真故障注入**（`verify_s5.py` ⑧⑨，真实组装出的 app + 真实 runtime，非 stub；2026-11-02 实跑全绿）：
  - ⑧ 超时：把 `settings.turn_timeout_seconds` 临时压到 `0.001`，真 `run_turn` 里
    `asyncio.wait_for` 立刻超时 → 实跑返回 **504 `http_504`**（`模型调用超时（>0.001s）`）。
    前提是 `build_agent` 设 `ReActConfig(interruption_raise_cancelled_error=True)`——AgentScope 默认
    会**吞掉** `wait_for` 的 CancelledError 并返回 "INTERRUPTED" reply，导致超时静默变成 502；
    打开该开关才让超时按 504 暴露。
  - ⑨ 工具故障：把 `app.state.runtime.service.search` 换成必抛错的函数，真实 AgentScope
    tool adapter 把异常转成 `ToolResultState.ERROR` → `run_turn` 转 502，实跑返回 **502 `http_502`**
    （`商品检索工具调用失败`）。
  - 之所以要「注入到 `service.search` 而不是 `FakeAgent`」，是因为 AgentScope 吞掉工具异常、
    塞回 ERROR tool_result 这一行为**只在真 adapter 上发生**，stub 测不到这条真实路径。
  - **verify_s5.py 的第二个坑（事件循环）**：冒烟脚本若用 `TestClient` 跑 ①-⑤ 再另起一个
    `asyncio.run` 跑 ⑥⑦，会把共享 runtime（模型 client 里的 asyncio 原语）跨两个事件循环，
    报 `Event bound to a different event loop` / `Event loop is closed`。已改成**单事件循环**：
    手动进入 `app.router.lifespan_context` + 一个 `httpx.ASGITransport` 客户端跑完全部 ①-⑨。

---

## S5-3 /health 返回 model 名 + 依赖状态 — PASS

清单原文：`/health` 返回 model 名 + 依赖状态。

```bash
curl -s http://127.0.0.1:8010/health
# → {"status":"ok","service":"harbor-purchase-agent","version":"0.1.0","environment":"development",
#    "llm_configured":true,"model":"qwen-plus",
#    "dependencies":{"llm":"ok","catalog":"ok"},"time":"…"}
```

### 判定：PASS

- `HealthResponse` 增加 `model`（`llm_configured` 时为 `openai_model`，否则 `null`）与
  `dependencies`（`llm` → `ok`/`unconfigured`，`catalog` → 运行时是否装配 `ok`/`unloaded`）。
- 路径双挂载：`/health`（清单）与 `/api/health`（现有前端）返回相同结构。
- `test_health_reports_model_and_dependencies` 断言未配 key 时 `model is None`、
  `dependencies["llm"] == "unconfigured"`。

---

## S5-4 跨会话并发不阻塞 — PASS

清单原文：两个不同 session 并发 → 互不阻塞（总耗时 ≈ 单个）。

### 实现

`product_search` 工具改 async，把同步 `service.search` 丢线程池：

```python
result = await asyncio.to_thread(service.search, query, spec, 10)
```

### 判定：PASS

- `test_tool_offloads_search_off_the_event_loop` 记录 `service.search` 的执行线程 id，断言
  ≠ 事件循环线程——证明同步检索已卸载，不再阻塞循环、串行化所有并发请求。
- `verify_s5.py` ⑥ 真模型下两个不同 `(buyer_id, shopping_session_id)` 并发各返回 200，
  实跑 `A=200 B=200 耗时 11.5s`（≈ 单轮，互不阻塞）。

---

## S5-5 同会话并发串行 — PASS

清单原文：同一 session 并发两个请求 → 串行执行（第二次等第一次完成）。

### 实现

`SessionStore` 给每个会话一把 `asyncio.Lock`，`run_turn` 全程持锁：

```python
async with session.lock:
    reply = await asyncio.wait_for(session.agent.reply(...), timeout=...)
```

### 判定：PASS

- `test_same_session_concurrent_turns_serialize` 用 `asyncio.gather` 并发跑同一 session 两个
  turn，断言 `Agent.reply` 的**最大并发数 == 1**（锁生效；若没锁会是 2）。
- `verify_s5.py` ⑦ 真模型下同一 `(buyer_id, shopping_session_id)` 并发两请求都 200，第二个等
  第一个——实跑 `A=200 B=200 耗时 23.1s`（≈ 单轮两倍，串行）。

---

## 附：手工验证前置条件

1. **模型 key**：`.env` 已配 `OPENAI_API_KEY`（provider=dashscope / model=qwen-plus）。
2. **本地索引**：`.cache/catalog-index/` 已建（`verify_s4.py --real-catalog` 产出）；缺失时
   `verify_s5.py` 现建，约 4–5 分钟。
3. **Qdrant 目录锁**：本地 path 模式单进程独占，跑 `verify_s5.py` 前先停 `make dev-api`
   （`lsof -iTCP:8010` 确认），否则撞 `Storage folder ... is already accessed by another
   instance of Qdrant client`。

```bash
cd /Users/ym/AgentDev/harbor-purchase-agent
make test        # 156 passed（离线确定性）
uv run --directory backend python scripts/verify_s5.py   # 真模型冒烟，退出码 0 = 通过
```
