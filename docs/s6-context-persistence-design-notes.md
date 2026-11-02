# S6 设计问答：模型上下文的持久化层

- 日期：2026-11-02
- 范围：`features/chat/` 持久化（快照 + 流水）+ `agents/factory.py` 状态恢复 + `core/config.py` 新配置
- 前置：S5（HTTP 会话 + 内存 `SessionStore`，重启即丢）

## 意图（为什么做）

S5 的会话状态只在内存里（`SessionStore` 是进程内 dict），进程一重启，所有会话历史、
工具调用、中间上下文全部丢失。用户「在原有会话中继续回答」的前提是：重启后，同一个
`(buyer_id, shopping_session_id)` 能找回完整上下文，继续对话而不是从零开始。

所以 S6 把「模型上下文」落盘成两份持久化产物：

1. **快照（snapshot）**——`AgentState` 的 JSON 快照，是**恢复的唯一真相源**。
   内容 = AgentScope 自己声明「该保存和加载」的完整状态：消息数组（`context`）、
   工具调用（`context` 里的 tool_result / tool_use 块）、中间上下文（`middle_context`）、
   待确认状态（`reply_context` / `permission_context` / `tool_context` / `summary`）。
2. **流水（journal）**——一问一答两行的 JSONL，只用于**复盘与评测**，不参与恢复。

## 主流程

```
请求 (buyer_id, shopping_session_id, raw_query)
  → 读快照：命中则恢复 Agent，未命中则新建
  → 回答（run_turn 现有逻辑：Agent.reply + 工具失败检测 + 结构化解析）
  → 写快照（成功才写，原子覆盖）
  → 写流水（成功才写，追加两行）
  → 返回 final_text + cards
```

## 目标 / 非目标 / 反目标

**目标**
1. 进程重启后，同会话能恢复**完整历史**并继续对话（不丢消息/工具调用/中间状态）。
2. 快照写入是**原子**的（temp + rename），崩溃不产生半截 JSON。
3. 快照失败是**硬错误**（5xx）：客户端不能以为这轮已经存好，实际没存。
4. 流水失败是**软错误**（告警 + 200）：流水只服务复盘/评测，不挡正常回答。

**非目标**
- 不做上下文治理 / 截断 / 摘要 / 增长控制——**用户明确：留到 S7**。S6 忠实地存全量历史。
- 不做多进程/多实例共享存储（快照是本机文件系统，无并发写者）。
- 不做快照的版本迁移（`schema_version` 先占位，S7 再谈迁移）。

**反目标（做成这样就算失败）**
- 恢复后 Agent 行为与「进程内连续对话」不一致（例如丢工具调用、丢 structured schema 状态）。
- 快照写失败却返回 200，用户以为历史已保存，重启后发现没了。
- 快照被截断/损坏后无法恢复，且没有报错信号。

## 约束（已核实的代码事实）

- **`AgentState` 是 pydantic `BaseModel`**（`agentscope/state/_state.py`），字段：
  `session_id`、`summary`、`context: list[Msg]`、`reply_context`、`permission_context`、
  `tool_context`、`tasks_context`、`middle_context`。docstring 明说它是「应该被保存和加载」的状态。
- **`Agent.__init__` 直接吃 state**：`self.state = state or AgentState()`，且 `reply` 全程读
  `self.state.context`（没有单独的 `memory` 属性要重建）——所以恢复 = 把快照
  `model_validate` 成 `AgentState` 再传给 `Agent(state=...)` 即可。
- **`build_agent(runtime)`（`agents/factory.py`）** 现在只建「全新」Agent；要支持恢复，需给它加
  `state: AgentState | None = None` 参数并透传给 `Agent(...)`。它建完还会调
  `allow_product_search_tool(agent)`（给 permission engine 放行工具）——恢复后的 Agent 也要走这一步，
  所以恢复路径应复用 `build_agent(runtime, state=...)` 而不是绕过它。
