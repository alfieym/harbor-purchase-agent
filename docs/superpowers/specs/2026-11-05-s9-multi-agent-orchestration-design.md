# S9 多 Agent 编排 — 设计文档

- 日期：2026-11-05
- 状态：待评审
- 前置：S4 工具面、S6 会话快照、S7 上下文治理、S8 长期偏好记忆
- 后续：S10 交易闭环（订单能力）

## 1. 意图与为什么

主 Agent 的上下文窗口是稀缺资源。一次「帮我把母婴、家电两个类目都找一遍，并交叉比价」的
请求，如果由主 Agent 串行做完，中间会塞进几十条商品、价格、过滤原因——这些都不是用户要的，
却会挤掉真正重要的历史与偏好。

S9 的解法不是「让更多 Agent 一起说话」，而是**把一段可以独立完成、链路够深、又不需要主
对话历史的活，打包交给一个没有对话包袱的执行者；它只把结论和可核对的证据带回来**。

一句话：**上下文隔离是为了让主 Agent 少记东西，不是为了多一个 Agent。**

### 1.1 「上下文隔离」在这里的准确含义

隔离是双向的：

- **上行**：子 Agent 的工具调用、候选商品、试错过程一律不进主上下文；只回
  `summary + evidence_refs + verified_product_ids`。
- **下行**：主 Agent 不递整段对话，只递一份为本次任务现裁的**结构化事实**——预算、品类、
  币种、排除材质、已知商品 id，加上买家长期偏好的只读快照。

把整段对话递给子 Agent 不是「共享上下文」，是「复制上下文窗口」：它破坏的正是
「是否可并行 / 是否需要隔离 / 链路是否够深」这三条派活判据里要省的那笔 token。

长期记忆与对话历史要分开看：长期偏好是按键（`buyer_id`）可寻址的外部事实，共享它不损失
隔离；对话历史是本次会话的轨迹，共享它就损失隔离。

## 2. 目标 / 非目标 / 反目标

### 目标

1. 新增工具 `task_dispatch`：一次调用携带**一批**互不依赖的任务，并发执行，默认并发 3。
2. 新增两套 prompt：主 Agent 的**派活纪律**与子 Agent 的**交差契约**。
3. 子 Agent 只读、无对话历史、不可递归派活；只持有 `product_search`。
4. 子 Agent 的结论必须带证据；商品 id 由 dispatch 层与真实工具返回值交叉核验，
   核不过的标 `unverified`。
5. 子 Agent 失败/超时降级为一条失败结果，整轮不失败。
6. 派发计划写进 `AgentState.middle_context`，重启后工具与计划都能恢复。

### 非目标

- **不实现「查单」/订单/购物车/交易闭环**——仓库当前没有订单数据源与订单工具，这是 S10
  的范围。因此 S9 的触发场景里「检索 + 交易混合」在 S10 之前不可用；S9-1 的并行验收用
  「多品类并行寻货」替代。
- 不做递归派活（子 Agent 的 toolkit 里没有 `task_dispatch`）。
- 不做子 Agent 的流式输出、人工中断、跨进程任务队列。
- 不改变 `product_search` 对主 Agent 的现有行为。
- 不把派发计划做成跨会话的持久任务系统；它跟随 S6 的会话快照。

### 反目标（技术上能跑也算失败）

- 子 Agent 的中间工具结果出现在主上下文里。
- 子 Agent 能自己声明 `verified`。
- 简单请求（单次检索）也触发派发。
- 一个子 Agent 超时导致整轮 502。
- 两个并发派发互相覆盖计划。

## 3. 触发判据

四个场景说明「什么时候值得看一眼」：

- 多目标比价（同一类目、多个候选维度）
- 多品类并行寻货
- 检索 + 交易混合（S10 前不可用）
- 大范围搜索（需要多轮换词、扩缩条件）

真正决定派不派活的是三条判据，**必须同时成立**：

| 判据 | 含义 | 不成立时 |
|---|---|---|
| 可并行 | 任务之间没有先后依赖，拆开不改变结论 | 主 Agent 串行自己做 |
| 需要上下文隔离 | 中间过程很长，留在主上下文会挤掉重要信息 | 主 Agent 自己做 |
| 链路够深 | 子任务需要多轮工具调用/换词/筛选 | 主 Agent 一次工具调用做完 |

