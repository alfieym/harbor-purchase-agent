# S7 设计问答：每次 model call 外层做上下文治理

- 日期：2026-11-03
- 范围：`agents/context_governance.py`（新）+ `agents/factory.py` + `agents/permissions.py`
  + `features/chat/service.py`（异常透传）+ `features/chat/router.py`（错误映射）+ `core/config.py`
- 前置：S6（快照恢复全量历史；上下文只增不减）

## 意图（为什么做）

S6 保证「历史不丢」，代价是它**只增不减**。长会话最终会撞上模型窗口；更危险的是
AgentScope 内置压缩只负责「把旧消息换成一段摘要」，它既不能保证摘要没编造商品，
也不能保证最近两轮和工具调用对还在。用户观察到的坏回答正是这一类：

> 实际没推荐过，却声称「我之前推荐过 X」。

S7 把压缩从「尽力而为」变成**可校验、可回滚、有硬闸门**的治理流程：

1. 旧结果不销毁——压缩前先落成**归档文件**，摘要里留下 `archive://` 指针；
   模型可通过 `read_archive` 回查，人也可以直接打开 JSON。
2. 摘要必须过**来源校验**——摘要里出现的 `sku_id` 必须在被压缩的原文或保留区里出现过；
   提到某个商品的 `sku_id` 时，价格必须与原文一致。校验不过 → 回滚。
3. **容量是闸门**——超过硬上限时，先强制压缩；压完仍超限 → 本轮直接失败（507），
   绝不把超限请求发给模型。
4. **保护边界**——最近两轮用户消息（及其后的全部消息）和完整的工具调用/结果对
   无论如何不进压缩区；压缩后逐条校验，掉了就回滚。

## 关键代码事实（已核实）

- 内置压缩在 `Reasoning` 动作里、每次 model call 之前触发
  （`_agent.py:1186` → `compress_context()`），阈值 `trigger_ratio * context_size`；
  `reserve_ratio * context_size` 决定保留尾部。
- 压缩流程：`_prepare_model_input` 计数 → `_split_context_for_compression` 切分
  → `model.generate_structured_output(summary_schema)` 生成摘要
  → `offloader.offload_context(session_id, msgs_to_compress)` 归档
  → `state.summary = new_summary; state.context = msgs_to_reserve`（`_apply_change`）。
- `on_compress_context(agent, input_kwargs, next_handler)` 能改 `context_config`
  再 `await next_handler(**input_kwargs)`，是**唯一**能在压缩前后拿到完整状态的位置。
- `on_model_call(agent, input_kwargs, next_handler)` 拿得到 `current_model`、
  `messages`、`tools`；可用 `messages=` 改写后转发。它包在 AgentScope 的重试循环里，
  所以闸门抛出后重试会再次进入——用 `_gate_error` 保存原异常，让后续重试立即失败，不再花 API。
- `state.summary` 类型是 `str | list[TextBlock | DataBlock]`。
- `Offloader` 是 Protocol：`offload_context` / `offload_tool_result` / `offload_data_block`。
- `run_turn` 现在把所有 `Exception` 包成 `ModelFailureError`（502）；容量异常必须先
  `except ContextCapacityError: raise` 透传，否则用户看到的是「模型调用失败」。

## 我的裁决（实现按此执行）