- **每会话一把 `asyncio.Lock`**（`SessionStore`）：`run_turn` 全程持锁，所以同会话的快照写入天然串行，
  不会出现两个请求同时写同一快照。
- **同步磁盘 I/O 不能堵事件循环**（S5-4 的教训）：快照 dump+写、流水追加都要丢
  `asyncio.to_thread`，否则长会话的大快照会阻塞所有并发请求。
- **错误信封**：`core/errors.py` 已有统一 `{"error":{"code","message"}}`；router 把具体异常映射成
  `HTTPException`。快照写失败要新增一个异常 → `HTTPException(500, ...)`。

## 快照格式（我的决定）

快照文件是一个「信封 + 状态」两层结构，信封承载文件名里放不下的东西：

```jsonc
{
  "schema_version": 1,
  "buyer_id": "u1",
  "shopping_session_id": "s1",
  "updated_at": "2026-11-02T12:00:00Z",
  "state": { /* AgentState.model_dump(mode="json") 的完整内容 */ }
}
```

- `state` 用 `AgentState.model_dump(mode="json")` 序列化（datetime / 枚举 → JSON 安全值），
  恢复用 `AgentState.model_validate(envelope["state"])`。
- `schema_version` 为 S7 的格式演进留钩子；`buyer_id`/`shopping_session_id` 冗余存一份，
  方便人肉对照「这个文件是哪个会话」；`updated_at` 供排查。
- **风险（已知，可接受）**：若某条 `Msg` 的 content block 里塞了非 JSON 数据（`DataBlock` 里装
  二进制/张量），`model_dump(mode="json")` 会抛——此时快照写失败 → 5xx，暴露问题而不是静默丢数据。
  当前链路（text + tool_result + structured_output）都是 JSON 安全类型；实现里用真实 `AgentState`
  的 round-trip 测试锁住这一条。

## 存储布局（我的决定）

```
data/sessions/
├── snapshots/
│   └── {key_hash}.json          # 每会话一个快照，真相源
└── journal.jsonl                 # 全局追加流水，一问一答两行
```

- `key_hash = sha256(f"{buyer_id}\x00{shopping_session_id}".encode()).hexdigest()`：
  会话 id 是任意字符串，不能直接当文件名（可能含 `/`、`..`），哈希保证文件系统安全。
- **快照按会话分文件**：真相源，恢复时只读自己的。
- **流水全局一份**：评测要按时间遍历所有会话的所有轮次，单一文件最省事；复盘某会话时
  `grep '"shopping_session_id":"s1"' journal.jsonl` 即可。
- **`data/sessions/` 加入 `.gitignore`**（运行时状态，可能含 PII，不入库）。`data/catalog-v1.jsonl`
  是静态目录数据照旧入库。

## 流水格式（我的决定）

每轮成功回答追加**恰好两行**，都是单行 JSON：

```jsonc
{"role":"user","buyer_id":"u1","shopping_session_id":"s1","ts":"2026-11-02T12:00:00Z","turn":3,"text":"预算300给猫买饮水器"}
{"role":"assistant","buyer_id":"u1","shopping_session_id":"s1","ts":"2026-11-02T12:00:11Z","turn":3,"text":"命中2款","cards":[…]}
```

- `turn` = 该会话已答轮次的序号，由恢复后的状态推导（`state.context` 里 `role=="user"` 的消息数），
  不额外持久化一个计数器，避免「内存计数器和盘上对不上」。
- `assistant` 行的 `text` = `final_text`（summary），`cards` = 完整商品列表（评测可能要查具体 sku）。
- 两行用**一次 `write()` 调用**追加，避免与其它会话的并发追加把一问一答拆开。

## 组件设计

### 1. `SessionRepository`（`features/chat/repository.py`，新增）

纯粹的「磁盘读写」，不含 Agent 逻辑，单测可完全离线。

