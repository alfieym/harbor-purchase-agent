# S8 设计问答：三作用域偏好记忆（本轮 / 本会话 / 长期）

- 日期：2026-11-03
- 范围：`features/memory/`（新）+ `agents/preferences.py`（新）+ `agents/context_governance.py`
  + `agents/factory.py` + `agents/prompts.py` + `core/config.py`
- 前置：S6（快照恢复）+ S7（会话偏好注入、`_normalize_preferences` 清洗）

## 意图（为什么做）

S7-7 已经会注入「会话约束」，但它是**隐式**的：按线索词从用户原话里猜，只活在当前
会话，模型不能主动记、也不能改或删。用户明确要求的是：

> 三个作用域：本轮、本会话、长期；工具：新增偏好、更新偏好、删除偏好。

S8 把偏好变成**显式、可寻址、可跨会话**的记录：

1. **本轮**：模型可以「这次先按你说的来」，轮末即弃，不落任何盘。
2. **本会话**：随 S6 快照持久化，换 `shopping_session_id` 就是新会话。
3. **长期**：按 `buyer_id` 落 SQLite，跨会话、跨进程、重启后仍在。
4. 每条记录有稳定 id，`update_preference` / `delete_preference` 按 id 操作；
   工具结果回传 id 和当前清单，模型下一轮才知道该改哪条。
5. 冲突**不做确定性仲裁**：三段都注入并标注作用域，由模型自己权衡。

## 关键代码事实（已核实）

- `FunctionTool(..., is_state_injected=True)` 的工具函数会收到
  `_agent_state: AgentState`（`agentscope/tool/_toolkit.py:303-309`）。偏好工具
  因此能直接读写会话作用域，不需要闭包抓 agent 实例。
- `AgentState.middle_context: dict[str, Any]`（`agentscope/state/_state.py:294`）
  参与 `state.model_dump()`，S6 的 `SessionRepository.save` 会原样持久化。
- `ContextGovernanceMiddleware.on_reply` 每个 `agent.reply` 只进一次，且在
  reasoning 之前（S7 的 `turn_count` 就在这里 +1）——这是「本轮」清空的唯一正确位置。
- `ContextGovernanceMiddleware.on_model_call` 在**计数与闸门之前**注入偏好
  （`context_governance.py:622-627`）。S8 的注入必须留在这里，否则注入的 token
  不计入 `gate_ratio`，闸门会失效。
- `SessionStore.get_or_create` 用 `agent_factory(state)` 建 agent（`chat/service.py:110`）。
  约 25 处测试用 `lambda state=None: …` 假工厂——**不改这个签名**。
- `SqliteMemoryStore` 的写路径在 `asyncio.to_thread` 里跑；SQLite 开 WAL +
  `busy_timeout`，跨进程也能安全排队。

## 三个作用域的生命周期

| 作用域 | 存储位置 | 活多久 | 换 `shopping_session_id` | 进程重启 | 快照 |
|---|---|---|---|---|---|
| `turn` | `PreferenceMemory._turn`（进程内存） | 一次 `agent.reply`（含多次 ReAct 迭代） | 丢弃 | 丢弃 | **不落** |
| `session` | `state.middle_context["harbor.preferences"]` | 该会话 | 新会话没有 | 恢复 | **落** |
| `long_term` | SQLite `memories` 表，按 `buyer_id` | 永久 | 仍在 | 仍在 | 独立于快照 |

## 我的裁决（实现按此执行）