任一不成立 → 主 Agent 自己做。这条只写在主 Agent 的 prompt 里，**代码不做关键词启发式
拦截**：判断权在模型，代码负责把成本与后果写清楚。

## 4. `task_dispatch` 契约

### 4.1 输入

```python
class DispatchTaskArgs(BaseModel):
    goal: str                      # 一句话任务说明，自包含
    category: str | None = None
    price_max_major: float | None = None
    currency: str = "CNY"
    weight_max_kg: float | None = None
    excluded_material_tags: list[str] = Field(default_factory=list)
    known_product_ids: list[str] = Field(default_factory=list)

class TaskDispatchArgs(BaseModel):
    tasks: list[DispatchTaskArgs]  # 1..dispatch_max_tasks
```

约束：

- `goal` 必须自包含：子 Agent 没有用户、没有历史，不知道「刚才那个」「上面说的」指什么。
- 硬约束用结构化字段传，不用自然语言重述；dispatch 层还会把买家长期 dislike 的材质
  合并进 `excluded_material_tags`（代码合并，不依赖子 Agent 记得）。
- `known_product_ids` 是**参考信息，不是硬过滤**：`ProductSearchSpec` 没有「排除这些 id」
  的字段，dispatch 层也不做后置删除。子 Agent 被要求把它们当作「用户已经见过」的上下文，
  除非明显更优，否则不重复推荐。
- 任务之间不得有依赖；`t1` 的结果不能是 `t2` 的输入。需要依赖就由主 Agent 串行做。

### 4.2 输出（主 Agent 唯一能看到的东西）

```json
{
  "dispatched": 2,
  "concurrency": 3,
  "elapsed_ms": 4210,
  "results": [
    {
      "task_id": "t1",
      "ok": true,
      "summary": "母婴宠物类目找到 3 款满足预算的饮水器，最低价 89 元。",
      "evidence_refs": [
        {
          "product_id": "P2175",
          "field": "price_major",
          "value": 89.0,
          "statement": "最低价 SKU 89 元",
          "source": "product_search",
          "verified": true
        }
      ],
      "verified_product_ids": ["P2175", "P2201", "P2210"]
    }
  ]
}
```

- 每个任务的 payload 只有 `summary`、`evidence_refs`、`verified_product_ids` 三项
  （外加 `task_id` / `ok` 信封字段）。子 Agent 的原始工具结果、商品列表、试错记录
  一律不出现。
- 失败任务形如：
  ```json
  {"task_id": "t2", "ok": false, "summary": "子任务超时（90s）",
   "evidence_refs": [], "verified_product_ids": []}
  ```
  它仍然是 `ToolResultState.SUCCESS`，避免 S5 的 ERROR→502 路径。

### 4.3 子 Agent 的内部交差格式

子 Agent 不直接产出 `evidence_refs`，它产出**声明**：

```json
{
  "summary": "……",
  "claims": [
    {"product_id": "P2175", "field": "price_major", "value": 89.0,
     "statement": "最低价 SKU 89 元"}
  ]
}
```

`verified` 由 dispatch 层附加。原因：`verified` 若由子 Agent 自己写，它就能自证；
这个字段必须来自工具返回值这一侧的事实。

## 5. 证据校验

dispatch 层给每个子 Agent 包一层 `product_search`，在真实调用返回时记录
`product_id → 该商品字段`（来自 `_search_result_to_dict` 的 hits）。子 Agent 结束后：

1. `verified_product_ids` = 该任务所有工具返回中出现过的 `product_id` 去重集合。
2. 对每条 claim：
   - id 不在集合里 → `verified: false`，进 `unverified` 语义（不单独开字段，
     由 `evidence_refs[].verified` 表达），并从 `verified_product_ids` 中不可见地排除。
   - id 在集合里且 claim 带 `field/value` → 与工具返回的同名字段比对，相等才
     `verified: true`。
   - id 在集合里但没有 `field/value` → 只核 id，`verified: true`。
3. `summary` 原样返回；它属于结论，不属于证据，不参与核验。