| 决策点 | 结论 | 理由 / 代价 |
|---|---|---|
| 触发比例 | `trigger_ratio=0.6` | 给压缩本身留出足够窗口；低于闸门 0.85 |
| 硬闸门 | `gate_ratio=0.85` | 到 0.85 仍压不下去就失败；0.9 是模型窗口的安全线 |
| 保护边界 | 最近 `protect_turns=2` 个 user 消息及其后全部消息 | 满足「最近两轮 + 工具正确调用」；配对关系由 AgentScope 切分保证，我们事后校验 |
| 压缩失败 | `compression_fallback_to_truncation=False`，中间件吞掉异常并回滚 | 截断会直接丢事实；回滚后由闸门决定本轮命运 |
| 校验对象 | **仅 LLM 产出的摘要文本**做「不许编造」检查；校验通过后再追加确定性事实附录 | 若要求 LLM 摘要「覆盖全部 SKU」会频繁误判回滚；覆盖性交给附录保证 |
| 允许出现的 SKU | 被压缩消息 ∪ 保留消息 ∪ 压缩前旧摘要 | 旧摘要里的商品是更早原文的合法延续 |
| 价格校验 | 摘要提到某 SKU 时，必须出现该 SKU 在原文里的价格数字 | 抓「商品对但价格编错」 |
| 归档指针 | `archive://{session}/{seq}`，`session` 经安全化（非法字符替换 + 短哈希） | 模型可读、人可读；避免 `..`、`/` 进路径 |
| 归档布局 | `data/sessions/archives/{session}/{seq:04d}-context.json` | 与 S6 快照同根，`.gitignore` 已覆盖 `data/sessions/` |
| 闸门 HTTP | **507**「上下文超出容量闸门」 | 语义准确；沿用统一错误信封 |
| 事实附录 | 摘要末尾追加 `## 受保护事实（机器生成，未经过模型）` 段落 | 金额/总价/商品信息不会因摘要失败而消失 |
| `read_archive` 权限 | 工具自带 `PermissionDecision(ALLOW)` | 只读归档，无需用户确认 |
| 轮次计数 | 存在 `state.middle_context["harbor.context_governance"]["turn_count"]`，`on_reply` 开头 +1 | S6 从 `context` 数 user 消息；压缩会删掉旧 user 消息，计数会倒退。失败轮随后被逐出，不会落盘 |
| 工具失败归因 | `run_turn` 改为「本轮新增的 tool_result id」而不是 `context[context_start:]` 下标 | 同一原因：压缩会让下标失效，`context_start` 可能越过列表尾部，漏掉本轮 ERROR |

## 组件

### 1. `SessionArchiveOffloader`（实现 `Offloader`）

- `offload_context(session_id, msgs) -> archive_id`
  - 序号 = 该会话目录内已有文件最大序号 + 1（同会话由会话锁串行）；
  - 写 `{seq:04d}-context.json`：`{archive_id, session_id, created_at, message_count, messages}`；
  - `messages` 用 `Msg.model_dump(mode="json")`；
  - 记录 `last_batch`（`archive_id`、`messages`、`created_at`）供中间件校验。
- `offload_tool_result(...)` 写 `{seq:04d}-tool-result.json`，返回同样格式的指针。
- `offload_data_block(block)` 原样返回（本链路无图片）。
- `read(archive_id) -> str`：解析指针、读文件、返回「消息列表 + 每条文本/工具调用」的 JSON，
  文件缺失或指针非法时返回可读错误字符串（模型能看懂并放弃回查，不炸回合）。

### 2. 确定性事实抽取 / 校验

- `product_facts(messages) -> list[ProductFact]`：从 `GenerateStructuredOutput` 的
  tool_call 入参（`cards`）取 `sku_id/title/price_major/currency`；兼容消息上
  `structured_output` 的桩形状。
- `sku_ids_in(text)`：按「id 形状」提取（字母开头、含数字、含分隔符），不绑定目录的
  `P<数字>-S<数字>`；编造判定是「不在来源 id 集合里」，不是「不符合目录正则」。
- `validate_summary(summary, *, source, retained, previous_summary) -> list[str]`：
  - 摘要提到但三处来源都没有的 id → 违规（**编造**）；
  - 摘要提到某 id，却没出现该 id 原文价格 → 违规（**价格错配**）。
- `append_protected_facts(summary, facts, preferences)`：幂等追加事实附录
  （同一 id 去重；已有附录则重写，不重复堆叠）；约束条目经
  `_normalize_preferences` 清洗（去问句 / 语义键去重 / 上限 24 条）。

### 3. `ContextGovernanceMiddleware`

`on_compress_context`：

1. 快照 `state.context` / `state.summary`，记下 `offloader.last_batch` 旧值；
2. 计算保护尾部 token，取 `max(cfg.reserve_ratio, 保护尾部占比)`，关闭截断兜底；
3. `await next_handler(context_config=adjusted)`；
4. 若 `last_batch` 没变 → 本次没压缩，直接返回；
5. 校验保护边界（尾部消息 id 全在、工具对完整）与摘要来源；
6. 任一不过 → 恢复快照、`last_batch` 复位、`logger.warning`，**不抛**；
7. 全过 → `state.summary = append_protected_facts(...)`。

`on_model_call`：

1. `tokens = await current_model.count_tokens(messages, tools)`；≤ 闸门 → 转发；
2. 超限 → 用 `trigger_ratio≈0.01` 的配置强制 `agent.compress_context()`；
3. 用 `agent._prepare_model_input()` 重取 messages/tools，重新计数；
4. 仍超限（或压缩被回滚）→ 置 `_gate_error` 并抛 `ContextCapacityError`；
5. 重试循环再次进入时，`_gate_error` 已置位 → 立即抛，不再调用模型/压缩。

