# S5 设计问答：CLI 会话 → HTTP 请求

> 记录 S5（把 CLI 的一次会话变成 HTTP 的一次请求）的架构决策与设计原则。
> 相关：验收报告 [_build_log/2026-11-02-s5-acceptance-report.md](_build_log/2026-11-02-s5-acceptance-report.md)、
> S3 设计 [s3-search-design-notes.md](s3-search-design-notes.md)、
> 检索接线 [../backend/src/harbor/agents/cli.py](../backend/src/harbor/agents/cli.py)、
> API 契约 [api-contract.md](api-contract.md)。

## 零、对齐验收清单

S5 的对外契约以验收清单为准（清单用词与本仓库早期命名不同），已对齐：

| 清单项 | 落地 |
|---|---|
| S5-1 `POST /commerce/intents` + `buyer_id`/`shopping_session_id`/`raw_query` → `final_text` + 商品数据 | [schemas.py](../backend/src/harbor/features/chat/schemas.py) |
| S5-2 模型超时 / 工具异常 → 结构化 `error` + 5xx、无堆栈 | 504 超时 / 502 工具失败，走统一信封 |
| S5-3 `/health` 返回 model 名 + 依赖状态 | health 增加 `model` + `dependencies` |
| S5-4 跨会话并发不阻塞 | 同步检索丢线程池（`asyncio.to_thread`） |
| S5-5 同会话并发串行 | 每会话一把 `asyncio.Lock` |

路径是根级（`/commerce/intents`、`/health`），不带 `/api` 前缀；为不破坏现有前端，
health 同时在 `/api/health` 也挂一份。

## 一、目标与决策

S4 交付了 CLI：一个进程内跑 `load_products → 开索引 → 装配 Agent → REPL 循环`。
S5 把「CLI 的一次对话」变成 HTTP 的一次请求，让模型 + 工具 + 结构化商品卡通过接口暴露。

已拍板的三项决策：

| 维度 | 结论 |
|---|---|
| 响应方式 | 一次性 JSON（非流式）；SSE 契约留给后续阶段 |
| 会话 | **有状态**：`(buyer_id, shopping_session_id)` → 服务端内存里一份 `Agent`，跨请求保留对话历史 |
| 响应体 | `final_text + cards`；诊断（`recall_strategy` 等）不进响应，走日志 |

默认参数（跑通用，后续可调）：TTL 30 分钟、上限 1000 会话、单轮超时 120s、暂不做 `DELETE`。

## 二、四个维度

### 1. 谁来装配、什么时候装配

| 内容 | 时机 | 理由 |
|---|---|---|
| 商品列表、Qdrant 客户端+盘上索引、`CatalogSearchService`（含 fit 好的词表）、`FastembedReranker`（~1GB）、chat model 客户端 | **lifespan 启动装配一次，全 app 共享** | 贵、只读、跨会话可复用 |
| `Agent` 实例（含 ReAct 对话状态 + Toolkit） | **每个 session 首次出现时装配，入 session store** | 带状态，必须按会话隔离 |

装配逻辑从 [cli.py](../backend/src/harbor/agents/cli.py) 抽到共享工厂 `harbor/agents/factory.py`，
CLI 与 HTTP 共用，避免两套装配漂移。共享的 `CatalogSearchService` 只建一个；每个会话的
Agent 各自 `make_product_search_tool(shared_service)` 包一层——tool 无状态、service 只读，可多会话共享。

### 2. 结果如何序列化

`await agent.reply(UserMsg(...), structured_schema=ProductCardList)` → `Msg.structured_output`
（dict）→ `ProductCardList.model_validate` → `final_text = summary`、`cards = cards.cards`。
复用 [cards.py](../backend/src/harbor/agents/cards.py) 的 `ProductCard`（`score` 字段保留但买家不消费）。

### 3. 并发 / 串行

- **跨会话并行（S5-4）**：不同 session 各自独立 Agent，FastAPI async 天然并行；
  同步的 `service.search`（Qdrant + embedding HTTP）在 tool 里用 `asyncio.to_thread`
  丢线程池，避免阻塞事件循环——这是 S5-4 的关键。
- **同会话串行（S5-5）**：每个 session 一把 `asyncio.Lock`，整个 `agent.reply(...)`
  期间持锁——AgentState 不可重入。
- **ReAct 循环内部串行**：模型↔工具往返，AgentScope 固有约束，无需处理。

### 4. 可共享 vs 隔离