这意味着一个编造的 `product_id`、或一个真实 id 配一个错价格，都会在
`evidence_refs` 里留下 `verified: false`，主 Agent 在 prompt 纪律下不得把它当事实转述。

## 6. 子 Agent 的构造

每个任务一个全新的 `Agent`：

- `name = f"dispatch_{task_id}"`
- `system_prompt = SUBAGENT_SYSTEM_PROMPT + brief_block + vocabulary_block + memory_block`
  - `brief_block`：`goal` 加上本任务的 `category / price_max_major / currency /
    weight_max_kg / excluded_material_tags / known_product_ids`，逐项写明「这是硬约束，
    不得自行放宽」。
  - `vocabulary_block`：商品目录真实类目（与主 prompt 的 S4 教训同源）。没有它，
    子 Agent 会发明类目，硬过滤把所有候选都筛掉。
  - `memory_block`：买家长期偏好/材质 dislike 的只读快照（最多 24 条，与 S8 注入同源）。
  - 不包含主会话的对话内容，也不包含主 Agent 的工具清单。
- `toolkit = Toolkit([sub_product_search])`
  - 与主 Agent 同一个 `CatalogSearchService`；没有偏好写工具、没有 `read_archive`、
    没有 `task_dispatch`。
  - `make_product_search_tool(..., fixed_constraints=...)`：把任务里的
    `category / price_max_major / currency / weight_max_kg` 与合并后的
    `excluded_material_tags` 一起**固定在工具层**。模型传了别的值也不覆盖；
    它想放宽约束时只能接受空结果并如实报告缺口。理由与 S8-8 相同：硬约束是代码事实，
    不是模型的自律。
- `state = AgentState()`（全新，无 `middle_context`、无 buyer 身份）
- `react_config = ReActConfig(max_iters=6, interruption_raise_cancelled_error=True)`
- 每个任务 `asyncio.wait_for(..., dispatch_task_timeout_seconds)`，计时从拿到
  semaphore 之后开始——排队时间不计入单任务预算。

### 6.1 并发

- `asyncio.Semaphore(dispatch_max_concurrency)` 包住**整个子 Agent 运行**，不是只包
  模型调用；超出的任务在本地排队，避免同时打满模型网关。
- `asyncio.gather(..., return_exceptions=True)`：单个任务异常转成 `ok: false`。
- **批次级截止时间**：`asyncio.wait_for(gather, dispatch_batch_timeout_seconds)`。
  单任务 90s × 两波（6 个任务 / 并发 3）会超过父回合的 120s，把整轮打成 504——
  这正是降级表要避免的结果。批次超时后取消未完成任务，它们各自返回
  `{"ok": false, "summary": "批次超时未完成"}`，已完成的任务结果照常返回。
  默认批次预算 75s，给父回合留出汇总与模型回答的余量。
- 默认并发 3；配置项可调，网关侧限流仍由模型客户端自己重试。

## 7. 两套 prompt

### 7.1 主 Agent：`DISPATCH_GUIDANCE`

追加在 `system_prompt_for(...)` 的尾部，仅在 `dispatch_enabled` 且 `governed` 时出现。
内容分四段：

1. **什么时候找人帮忙**：四个场景 + 三条判据必须同时成立；任一不成立自己做。
2. **怎么把活说明白**：任务自包含；预算/品类/币种/排除项用字段填；不要写「刚才那个」；
   不要在一个任务里塞两个目标。
3. **怎么不信人家的话**：只把 `verified: true` 的 `evidence_refs` 当事实；`summary`
   是转述不是证据；`verified: false` 的商品必须标「未核实」或直接不提。
4. **失败怎么办**：`ok: false` 的任务由主 Agent 决定重派、自己做、或向用户说明缺口；
   不得假装成功。

### 7.2 子 Agent：`SUBAGENT_SYSTEM_PROMPT`

单独一份，不拼接主 prompt。内容：

- 你没有用户，也没有对话历史；你只有这一条任务说明。
- 你只能调用 `product_search`；不要问问题、不要等确认、不要派活。
- 先想清楚要换几种问法/条件，再检索；拿到结果后按任务里的硬约束自查一遍。
- 交差格式固定为 JSON：`{"summary": "...", "claims": [...]}`；没有找到就
  `claims: []` 并在 summary 里说明缺口。