`_gate_error` 在每次 `reply` 开头复位：实现 `on_reply`，在转发前清标志
（`reply` 是外层链，压缩/模型调用都在其内）。

### 4. `read_archive` 工具

`FunctionTool(read_archive, name="read_archive", is_read_only=True, permission=ALLOW)`，
入参 `archive_id: str`，返回 `offloader.read(archive_id)`。在 `build_agent` 里与
`product_search` 一起放进 `Toolkit`。

## 失败语义

| 场景 | 行为 |
|---|---|
| 摘要模型报错 | 回滚到压缩前状态；闸门下继续或 507 |
| 摘要编造 SKU / 价格错配 | 回滚；同上 |
| 保护尾部被切走 / 工具对被拆 | 回滚；同上 |
| 归档写失败 | 压缩整体失败 → 回滚 |
| 超闸门且压不下去 | `ContextCapacityError` → 507，会话被逐出（S6 失败语义） |

## 追问：强制压缩之后还会超限吗？超限后发生什么？

会。`on_model_call` 的顺序是「强制压缩一次 → 重新计数 → 仍超就失败」。压缩的目标不是
「压到闸门以下」，而是「按比例保留尾部、把更早的消息换成摘要」。四种情况会让最终计数
仍然高于 `0.85 × context_size`：

| # | 原因 | 说明 |
|---|---|---|
| 1 | 不可压缩的下限高于闸门 | system prompt、工具 schema、最近 `protect_turns` 轮及其后全部消息、摘要本身都不参与压缩。它们的合计超过闸门时，压缩无能为力。 |
| 2 | 压缩被回滚 | 摘要报错 / 编造 SKU / 价格错配 / 边界被切 / 归档写失败 → 恢复压缩前状态；压缩前已超限，恢复后依旧超限。 |
| 3 | 压了但不够 | 内置切分依据 `reserve_ratio`，不是闸门；`_protect_boundary` 把 reserve 上限卡在 `trigger - 0.01`。摘要本身偏大或保留区比预估大，结果仍可能 > 0.85。 |
| 4 | 一次调用只压一次 | 强制压缩只做一次；`_gate_error` 让后续重试立即失败，不会「再压一轮」。 |

### 代码路径

```
on_model_call        tokens > limit
  └─ _gate_error = ContextCapacityError(tokens, limit); raise
     └─ run_turn     except ContextCapacityError: raise   # 不包成 502
        └─ 外层      store.evict(key); raise
           └─ router HTTPException(507, "上下文超出容量闸门")
              └─ core/errors.py 统一信封
```

- 工厂未设置 Agent 的 `model_config`，`max_retries` 默认 0：抛一次即出。若将来调大，
  `_gate_error` 保证重试立即失败，不重复压缩、不重复计数。
- 本轮不写快照、不写流水。
- 会话从内存逐出，下次请求从最后一轮成功的快照重建。
- `on_reply` 已给 `turn_count` +1，但会话逐出使这个增量不落盘，不会污染流水。

### 用户端感知

```json
// HTTP 507
{"error": {"code": "http_507", "message": "上下文超出容量闸门"}}
```

1. 这一问没有回答：没有 `final_text`、没有卡片，也没有半截回复。
2. 历史没丢：之前成功轮次的快照、轮次计数、商品卡都还在。
3. 重试没用：下一轮从同一份快照重建，同样输入走到同样位置，仍是 507——确定性失败，
   不是抖动。
4. 唯一自救是开新会话（新的 `shopping_session_id`），但当前没有任何提示引导用户这样做。
5. 前端对话页尚未实现；`docs/api-contract.md` 只记录了 503/504/502，**没有 507**。

### 三个实现细节

- **闸门刻度**：`build_chat_model` 未传 `context_size`，用 `DeepSeekChatModel` 的构造默认值
  65536；因此 `limit = int(65536 × 0.85) = 55,705`，`trigger ≈ 39,321`（均为估算 token）。
  模型卡里的 1,000,000 并未生效。
- **计数方式是 bytes/4**：中文一个字 3 字节 ≈ 0.75 token，真实分词器通常按 ~1 token 计，
  闸门对中文偏松。它防的是数量级溢出，不是 provider 的精确上限。
- **507 是终态**：设计上「宁可失败也不发超限请求」；若产品要求用户可继续，需要会话轮转
  （auto-rollover）或至少一个「开新会话」的明确出口。