| 决策点 | 结论 | 理由 / 代价 |
|---|---|---|
| 长期存储 | SQLite，`data/sessions/memory.sqlite3`（`.gitignore` 已覆盖 `data/sessions/`） | 小记录多写；WAL + `BEGIN IMMEDIATE` |
| 事实与向量 | 同一行（`text` + `vector` BLOB）、同一条 INSERT/UPDATE、同一事务 | 不可能出现「有事实无向量」或「向量滞后于文本」 |
| 向量编码 | float32 little-endian `struct.pack`，`vector_model` 同列记录 | 换模型时能识别旧向量 |
| 向量来源 | 写时同步调 `OpenAIEmbedder`（复用 S3 的实例） | 长期每次 add/update 花 1 次 embedding；embedding 失败则不写、返回结构化失败 |
| 会话作用域落点 | `state.middle_context`，存纯 dict（不是 dataclass） | 随快照序列化；不塞 `context`，S7-6 前缀稳定不受影响 |
| 本轮作用域落点 | 中间件内存，`on_reply` 开头清空 | 不进 state、不进快照 |
| 身份传递 | `SessionStore` 在建 agent 前把 `buyer_id` 写进 `state.middle_context["harbor.identity"]`；`build_agent` 从这里读 | **不改** `agent_factory(state)` 签名，25 处测试零改动 |
| 无身份时 | `build_agent(..., buyer_id=None)` 且 state 无身份 → 不注册长期工具，本轮/本会话照常 | CLI 显式传 `buyer_id="cli"` |
| 工具集 | `add_preference` / `update_preference` / `delete_preference` | 名字与用户要求逐字对应 |
| 作用域能否改 | **不能**。`update_preference` 只改 text；换作用域 = delete + add | 避免「长期→本轮」的删除语义和向量重建分支 |
| 冲突仲裁 | **不仲裁**。三段全注入、各自标注作用域，模型自己权衡 | 用户明确要求；代价是矛盾时依赖模型判断 |
| 会话内启发式抽取 | 保留（S7-7 不回退），与显式 session 记录按 `_preference_key` 合并去重 | S7 真额度行为不变 |
| 去重 | 同作用域内同语义键 → 返回已有记录（`duplicate: true`），不新增 | 省一次 embedding；不跨作用域去重 |
| 写入时机 | 工具调用**即写穿**（长期立刻落盘） | 用户说「记住」后，本轮后续失败也不该丢 |
| 失败语义 | 长期写入失败返回 `{"ok": false, ...}` 的 **SUCCESS** ToolChunk，不 fail 整个回合 | 让模型告诉用户 / 重试；事务保证无半行 |
| 策略开关 | S8 工具只在 `governed` 下注册 | 保持 S7-5「legacy = pre-S7 基线」 |
| 注入上限 | 每个作用域最多 24 条（复用 `MAX_PREFERENCES`），长期取最近 24 条并在超限时提示还有 N 条 | 注入体积有界 |

## 组件

### 1. `features/memory/models.py`

```python
PreferenceScope = Literal["turn", "session", "long_term"]

@dataclass(frozen=True)
class Preference:
    id: str                 # "pref_" + 8 hex
    scope: PreferenceScope
    text: str               # 用户原话，逐字保留（仅做空白归一）
    created_at: str         # ISO-8601 UTC
    updated_at: str
```

### 2. `features/memory/sqlite_store.py`

```sql
CREATE TABLE IF NOT EXISTS memories (
    id           TEXT PRIMARY KEY,
    buyer_id     TEXT NOT NULL,
    kind         TEXT NOT NULL,   -- S8 只写 'preference'；留给未来的 'fact'
    scope        TEXT NOT NULL,   -- 'long_term'
    text         TEXT NOT NULL,
    vector       BLOB NOT NULL,   -- float32 小端；长度 = dim * 4
    vector_model TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_buyer_kind ON memories(buyer_id, kind);
```

`SqliteMemoryStore`（全部方法 async，内部 `asyncio.to_thread`）：

- `add(*, buyer_id, text, vector, vector_model, kind="preference") -> Preference`
- `update_text(*, buyer_id, memory_id, text, vector, vector_model) -> Preference | None`
- `delete(*, buyer_id, memory_id) -> Preference | None`
- `list(*, buyer_id, kind="preference", limit=MAX_PREFERENCES) -> list[Preference]`
- `search_similar(*, buyer_id, vector, limit=3) -> list[tuple[Preference, float]]`
- `close()`

写连接：`PRAGMA journal_mode=WAL`、`PRAGMA busy_timeout=5000`、`BEGIN IMMEDIATE`；
进程内再加一把 `asyncio.Lock` 串行写。每次操作开短连接（小记录场景足够）。

### 3. `features/memory/vector.py`

- `pack(vector) -> bytes` / `unpack(blob) -> list[float]`
- `cosine(a, b) -> float`（长度不一致返回 `-1.0`，调用方过滤）

### 4. `agents/preferences.py`

`PreferenceMemory`（每个 agent 一个）：

```python
class PreferenceMemory:
    def __init__(self, *, buyer_id: str | None,
                 store: SqliteMemoryStore | None,
                 embedder: Embedder | None) -> None: ...

    async def begin_turn(self, state: AgentState) -> None      # 清 turn + 重载长期
    def turn_items(self) -> list[Preference]
    def session_items(self, state: AgentState) -> list[Preference]
    def long_term_items(self) -> list[Preference]

    async def add(self, state, scope, text) -> Preference
    async def update(self, state, preference_id, text) -> Preference | None
    async def delete(self, state, preference_id) -> Preference | None
```