| 共享（只读/无状态） | 隔离（带状态） |
|---|---|
| settings、products、索引+词表、reranker、model client、service | per-session `Agent` + `AgentState` + `asyncio.Lock` |

## 三、契约

```jsonc
// 请求 POST /commerce/intents
{ "buyer_id": "u1", "shopping_session_id": "s1", "raw_query": "2000 以内的吸尘器" }
// 响应 200
{ "final_text": "命中35条，展示2款…", "cards": [ /* ProductCard... */ ] }
```

- `raw_query` 纯自然语言（同 CLI）；模型通过 `product_search` 工具自行提取预算/类目等硬约束，
  请求里不带结构化筛选字段。
- `(buyer_id, shopping_session_id)` 二者都必填；该 key 未见过 → 新建会话，见过 → 复用
  （create-or-resume，无 404）。`buyer_id` 保证多租户隔离。

## 四、会话生命周期

- **创建**：首次见到某 `(buyer_id, shopping_session_id)` → 装配 Agent 入库。
- **淘汰（TTL + LRU 上限）**：TTL 30 分钟无活动 + 上限 1000（LRU），访问时惰性清理。
  TTL 管「单个会话变旧」，上限管「总量别爆」，两者叠加。
- **清除**：暂不做 `DELETE`，靠 TTL 自动清理（YAGNI）。

## 五、错误处理（沿用 api-contract 错误信封）

信封 `code` 只有两种取值（见 [core/errors.py](../backend/src/harbor/core/errors.py)）：
404 → `not_found`，其余 → `http_<status>`。语义用 HTTP 状态码 + 描述性 `message` 表达。

| 场景 | 状态码 | code | message |
|---|---|---|---|
| `llm_configured=False`（store 未装配） | `503` | `http_503` | 模型未配置（缺少 OPENAI_API_KEY） |
| 单轮超过 `turn_timeout_seconds` | `504` | `http_504` | 模型调用超时（>120s） |
| `product_search` 工具以 ERROR 收尾（S5-2） | `502` | `http_502` | 商品检索工具调用失败 |
| `structured_output is None`（模型没产出结构化） | `502` | `http_502` | 模型未产出结构化输出：<模型原话> |

工具异常不直接抛栈：AgentScope 的 `Toolkit.call_tool` 会把工具异常转成 `ToolResultState.ERROR`
的结果塞回给模型，`run_turn` 在解析前检测「最后一个 tool_result 是 ERROR」并转成 502，
而不是信任模型在失败检索后的猜测。

## 六、文件布局

```
harbor/features/chat/schemas.py   # CommerceIntentRequest / CommerceIntentResponse
harbor/features/chat/service.py   # SessionStore（(buyer_id,session) 键 + TTL/LRU + 锁）+ run_turn()
harbor/features/chat/router.py    # POST /commerce/intents
harbor/agents/factory.py          # 从 cli.py 抽取的共享装配（AgentRuntime + build_runtime + build_agent）
harbor/agents/tools.py            # product_search 工具（async，检索丢线程池）
harbor/main.py                    # lifespan 装配共享组件 + 挂载路由（/commerce/intents、/health、/api/health）
tests/test_chat_service.py        # 离线：SessionStore 键/TTL/LRU/锁、run_turn（stub，含超时/工具失败/串行）
tests/test_chat_router.py         # 离线：路由 + 错误路径（stub service）
tests/test_product_search_tool.py # 含 S5-4 线程池卸载断言
scripts/verify_s5.py              # 手工：真模型 + 真索引端到端（同 verify_s4 风格）
```

## 七、实现时需验证的技术风险

1. `Agent.reply` 是否在同一 Agent 实例上累积多轮历史（CLI 已证明可用，需确认 HTTP 下同 Agent 并发安全）——同会话加锁已覆盖。
2. 共享的 model client（`DeepSeekChatModel`）跨会话并发是否线程安全——异步 HTTP 客户端，实测跨会话并发 OK。
3. 共享的 reranker（fastembed）并发安全——CPU 推理无共享可变状态，GIL 下会串行。
4. `QdrantClient` path 模式并发读——本地模式锁目录，一个进程一个 client；卸载到线程池后并发读安全。

## 八、一句话总结

S5 = 把 CLI 的会话循环拆成「lifespan 装配共享只读件 + 每会话装配带状态 Agent」，
用一次性 JSON 暴露成 `POST /commerce/intents`——**贵的只读件共享一次，带状态的 Agent
按 `(buyer_id, shopping_session_id)` 隔离，同会话串行、跨会话并行，同步检索丢线程池**。