### 待办

- 把 507 补进 `docs/api-contract.md`，前端错误文案同步。
- 度量「system + tools + 保护尾部」的占比；它超过 `gate_ratio` 时，压缩已救不了这一轮，
  应在更早的时机给出信号（或直接会话轮转）。

## 验收清单校准（2026-11-03）

落地后收到 S7 验收清单（来自 GlobeX 复刻路线）。逐条对齐后，3 条需要新增实现、
2 条需要补证据，以清单为准：

| 清单 | 结论 | 落地 |
|---|---|---|
| S7-1 40 轮长对话不 400 | 补证据 | 离线 40 轮长压测试（真实压缩管线，窗口缩到 8000） |
| S7-2 摘要五字段 | 补实现 | `SUMMARY_TEMPLATE` 改为中文五段 |
| S7-3 预算/product_id 答对 | 补实现 | 附录扩展为「商品 + 会话约束」，且跨压缩只增不减 |
| S7-4 记录前后 token / 耗时 | 新增 | `on_compress_context` 打 INFO 遥测行 |
| S7-5 `legacy` 开关 | 新增 | `HARBOR_CONTEXT_STRATEGY=governed\|legacy` |
| S7-6 前缀稳定 | 补证据 | 压缩前后 system 文本 + tools 序列化逐字比较 |
| S7-7 偏好以 UserMsg 注入 | 新增 | 每次 model call 末尾追加，不落盘、不进 system prompt |

### 1. 策略开关（S7-5）

新增 `context_strategy: Literal["governed", "legacy"]`，默认 `governed`。
`legacy` 是 **S7 之前的基线**：不挂 `ContextGovernanceMiddleware`、不注册
`read_archive`、不传 `context_config`（用 AgentScope 默认的 0.8 触发、允许截断兜底），
系统提示里也不提归档。代价：`legacy` 下没有闸门，超限请求可能直达模型——这正是
「回到压缩前基线」的定义，不是退化。

### 2. 压缩遥测（S7-4）

每次真正发生压缩（含失败与回滚）打一行 INFO：

```
context compression ok: tokens 5120 -> 1830, 12.4 ms, archived 5 messages (archive://.../0001)
```

`result ∈ {ok, failed, rolled_back}`；失败/回滚时 `after == before`（状态已恢复）。
计数仍是 `bytes/4` 估算，不是 provider 精确 tokenizer。

### 3. 关键事实的覆盖与延续（S7-3）

- 附录从「商品事实」扩展为「商品 + 会话约束」。约束句从**用户消息**里按
  `预算|以内|以下|不超过|最多|不要|避开|偏好|喜欢|轻一点|重量|材质` 切出，**原样保留**，
  不改写。只扫 user 文本块，工具结果不会混进来。
- 修复了一个真实缺口：第二次压缩时 `product_facts(record.messages) +
  product_facts(retained)` 看不到第一次压缩留下的商品，附录会悄悄变短。现在额外合并
  `facts_from_appendix(before_summary)`，附录**跨压缩只增不减**，产品与约束都不会丢。
- 限制：抽取是提示词驱动的启发式。用户用完全不含上述线索的说法表达预算（例如
  「别超过两千」里的「别超过」）可能漏掉；漏掉时仍由 LLM 摘要兜底，但不是确定性保证。
- 约束列表的卫生：疑问句不算约束（`我预算多少？` 含「预算」但不是约束），
  同一约束的多次复述按语义键合并，列表上限 24 条（保留最近）。

### 4. 五字段摘要（S7-2）

AgentScope 默认 schema 就是五个字段；这次把渲染模板改成中文，名称与清单一致：
`# 目标 / # 当前进展 / # 关键事实 / # 下一步 / # 必须保留`。schema 字段名不变
（`task_overview / current_state / important_discoveries / next_steps /
context_to_preserve`）。

### 5. 偏好注入（S7-7）

来源是**当前会话里用户的原话**（会话作用域，不落长期记忆——S8 才做）。每次
`on_model_call` 在消息末尾追加一条 `UserMsg`：

```
<system-info>会话偏好（用户原话，逐字保留）：
- 预算 2000 以内
</system-info>
```

它不写回 `state.context`、不进 system prompt、不落快照；下一次 model call 重新生成。
因此前缀（system prompt + 工具定义）保持稳定，缓存可复用。

### 6. 前缀稳定（S7-6）