```python
class SessionRepository:
    def __init__(self, root: Path) -> None: ...
    def load(self, key: SessionKey) -> AgentState | None: ...   # 读快照信封，校验 schema_version，返回 state
    def save(self, key: SessionKey, state: AgentState) -> None: ...  # 原子：写 .json.tmp + os.replace
    def append_journal(self, key, question: str, answer: CommerceIntentResponse) -> None: ...  # 追加两行
```

- 文件缺失 / 损坏 / `schema_version` 不匹配 → `load` 返回 `None`（视作新会话，别让坏文件挡住服务；
  损坏原因 `logger.warning` 留下痕迹）。这是一个取舍：宁可从零开始，也不崩溃或返回半坏状态。
- `save` 失败（磁盘满 / 权限）→ 抛 `SnapshotWriteError`（新增异常）。

### 2. `SessionStore` 变成「内存缓存 + 磁盘真相源」（改 `service.py`）

- 构造加 `repository: SessionRepository | None = None`（`None` 保持 S5 纯内存行为，离线测试不用碰盘）。
- `get_or_create`：内存命中 → 返回；未命中 → `repository.load(key)` 有则 `build_agent(runtime,
  state=...)` 恢复、无则新建，然后缓存。
- 新增 `persist(key, agent, question, answer)`：先 `repository.save`（失败 → `SnapshotWriteError`），
  再 `repository.append_journal`（失败 → 只 `logger.warning`，不抛）。
- 既有 TTL/LRU 语义不变：过期/被逐出的会话，下次访问会从磁盘恢复，所以 LRU 逐出不再等于「丢历史」。
- **修 LRU 逐出竞态（我的决定）**：`_evict_lru` 现在只按 `last_active` 挑最旧，而 `run_turn` 直到
  `finally` 才 `touch`——所以一个正在 `reply` 中、持锁的会话，其 `last_active` 是陈旧的，会在高并发
  下被另一个 key 的 `get_or_create` 触发逐出；随后同 key 的第二个请求建新 `Session`+新锁、读旧快照，
  两个 Agent 对同一 key 并发 `reply`+`persist`，破坏同会话串行、且 last-writer-wins 丢一轮。修法：
  `_evict_lru` 跳过 `session.lock.locked()`（在飞的会话）——一句话 + 一个并发测试。这条修正是 S6 的
  正确性承诺「每会话一把锁 → 不会同时写同一快照」成立的前提，不是可选优化。

### 3. `run_turn` 加持久化钩子 + 失败即逐出（改 `service.py`）

在**成功路径**（结构化解析出 `cards` 之后、`return` 之前）插入持久化，仍持锁：

```python
cards = ProductCardList.model_validate(structured)
response = CommerceIntentResponse(final_text=cards.summary, cards=cards.cards)
await store.persist(key, session.agent, raw_query, response)   # 快照 + 流水，to_thread 卸载
return response
```

- **只在成功时持久化**（我的决定）：超时/工具失败/模型失败这些路径抛异常前，不写快照也不写流水——
  失败的那轮等于「在持久化层回滚」，重启后从最后一轮成功处继续。
- **失败即逐出（我的决定，关键补充）**：`run_turn` 的异常路径除了重新抛出，还要 `store.evict(key)`，
  把该会话从内存缓存移除。这样两个隐患一起消掉：
  1. 失败那轮里 AgentScope 可能已经在 `agent.state.context` 里留下半截消息（半个 user msg / 半个工具
     调用）；若不逐出，**下一轮成功时会把这段残渣一起写进快照**，把「失败轮回滚」破坏掉。
  2. 快照写失败时若不逐出，客户端重试会命中同一个 `Session`，同一个 user msg 被追加第二次，把
     用户被告知「已丢失」的那轮又写回去。
  逐出后，下一次访问走 `get_or_create` → 从盘上**最后一轮成功**的快照重建，内存与磁盘语义一致。
- `persist` 内部用 `asyncio.to_thread` 卸载 `model_dump` + 磁盘写。