- 不要编造 `product_id`；只引用工具真实返回过的商品。

## 8. 失败与降级

| 情况 | 行为 |
|---|---|
| 子 Agent 抛异常 | 该任务 `ok: false`，其余任务照常返回 |
| 单任务超时 | `ok: false`，summary 写明超时 |
| 模型网关限流 | 交给模型客户端重试；本地 semaphore 控制并发 |
| 所有任务失败 | 仍是成功 ToolChunk；主 Agent 按 prompt 自己做或向用户说明 |
| 任务数超过上限 | 工具返回 `ok: false` 的 SUCCESS 结果，提示拆批；不静默截断 |

## 9. 并发写与 S9-6

子 Agent 是只读的：全新 `AgentState`、toolkit 里没有任何写 state 的工具。因此
「两个子 Agent 同时写同一 session」在结构上不可能发生——测试要证明的是**写路径不存在**，
而不是锁住了它。

唯一会写父会话 `middle_context` 的是 `task_dispatch` 自己（写派发计划）。防护：

- `FunctionTool` 默认 `is_concurrency_safe=True`，同一轮里模型发出的两个
  `task_dispatch` 调用**确实会并发执行**——所以不能假设串行。
- 每个 `AgentState` 一把进程内 `asyncio.Lock`（按 state 标识登记）。
- **任务 id 分两层**：返回给主 Agent 的仍是 `t1..tn`；写入计划块时用批次前缀
  `b{revision}.{task_id}` 作为键。每次 dispatch 在锁内领取新的 `revision`，
  两个并发批次的 `t1` 不会互相覆盖。
- 写计划前在锁内重新读取当前块，按批次键合并，`revision + 1`，整块替换。
- 计划块只保留最近 N 个批次（默认 5），避免随会话无限增长；更早的批次在恢复
  prompt 里以「历史批次已省略」带过。

## 10. 持久化与重启恢复（S9-7）

计划块：

```python
state.middle_context["harbor.dispatch"] = {
    "revision": 3,
    "batches": {
        "b3": {
            "t1": {
                "goal": "母婴宠物类目找预算 500 以内的饮水器",
                "status": "ok",        # running | ok | failed | timeout
                "started_at": "...",
                "finished_at": "...",
                "verified_product_ids": ["P2175"],
                "summary": "..."
            }
        }
    }
}
```

- 工具恢复：`build_agent(runtime, state)` 每次都按配置重建 toolkit；重启后
  `task_dispatch` 自然在场（`dispatch_enabled` 且 `governed`）。
- 计划恢复：`build_agent` 读取该块，把最近批次里未完成/失败的任务作为「上次派发计划」
  追加进主 Agent 的 system prompt，提示它重派或向用户说明。
- **不新增中途持久化**：`SessionStore.persist` 只在整轮成功后写快照，失败轮次会
  evict 而不落盘。因此 S9 不承诺「进程死在子 Agent 运行中，重启后看到 `running`」；
  能恢复的是**上一轮成功快照里的计划**。这保住 S6 的写入语义，也不改
  `agent_factory(state)` 与 `SessionStore` 的签名。
- 快照里的 `running` 只可能来自「本轮尚未结束、由测试或未来的增量持久化写入」；
  恢复逻辑把它当作「需要重派」处理，不当作成功。

## 11. 配置

```python
dispatch_enabled: bool = True          # HARBOR_DISPATCH_ENABLED
dispatch_max_concurrency: int = 3      # HARBOR_DISPATCH_MAX_CONCURRENCY, ge=1, le=8
dispatch_task_timeout_seconds: float = 90.0
dispatch_batch_timeout_seconds: float = 75.0   # < turn_timeout_seconds(120)
dispatch_max_tasks: int = 6            # 单批上限, ge=1, le=12
```

`dispatch_batch_timeout_seconds` 必须小于 `turn_timeout_seconds`；二者关系在
`Settings` 初始化时用校验器检查，配反了直接报配置错误，而不是运行时 504。

`legacy` 策略下不注册 `task_dispatch`，也不追加 `DISPATCH_GUIDANCE`（保持 S7-5 基线）。

## 12. 文件结构