压缩与注入都不改 system prompt、不改工具定义顺序；测试比较压缩前后
`_prepare_model_input()` 的 system 文本与 tools 的 JSON 序列化，要求逐字相等。
摘要插入在 system 之后、注入追加在末尾，二者都在可变区。

## 真额度验收记录与两个保真盲区（2026-11-03，已修复）

真额度验收（`scripts/accept_s7_real.py`，真实 qwen-plus + 真实压缩 + 真实检索）7 条
全部通过，详见 [验收报告](_build_log/2026-11-03-s7-acceptance-report.md)。第一轮
（检索为桩）暴露了两个 S7 **自己的**盲区，已修复并用单元测试钉住。

### 1. SKU 形状写死 → 事实以卡片中的 id 为准（已修复）

真模型把桩里的 `selected_sku_id: P2175-S1` 写成了 `sku_2175_b01`。修复前：

| 位置 | 后果 |
|---|---|
| `SKU_PATTERN`（`validate_summary`） | 这种 id 不被提取 → 不校验来源、不校验价格；编造也永不回滚 |
| `_APPENDIX_PRODUCT`（`facts_from_appendix`） | 附录里的该行读不回来 → 再次压缩时该商品从附录静默消失 |

现在的做法：

- `sku_ids_in(text)` 按「id 形状」识别——字母开头、含至少一个数字、含分隔符
  （`[_-]`）。`P3472`（product id，无分隔符）不会误当 SKU；`P3472-S1`、
  `sku_2175_b01` 都会被识别。
- 校验的**允许集合**取来源消息/保留区/旧摘要里实际出现的 id，而不是用目录正则
  去预测「合法 id 长什么样」。事实集合本身来自 `product_facts()`（卡片中的
  `sku_id` 原样保留），与形状无关。
- 附录解析改成通用 id（`[^\s|]+`），`facts_from_appendix()` 能读回任意形状，
  「附录跨压缩只增不减」不再依赖目录正则。
- 价格比对前先用同一形状正则剥离 id，避免 id 里的数字污染金额集合。

残留风险：形状识别比固定正则宽，`UTF-8` 这类普通 token 若出现在摘要、又不在来源里，
会被判为编造并触发回滚。购买对话里概率很低；真出现误回滚时，收窄 `_SKU_LIKE`
（例如要求 id 含 `sku`/`P` 前缀 + 数字 + 分隔符）即可。

### 2. `会话约束` 附录膨胀，且把提问当约束（已修复）

第一轮真额度附录累计 30 行 `- 会话约束：…`。修复前两个成因：

- 去重是逐字的：用户每轮以不同前缀重复同一约束（`第N轮：`），逐字不同 → 全保留。
- 线索词命中不看句式：`我预算多少？` 命中「预算」，于是**提问**被当成约束写入附录，
  并随每次 model call 注入到消息末尾。

现在的做法（`_normalize_preferences`，附录与注入共用）：

- `_preference_key()` 去掉 `第N轮：`/`第N次：` 前缀后作为语义键去重，
  同一约束的多次复述只留一条；
- `_QUESTION_CUES`（多少 / 吗 / 哪 / 怎么 / 是否 / 几件…）跳过疑问句；
- `MAX_PREFERENCES = 24`：去重后只保留最近 24 条。上限在正常会话里不会触发，
  但它保证最坏情况下注入块仍有界。

第二轮真额度实测：附录约束从 30 行降到 **2 行**（原始预算 + 一条历史复述）。

### 3. 缩小窗口时保护边界回滚是预期行为

把 `context_size` 缩到 8000 跑真模型时，`system + tools + 最近两轮` 在
`trigger - 0.01` 里的占比明显变大，`_protect_boundary` 会更频繁地卡在上限并触发
回滚（早先几轮实测：回滚后或被强制压缩救回，或 507）。这不是缺陷，而是「保护
尾部属于容量下限」的直接体现；真实 131072 窗口下同样的尾部占比小得多。

### 4. 上游待办：卡片里的 `sku_id` 其实是 product_id

真实检索下模型把卡片写成 `sku_id: P2175`（product id），而不是工具返回的
`selected_sku_id: P2175-S1`。S7 忠实保留了「模型实际写下的 id」；但下单需要真正的
SKU，这属于提示词/卡片契约层面，另行处理。

## 显式不在 S7

- 不做跨会话归档检索、不做归档文件的自动清理（后续运维项）。
- 不做摘要质量打分（LLM-as-judge）；只做确定性来源校验。
- 不改 AgentScope 内置压缩算法本身。