- 清洗复用 `context_governance._normalize_preferences` 的规则：空 / >200 字 /
  疑问句（`_QUESTION_CUES`）→ 拒绝；同作用域按 `_preference_key` 去重。
- `turn` 只写内存；`session` 写 `state.middle_context`；`long_term` 先算向量再落库。
- 长期 `add` 在写入前用「同语义键」比对已加载的长期列表，命中则直接返回旧记录，
  **不调 embedding**。
- 长期 `add` 成功后，用刚算出的向量跑一次 `search_similar`，把相似记录附在工具结果里
  （`similar`），让模型自己决定要不要 `update_preference`——不额外花 embedding。

三个工具（`FunctionTool(is_state_injected=True)`）：

```python
async def add_preference(_agent_state, text: str,
                         scope: Literal["turn", "session", "long_term"]) -> ToolChunk
async def update_preference(_agent_state, preference_id: str, text: str) -> ToolChunk
async def delete_preference(_agent_state, preference_id: str) -> ToolChunk
```

返回体（模型可读）：

```json
{"ok": true,
 "preference": {"id": "pref_1a2b3c4d", "scope": "long_term", "text": "预算 500 以内"},
 "similar": [{"id": "...", "text": "...", "score": 0.93}],
 "counts": {"turn": 0, "session": 1, "long_term": 3}}
```

### 5. 注入格式（`ContextGovernanceMiddleware._with_preferences`）

```text
<system-info>用户偏好（[本轮] 只影响这一次对话，[本会话] 随会话保存，[长期] 跨会话保存。
作用域越窄越贴合当下；互相冲突时你自己权衡，必要时向用户确认）：
[本轮] 这次先看看 2000 以内的
[本会话] pref_9f8e7d6c 只考虑母婴宠物类目
[长期] pref_1a2b3c4d 预算 500 以内
</system-info>
```

- 空的作用域整段省略；一段都没有就不注入（与 S7-7 一致）。
- 每次 model call 重建，不写回 `state.context`、不进 system prompt（S7-6 / S7-7 承诺不变）。
- 未接 `PreferenceMemory` 时回退到 S7 原有行为（启发式 + 附录）。

### 6. 装配

- `AgentRuntime` 增加 `memory_store: SqliteMemoryStore | None`；`build_runtime` 用
  `settings.memory_db_path` 建。embedding 复用 `runtime` 里同一个 `OpenAIEmbedder`。
- `build_agent(runtime, state=None, *, buyer_id=None)`：
  - `buyer_id` 优先取 `state.middle_context["harbor.identity"]["buyer_id"]`；
  - `governed` 且 `buyer_id` 存在 → `PreferenceMemory` + 三个工具 + 注入；
  - `legacy` → 完全不加（S7-5 基线不变）。
- `SessionStore.get_or_create`：加载/新建 state 后写入身份，再交给工厂。
- `core/config.py` 增加 `memory_db_path: Path`（默认 `data/sessions/memory.sqlite3`）。
- `prompts.py` 增加 `PREFERENCE_GUIDANCE`（仅在 governed 下拼接）：何时用哪个作用域、
  怎么用 id 改删。

## 失败语义

| 情况 | 结果 |
|---|---|
| 长期 add/update 时 embedding 失败 | `{"ok": false, "error": "记忆向量生成失败", "retryable": true}`；**不写库**、回落 S7 行为 |
| SQLite 写入失败 | `{"ok": false, "error": "偏好写入失败", "retryable": true}`；事务回滚，无半行 |
| update/delete 的 id 不存在 | `{"ok": false, "error": "偏好不存在"}` |
| text 为空 / 超长 / 疑问句 | `{"ok": false, "error": "不是一条可保存的偏好"}` |
| 同作用域重复 | `{"ok": true, "duplicate": true, "preference": <已有>}`，不新增 |
| 长期库里有旧模型的向量 | `list` 正常返回；`search_similar` 跳过 `vector_model` 不一致的行 |

以上都不会把整个回合判失败——`run_turn` 只把 `ToolResultState.ERROR` 当工具失败，
S8 的失败一律走 SUCCESS + `ok:false`。

## 非目标

