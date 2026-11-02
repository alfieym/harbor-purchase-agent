# S4 Agent 层设计问答与设计思维

> 记录 S4（`product_search` 工具 → 聊天模型 → 结构化商品卡）落地过程中的概念辨析与设计原则，供后续回顾。
> 相关：验收报告 [2026-11-01-s4-acceptance-report.md](_build_log/2026-11-01-s4-acceptance-report.md)、
> 交互图 [_pipeline_html/s4-agent-pipeline.html](_pipeline_html/s4-agent-pipeline.html)、
> 冒烟脚本 [backend/scripts/verify_s4.py](../backend/scripts/verify_s4.py)。

## 一、核心概念辨析（Q&A）

### Q1：模型把自然语言解析成结构化参数，这段代码在哪？

**没有这段代码——「解析」是模型做的，我们只声明数据契约。**

传统 NLP 会写槽位填充 parser（正则抠「预算300元」→ `price=300`）。S4 不写，而是用
`ProductSearchArgs` 的 `Field(description=...)` 告诉模型「我想收哪些字段」，让 LLM 在
function-calling 时自己填。代码只做三件事：

| 环节 | 位置 | 内容 |
|---|---|---|
| 声明形状 | [tools.py:20](../backend/src/harbor/agents/tools.py#L20) | `class ProductSearchArgs(BaseModel)` + 每个字段的 `description` |
| 暴露给模型 | [tools.py:114](../backend/src/harbor/agents/tools.py#L114) | `FunctionTool(..., input_schema=ProductSearchArgs)` |
| 接收并转发 | [tools.py:78](../backend/src/harbor/agents/tools.py#L78) | `product_search(...)` → 组装 `ProductSearchSpec` → `service.search()` |

```
自然语言 ──► [LLM 工具调用] ──► ProductSearchArgs（schema 兜住形状）
                                      │ tools.py handoff
                                      ▼
                          ProductSearchSpec ──► S2 硬约束 / S3 检索
```

代价：字段语义不可靠（模型可能臆造类目）。所以靠 prompt 收窄可选答案，而不是写更复杂的解析。

### Q2：AgentLoop（ReAct 循环）的代码在哪？

**在本仓库之外——在 `agentscope` 依赖里。** 我们只写两行：配置 + 触发。

| 我们写的 | 位置 |
|---|---|
| 触发 | [cli.py:180](../backend/src/harbor/agents/cli.py#L180) `await agent.reply(UserMsg(...), structured_schema=ProductCardList)` |
| 配置 | [cli.py:151](../backend/src/harbor/agents/cli.py#L151) `react_config=ReActConfig(max_iters=15)` |

循环本体在 `.venv/.../agentscope/agent/_agent.py`：

| 环节 | 位置 | 做什么 |
|---|---|---|
| 入口 `reply` | `_agent.py:333` | 建 `reply_context`，挂 `structured_schema` 与工具 |
| 循环体 | `_agent.py:1136` | `while True:` 推理-行动循环，直到无工具调用或撞 `max_iters` |
| 决策 `_next_action` | `_agent.py:3512` | 三态：`Reasoning`（问模型）/ `Act`（跑工具）/ `Exit`（收尾） |
| 问模型 `_call_model` | `_agent.py:3291` | 模型要么吐工具调用 JSON，要么吐最终文本 |
| 跑工具 `_execute_tool_call` | `_agent.py:2431`（校验在 `:2499`） | `jsonschema.validate(parsed_input, tool.input_schema)` → `product_search(**args)` |
| 撞上限 | `_agent.py:3604` / `:3636` | 注入 `<system-reminder>`，返回文本，**不抛栈** |

```
        ┌──────────────────────────────────────────────┐
        ▼                                              │
   _next_action ──Reasoning──► _reasoning → _call_model │
        │                            │                 │
        │                     工具调用? ──否──► Exit（文本 / structured_output）
        │                            │是
        │                            ▼
        └──────Act ◄── _execute_tool_call（校验 → product_search）
                                    │ ToolChunk 回填 context
                                    └──► 回到 _next_action
```

### Q3：`run_repl(agent: Agent)` 里的 `Agent` 实现在哪？

**也是 agentscope 的类**，定义在 `_agent.py:118`：

```
run_repl(agent: Agent)                       # cli.py:169
build_agent() 里 Agent(...)                  # cli.py:146
from agentscope.agent import Agent           # cli.py:22
from ._agent import Agent                    # agentscope/agent/__init__.py:3
class Agent:                                 # _agent.py:118  ← 定义处
```

`Agent.__init__(name, system_prompt, model, toolkit, middlewares, state, offloader,
model_config, context_config, react_config, injection_config)`。我们只装配三样：

- `model` —— [models.py](../backend/src/harbor/agents/models.py) 的 `build_chat_model()`（qwen-plus）
- `toolkit` —— [tools.py](../backend/src/harbor/agents/tools.py) 的 `product_search`
- `react_config` —— 循环上限

`Agent` 本身不含业务逻辑，是个「装配槽」。

### Q4：一次参数组装，有哪些 prompt 参与？在哪些地方？

**8 处，4 处我们写的 + 4 处框架注入的。**

**我们写的：**

| # | 位置 | 内容 | 影响 |
|---|---|---|---|
| ① | [prompts.py:5](../backend/src/harbor/agents/prompts.py#L5) `SYSTEM_PROMPT` | 行为指令：理解需求 → 调工具 → 用 `total` 报总数 → 空结果如实说；价格原样引用、不编造 | 全局：调不调、怎么填 |
| ② | [prompts.py:26](../backend/src/harbor/agents/prompts.py#L26) `system_prompt_for()` | **唯一的「组装」逻辑**：注入目录真实类目表，`category 必须是其中之一，或省略` | `category`：防臆造 |
| ③ | [tools.py:23-31](../backend/src/harbor/agents/tools.py#L23-L31) 字段 `description` | 逐字段说明书（`currency`「ISO-4217 三位码」等） | 每个字段各管一个 |
| ④ | [tools.py:109](../backend/src/harbor/agents/tools.py#L109) `FunctionTool(description=)` | 工具整体语义：何时调、参数是什么、返回什么 | 工具选择的依据 |

**框架注入的：**

| # | 位置 | 内容 | 作用 |
|---|---|---|---|
| ⑤ | `_structured_output_tool.py:45` | `GenerateStructuredOutput` 工具描述 + `ProductCardList` schema：「MUST end your response with calling this tool」 | 强制结构化收尾 |
| ⑥ | `_agent.py:1366` `_inject_runtime_state` | `HintBlock` 注入时间 / 任务 / 上下文占用 | 每轮追加，**不动 system prompt**（保 prompt cache） |
| ⑦ | `_agent.py:3718` | `<system-reminder>... maximum of 15 reasoning-acting iterations ... Do not call any tools.</system-reminder>` | S4-4 强制收尾 |
| ⑧ | `_config.py:11` `SummarySchema` | 上下文压缩时的总结字段 | 长对话保住关键约束 |

**拼装点**：`build_agent()` → `Toolkit.get_json_schemas()`（`_toolkit.py:193` 生成
`{"type":"function",...}`）→ 每轮 `_call_model` 发出 `messages=[system(①+②), 历史, hint(⑥⑦)]`
+ `tools=[product_search(③④), GenerateStructuredOutput(⑤)]`。

### Q5：S4-7 的「币种抖动」是怎么回事？

**不是模型抽风，是工具报告漏了一步换算。**

- 目录**多币种**：CNY 1469 / USD 1412 / EUR 1408 / JPY 1408 / SGD 1408。
- `GX-30-1` 原价 `24.93 USD`、`GX-30-2` 原价 `38.08 EUR`。
- 预算过滤（`constraints.matches_facts`）与去重排序（`dedupe.converted_price`）**都已**换算成
  目标币种，唯独工具报告吐原币种 → 模型有时照抄 `24.93 USD`，有时自行换算 `180 CNY`，随机摇摆。
- 修复：`tools._search_result_to_dict` 报告前用 `fx.convert()` 统一换算到 `spec.currency`，
  与过滤/去重三点一线。修复后 3 连真模型输出稳定为 `179.496 / 297.024 CNY`。

### Q6：S4 验收清单有两条口径被收敛，为什么？

| 条目 | 清单原文 | 实际口径 | 理由 |
|---|---|---|---|
| S4-3 | 5 万 token 假数据 → 截断 + 说明「可用 next_page 继续」 | **只报总数，不翻页** | `limit=10` 已在服务层兜住体积；翻页需会话态 + 游标，属后续阶段。`SearchResult.total` = 去重后、截断前计数 |
| S4-5 | 参数类型错 `top_k="五"` | 映射到 `price_max_major="五"` | 工具 schema（`ProductSearchArgs`）**没有 `top_k`**；按「用现有参数测」改用同类数值参数 |

### Q7：S4 的产物在哪里？

| 产物 | 位置 |
|---|---|
| 代码 | `backend/src/harbor/agents/`（tools / cards / prompts / models / permissions / cli） |
| 验收报告 | [docs/_build_log/2026-11-01-s4-acceptance-report.md](_build_log/2026-11-01-s4-acceptance-report.md) |
| 交互图 | [docs/_pipeline_html/s4-agent-pipeline.html](_pipeline_html/s4-agent-pipeline.html)（也已内嵌进报告附录） |
| 冒烟 | [backend/scripts/verify_s4.py](../backend/scripts/verify_s4.py) |
| 单测 | `backend/tests/test_cli.py` + `test_product_search_tool.py`（8 条） |

## 二、设计原则（提炼）

1. **契约优于解析**：能用 schema 声明让模型填，就不写 parser。代码从「实现解析逻辑」退化成
   「声明数据契约」——这是 LLM 应用与传统系统的分水岭。
2. **收窄答案，而不是写长提示**：模型臆造类目会让 S2 硬过滤全清空。解法不是更复杂的解析，
   而是 `system_prompt_for()` 把目录真实类目表塞进去，缩小模型的可选空间。
3. **同一个值只在一处产生（三点一线）**：预算过滤、去重排序、工具报告必须共享同一个
   「最低价 SKU 换算到目标币种」的值。任何一处漏换算，模型就会在两种自洽答案间摇摆。
4. **精度不妥协于观感**：报告 `179.496 CNY` 而非 `179.50`，是 `Decimal` 精确换算，故意不四舍五入
   ——报告值必须与过滤比较值完全一致，否则出现「报告 ¥179.50、内部按 179.496 过滤」的 1 分钱不一致。
5. **分清框架边界**：循环、重试、上限、结构化输出编排在 `agentscope`；工具、产物形状、prompt 在
   我们这侧。改行为时先问「该改哪一侧」，不要试图在应用层重造框架的循环。
6. **prompt 是分散定义的**：同一个字段被 system prompt、工具描述、字段 description 多处同时约束，
   改任何一处都要有全局观。

## 三、边界图（一张图看清谁负责什么）

```
┌──────────────── 我们仓库（harbor） ────────────────┐
│  cli.py        build_agent / run_repl（装配 + REPL）│
│  prompts.py    SYSTEM_PROMPT + 类目词表（① ②）     │
│  tools.py      ProductSearchArgs + product_search   │
│                （③ ④ + SearchResult → JSON）        │
│  cards.py      ProductCardList（结构化产物）        │
│  models.py     build_chat_model（qwen-plus）        │
│  permissions.py 白名单放行（否则工具调用挂起）       │
└──────────────────┬─────────────────────────────────┘
                   │  Agent(name, system_prompt, model, toolkit, react_config)
                   ▼
┌──────────────── agentscope（框架） ─────────────────┐
│  _agent.py            ReAct 循环 / _next_action /   │
│                       _call_model / _execute_tool_call│
│  _structured_output_tool.py  GenerateStructuredOutput│
│  _config.py           ReActConfig / SummarySchema   │
└─────────────────────────────────────────────────────┘
                   │  每轮 _call_model
                   ▼
            qwen-plus（模型）
```

## 四、一句话总结

S4 = 不写解析器，用 schema 声明契约让模型填参；循环交给 agentscope，我们只提供工具、产物形状与
prompt——**契约在应用层，编排在框架层，而同一个值（换算后的价格）必须在三处取同一份**。