| 文件 | 变更 |
|---|---|
| `backend/src/harbor/agents/dispatch.py` | 新增：`TaskBrief`、`DispatchLedger`、`make_task_dispatch_tool`、子 Agent 构造与证据核验 |
| `backend/src/harbor/agents/prompts.py` | 修改：`DISPATCH_GUIDANCE`、`SUBAGENT_SYSTEM_PROMPT`、`system_prompt_for(dispatch_enabled=..., dispatch_ledger=...)` |
| `backend/src/harbor/agents/tools.py` | 修改：`make_product_search_tool(..., fixed_constraints=...)` 支持固定类目/预算/币种/重量/排除材质 |
| `backend/src/harbor/agents/factory.py` | 修改：注册 `task_dispatch`；从 state 恢复计划并传给 prompt |
| `backend/src/harbor/core/config.py` | 修改：四个配置项 |
| `backend/tests/test_dispatch.py` | 新增：批处理、并发、证据核验、失败降级、写锁 |
| `backend/tests/test_s9_acceptance.py` | 新增：S9-1..S9-7 逐条 |
| `backend/scripts/verify_s9.py` | 新增：离线手工验证（假模型/假 runner，零 API） |
| `backend/scripts/accept_s9_real.py` | 新增：真模型验收（零 embedding） |

## 13. 验收映射

| ID | 清单 | 验证方式 |
|---|---|---|
| S9-1 | 一批任务并发，总耗时明显小于串行 | 离线：注入 `sleep(0.4)` 的 fake runner，并发 < 0.65s，串行 > 0.75s；真模型：两类目检索计时 |
| S9-2 | 主上下文只有 summary + evidence_refs + verified_product_ids | 断言工具输出 JSON 的字段白名单；断言子 Agent 原始 hits 不出现在任一 message |
| S9-3 | 声称的商品 id 必须在 verified_product_ids；否则 unverified | fake runner 返回一个真实 id + 一个编造 id + 一个错价格；断言对应 `verified` 布尔 |
| S9-4 | 子 Agent 抛异常，整轮不失败 | fake runner 第二个任务 raise；断言 `ok: false` + 另一任务 `ok: true` |
| S9-5 | 简单请求不派活 | prompt 内容断言 + 真模型跑单次检索，检查消息历史里没有 `task_dispatch` 调用 |
| S9-6 | 并发写同一 session 不覆盖 | 断言子 Agent toolkit 无写工具；两个并发 dispatch 的计划块都保留 |
| S9-7 | 重启后恢复工具与计划 | `state.model_dump()` → 新 state → `build_agent`；断言 toolkit 含 `task_dispatch`，prompt 含上次计划 |

## 14. 交给实现者的决定（我的判断）

- 任务 id 由 dispatch 层按顺序生成 `t1..tn`，不让模型填，避免重复/伪造；
  计划块内部用批次前缀 `b{revision}.{task_id}` 保证并发批次不撞键。
- `evidence_refs` 的 `field` 用 `product_search` 输出里已有的键名
  （`price_major`、`title`、`brand`、`weight_kg`、`selected_sku_id`、`score`）。
- `summary` 限制在 200 字以内（prompt 约束）；工具层只做截断兜底，不因超长判失败。
- 字段级核验只覆盖 `product_search` 返回的字段；不核验模型对商品的自由描述。
- 子 Agent 的 `max_iters=6`；超过就把已有结论交回，不无限换词。
- `dispatch_enabled` 默认开，但只在 `governed` 下生效。

## 15. 已知取舍

- **不做跨进程任务恢复**：`SessionStore` 只在整轮成功后落快照，所以进程死在子任务运行
  中时，本轮计划根本不会进快照；能恢复的只有上一轮成功快照里的计划。正在跑的子任务不会
  被续跑，由主 Agent 在下一轮决定重派。
- **不核验自由文本**：`summary` 里的措辞无法逐句核验，只有 `claims` 里的结构化字段
  可核。这是「给结论和证据」而非「给逐句引用」的必然代价。
- **S9-1 的并行验收不含交易侧**：`查单` 属于 S10；当前用多品类检索证明并发收益。
- **子 Agent 不读归档**：它没有主会话的对话轨迹，归档对它没有意义。