- 不做跨 buyer 的共享或推荐。
- 不做前端偏好管理页 / REST 端点（S8 只到 agent + 存储）。
- 不做长期记忆的语义检索注入：S8 注入最近 24 条；`search_similar` 只服务于
  add 时的「相似偏好提醒」。
- 不做自动作用域推断之外的隐式写入：显式工具是唯一写入口；S7 启发式仍只影响注入，
  不写成 session 记录。
- 不改 S6 快照 schema 版本（`middle_context` 是自由字段，向后兼容）。

## 验收清单（S8）

| ID | 验收项 | 判定方式 |
|---|---|---|
| S8-1 | 三个作用域互不串味：turn 轮末消失、session 随快照恢复、long_term 重启仍在 | 单测 + 真额度 |
| S8-2 | add / update / delete 全链路可用，id 稳定 | 单测 + 真额度 |
| S8-3 | 注入是一次 model call 末尾的 `UserMsg`，三段带作用域标签，不写回 `context` | 断言 `_prepare_model_input()` |
| S8-4 | 长期记忆一行同时含 text 与 vector，同一事务写入；嵌入失败无半行 | SQLite 直查 + 故障注入 |
| S8-5 | 同一 buyer 两个会话并发写长期记忆：无 `database is locked`、无丢失 | 并发单测 |
| S8-6 | `legacy` 策略下没有 S8 工具、没有注入，S7-5 基线不变 | 工厂单测 |
| S8-7 | 会话启发式抽取与显式记录合并去重，S7-7 行为不回退 | 单测 + 真额度 |
| S8-8 | 真额度：模型自己调用 add/update/delete，跨轮记住 | `scripts/accept_s8_real.py` |

## 风险与待办

- **embedding 额度**：长期每次 add/update 1 次调用；重复检测先于 embedding，能省掉
  一部分。测试全部用假 embedder。
- **SQLite 并发**：WAL 只允许一个写者；`BEGIN IMMEDIATE` + `busy_timeout` + 进程内锁
  是三层保险。跨进程（API + CLI 同时跑）也安全，但 CLI 写入后 API 的本轮缓存要等
  下一次 `begin_turn` 才看到。
- **注入体积**：24 × 3 段仍然可能偏大；S7 的闸门会兜底，但若真额度出现频繁强制压缩，
  下一步应改成按查询向量检索（`search_similar` 已就位）。
- **身份传递走 `middle_context`**：`build_agent` 不传 state 的调用（部分单测）没有
  长期能力，这是有意的默认。

## 真额度记录（2026-11-04）

`backend/scripts/accept_s8_real.py`，真实 `qwen-plus` + 确定性假 embedder（零 embedding）：

```
✓ S8-1 turn 作用域写入 — turn=['这次先看看 2000 以内的']
✓ S8-2 本轮偏好被注入 — [本轮] pref_db879c4f 这次先看看 2000 以内的
✓ S8-3 begin_turn 清空 turn — turn=[]
✓ S8-4 session 作用域跨轮保留 — session=['只考虑母婴宠物类目']
✓ S8-5 long_term 落 SQLite — rows=['以后都只买 500 以内的']
✓ S8-6 长期记录与文本同事务 — vector_model=['qwen3.7-text-embedding-flash']
✓ S8-7 新会话仍注入长期偏好 — [长期] pref_bb033c60 以后都只买 500 以内的
✓ S8-8 update 改文本保 id — id 前后一致
✓ S8-8 delete 移除记录 — remaining=[]
✓ embedding 额度未消耗 — 本地 embed 2 次
```

完整报告：[`_build_log/2026-11-04-s8-acceptance-report.md`](_build_log/2026-11-04-s8-acceptance-report.md)。

## v2 补充：验收清单驱动的设计（2026-11-04）

用户给出的验收清单（S8-1..S8-8）比 v1 的设计更严格，补齐了六处机制。下面只记
**变化**，v1 中未冲突的部分继续有效。

### 1. 两段式写入 + 待确认队列（S8-1 / S8-6）

`session` 与 `long_term` 不再「工具一调就落库」：

- `add_preference` 调 `PreferenceMemory.stage()`，把操作写进
  `state.middle_context["harbor.pending_preferences"]`（纯 dict 列表），**不落库**。