### 4. `build_agent` 支持恢复（改 `agents/factory.py`）

```python
def build_agent(runtime: AgentRuntime, state: AgentState | None = None) -> Agent:
    ...
    agent = Agent(..., state=state, ...)
    allow_product_search_tool(agent)
    return agent
```

- `Agent(name=..., system_prompt=..., model=..., toolkit=..., state=state, ...)`——`state=None` 时
  AgentScope 内部 `state or AgentState()`，行为与 S5 完全一致。
- 恢复后仍走 `allow_product_search_tool`，保证 permission 放行在恢复后的 engine 上同样生效。

### 5. 配置（改 `core/config.py`）

```python
sessions_dir: Path = Field(
    default=Path(__file__).resolve().parents[4] / "data" / "sessions",
)
```

- 默认**锚定到仓库根**的 `data/sessions/`，与 catalog/index 同一约定（`factory.py` 的
  `DEFAULT_DATA_PATH = Path(__file__).resolve().parents[4] / "data" / "catalog-v1.jsonl"`）。
  不能写 cwd 相对的 `Path("data/sessions")`：`make dev-api` 用 `uv run --directory backend`，
  进程 cwd 是 `backend/`，那样会落到 `backend/data/sessions`——既违背「与 catalog 同级」，
  也让仓库根 `.gitignore` 的 `data/sessions/` 漏掉含 PII 的会话文件。
- 仍可 `HARBOR_SESSIONS_DIR` 覆盖（测试用临时目录做隔离，不碰真实 `data/sessions/`）。

### 6. 装配（改 `main.py` lifespan）

```python
repo = SessionRepository(settings.sessions_dir)
runtime = build_runtime(settings)
app.state.chat_store = SessionStore(
    agent_factory=lambda state=None: build_agent(runtime, state=state),
    repository=repo,
)
```

## 失败语义汇总

| 场景 | 行为 |
|---|---|
| 快照读缺失/损坏 | `load` → `None`，当新会话（warning 记录） |
| 快照写失败 | `SnapshotWriteError` → HTTP 500（新异常 + router 映射），**并逐出该会话** |
| 流水写失败 | `logger.warning`，请求照常 200 |
| 超时 / 工具失败 / 模型失败 / 无结构化输出 | 与 S5 相同（504/502/502/502），**不持久化本轮，并逐出该会话** |

「逐出该会话」= 内存缓存移除该 `Session`；下次访问从盘上最后一轮成功快照重建，保证内存与磁盘
在失败后一致（见 §3「失败即逐出」）。

## 留给实现者的（TDD 里的东西）

- 具体异常类 `SnapshotWriteError` 与 router 映射（500 + `http_500` / 或复用 `internal_error`）。
- `SessionKey` → `key_hash` 的精确写法、信封字段名、`updated_at` 时区格式。
- `SessionRepository` 的路径拼接、`os.replace` 原子写的临时文件名（同目录 `.tmp` 后缀）。
- `SessionStore.evict(key)` 方法（从 `_sessions` 删 key，配合 §3 失败即逐出）；`run_turn` 异常路径调它。
- 恢复时 `turn` 序号如何从 `state.context` 数 `role=="user"` 得到（含首轮的边界）。
- 测试覆盖：真实 `AgentState` 的 dump→load round-trip（锁住 `mode="json"` 可序列化）；
  快照失败 → 500 + 会话被逐出；流水失败 → 200 + warning；重启（新建 store + 同 repo）后同会话能恢复
  上下文；失败那轮不落盘（下一轮成功时快照里不含失败轮的半截消息）；`_evict_lru` 跳过在飞会话的并发测试；
  `.gitignore` 加 `data/sessions/`。

## 显式不在 S6

- 上下文增长控制 / 截断 / 摘要 / 早期消息卸载 → **S7**。
- 快照压缩、后台异步写、多实例共享存储、`schema_version` 迁移逻辑 → 以后再说（字段先占位）。