- 待确认操作随 S6 快照保存；`begin_turn` **不清空**它。
- 每轮注入末尾追加：

  ```text
  <system-info>还有待确认的记忆操作（尚未写入）：
  [待确认] op_1a2b3c4d scope=long_term 我以后只看官方店
  先向用户确认；同意后调用 confirm_preference，拒绝则 cancel_preference。
  </system-info>
  ```

- 用户同意 → `confirm_preference(operation_id)` 调 `add()` 真正写入；拒绝 →
  `cancel_preference`。写入失败时操作**留在队列里**，可以稍后重试。
- `turn` 作用域仍然立即生效（只活一轮，不存在「以后」的确认语义）。
- 无身份（`long_term_enabled=False`）时 `add_preference` 在暂存**之前**就返回
  「长期记忆未启用」，避免问用户一个永远写不进去的确认。

### 2. 结构化 dislike（S8-2 / S8-8）

- `kind: Literal["preference", "dislike"]`，dislike 必须带 `material_tags`。
- SQLite 增列 `material_tags TEXT NOT NULL DEFAULT '[]'`（JSON 数组）。
- `PreferenceMemory.blacklisted_material_tags(state)` 汇总三个作用域里所有 dislike
  的材质；`make_product_search_tool(service, memory=memory)` 在**代码里**把它们并进
  `ProductSearchSpec.excluded_material_tags`（与模型自己传的取并集）。
- 撤回 = 删除那条 dislike；下一次检索的黑名单立刻不再包含它。

### 3. 版本号与 409（S8-4）

- `memories.version INTEGER NOT NULL DEFAULT 1`，每次 `update_text` 在同一事务里
  `version = version + 1`。
- `update_preference(..., expected_version=N)`；提交前在**写锁内**比对，不一致抛
  `MemoryVersionConflict`（携带当前行），事务回滚、不覆盖新值。
- 工具返回 `{"ok": false, "code": "version_conflict", "status": 409, "current": {...}}`。
- 注入的长期行带版本：`[长期] pref_x 预算 500 以内（v1）`，否则模型没有可回传的
  `expected_version`。

### 4. 200 条上限（S8-5）

- `MAX_LONG_TERM_PREFERENCES = 200`，只统计 `kind='preference'`。
- 上限检查放在 `add_or_get` 的 `BEGIN IMMEDIATE` 事务里（`max_count` 参数），
  并发批量写也不会超过。
- 第 201 条：`{"ok": false, "code": "limit_reached", "limit": 200}`；dislike 传
  `max_count=None`，不受限。
- 判定发生在 `confirm_preference`（因为 add 只暂存）。

### 5. 去重的语义键（S8-3）

真额度运行暴露：模型会把「我以后只看官方店」重述成「以后只看官方店」。  
`_preference_key` 的噪声前缀现在还会剥掉开头的「我 / 我们 / 咱们」（以及「第N轮：」），
所以两种说法落到同一个 `text_key`，唯一索引 + `add_or_get` 只留一行。

### 6. 新的工具面

| 工具 | 语义 |
|---|---|
| `add_preference(text, scope, kind, material_tags)` | turn 立即写；session/long_term 只暂存 |
| `confirm_preference(operation_id)` | 用户同意后写入 |
| `cancel_preference(operation_id)` | 用户拒绝后丢弃 |
| `update_preference(preference_id, text, expected_version)` | 版本不符 → 409 |
| `delete_preference(preference_id)` | 删除（也是撤回 dislike 的方式） |

### 7. v2 验收对照

| ID | 验收项 | 实现落点 | 测试 |
|---|---|---|---|
| S8-1 | 确认后写入；新会话仍生效 | stage/confirm + `begin_turn` 重载 | `test_s8_acceptance.py` + 真额度 |
| S8-2 | 撤回 dislike 后不再过滤 | `delete` + `blacklisted_material_tags` | 单测 + 真额度 |
| S8-3 | 同一句两次只一条 | `text_key` 唯一索引 + 代词归一 | 单测 + 真额度 |
| S8-4 | 旧 version → 409 | `version` 列 + `expected_version` | 单测 + 真额度 |
| S8-5 | 第 201 条被拒；dislike 豁免 | 事务内 `max_count` | 单测 + 真额度 |
| S8-6 | 未确认不落库；下一轮提示 | pending 队列 + 注入块 | 单测 + 真额度 |
| S8-7 | 「这次」不进长期 | turn 作用域只写 `_turn` | 单测 + 真额度 |
| S8-8 | 材质黑名单代码级过滤 | `product_search` 合并 spec | 单测 + 真额度 |
