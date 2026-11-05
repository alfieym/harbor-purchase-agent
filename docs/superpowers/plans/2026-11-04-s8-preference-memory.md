# S8 三作用域偏好记忆 — 实现计划

> **For agentic workers:** 按任务顺序 TDD 执行：先写失败测试，看它红，再写最小实现，看它绿。

**Goal:** 给采购 Agent 增加本轮 / 本会话 / 长期三层偏好记忆，并提供增、改、删三个工具。

**Architecture:** 长期记忆落 SQLite（一条记录 = text + vector 同一行同一事务）；
会话作用域存 `AgentState.middle_context`（随 S6 快照持久化）；本轮作用域存在
`PreferenceMemory` 内存里，每次 `agent.reply` 开头清空。注入仍由 S7 的
`ContextGovernanceMiddleware` 在每次 model call 末尾追加一条 `UserMsg`。

**Tech Stack:** Python 3.11、AgentScope 2.0.11、sqlite3（标准库）、pytest、ruff、mypy。

**Spec:** `docs/s8-preference-memory-design-notes.md`

## Global Constraints

- 长期记录必须 text 与 vector **同一行、同一条语句、同一事务**写入。
- 注入不写回 `state.context`、不进 system prompt（S7-6 / S7-7 承诺）。
- `legacy` 策略下不注册任何 S8 工具、不做 S8 注入（S7-5 基线）。
- 作用域不可通过 update 修改；换作用域 = delete + add。
- 失败不判整回合失败：返回 `{"ok": false, ...}` 的 SUCCESS ToolChunk。
- 不改 `SessionStore` 的 `agent_factory(state)` 签名。
- 测试零网络、零真实 embedding（用假 embedder）。

## Review Focus

- **同一 buyer 两个会话并发写长期记忆**：期望两边都成功，不出现 `database is locked`、不丢记录。
- **embedding 失败后重试**：期望库里没有半条记录（无 text 无 vector 的行）。
- **快照恢复后的会话偏好**：期望显式 session 记录原样回来，且与启发式抽取去重。
- **没有 buyer_id 的调用路径**（CLI / 部分单测）：期望本轮与本会话照常，长期工具缺席而不是报错。
- **长期记录换 embedding 模型后**：期望 `list` 仍可读，`search_similar` 跳过旧模型行。

---

### Task 1: 向量编码与相似度

**Files:**
- Create: `backend/src/harbor/features/memory/__init__.py`
- Create: `backend/src/harbor/features/memory/vector.py`
- Test: `backend/tests/test_memory_vector.py`

**Interfaces:**
- Produces: `pack(values: Sequence[float]) -> bytes`、`unpack(blob: bytes) -> list[float]`、
  `cosine(a: Sequence[float], b: Sequence[float]) -> float`

- [x] **Step 1: 写失败测试**

```python
def test_pack_round_trips_float32() -> None:
    values = [0.5, -1.25, 3.0]
    assert unpack(pack(values)) == [0.5, -1.25, 3.0]

def test_cosine_of_identical_vectors_is_one() -> None:
    assert cosine([1.0, 2.0], [1.0, 2.0]) == pytest.approx(1.0)

def test_cosine_of_orthogonal_vectors_is_zero() -> None:
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

def test_cosine_of_mismatched_lengths_is_negative_one() -> None:
    assert cosine([1.0, 2.0], [1.0]) == -1.0

def test_cosine_of_zero_vector_is_zero() -> None:
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
```

- [x] **Step 2: 运行看红** — `pytest tests/test_memory_vector.py -q`，期望 ImportError。
- [x] **Step 3: 实现 `vector.py`**（`struct.pack(f"<{len(values)}f", *values)`；余弦用 `math.fsum`）。
- [x] **Step 4: 运行看绿**。
- [ ] **Step 5: 提交** — 等用户要求再提交。

---

### Task 2: SQLite 长期存储

**Files:**
- Create: `backend/src/harbor/features/memory/sqlite_store.py`
- Test: `backend/tests/test_memory_store.py`

**Interfaces:**
- Produces: `MemoryRecord`（id/buyer_id/kind/scope/text/vector/vector_model/created_at/updated_at）
  与 `SqliteMemoryStore`：
  `add(*, buyer_id, text, vector, vector_model, kind="preference") -> MemoryRecord`、
  `update_text(*, buyer_id, memory_id, text, vector, vector_model) -> MemoryRecord | None`、
  `delete(*, buyer_id, memory_id) -> MemoryRecord | None`、
  `list(*, buyer_id, kind="preference", limit=24) -> list[MemoryRecord]`、
  `search_similar(*, buyer_id, vector, limit=3) -> list[tuple[MemoryRecord, float]]`、
  `close() -> None`

测试覆盖：

```python
def test_add_persists_text_and_vector_in_one_row(tmp_path)
def test_add_is_visible_after_reopen(tmp_path)
def test_update_text_rewrites_text_and_vector(tmp_path)
def test_delete_removes_only_that_buyer_record(tmp_path)
def test_list_is_scoped_to_one_buyer_newest_first(tmp_path)
def test_search_similar_orders_by_cosine_and_skips_other_vector_models(tmp_path)
def test_concurrent_writers_do_not_lose_records(tmp_path)   # asyncio.gather 20 次写
def test_failed_write_leaves_no_partial_row(tmp_path)        # monkeypatch execute 抛错
```

- [x] **Step 1: 写失败测试**（含上述 8 条）。
- [x] **Step 2: 运行看红**。
- [x] **Step 3: 实现 store**：WAL + `busy_timeout=5000` + `BEGIN IMMEDIATE`；
  每次操作短连接；进程内 `asyncio.Lock` 串行写；`with conn:` 保证事务。
- [x] **Step 4: 运行看绿**。

---

### Task 3: PreferenceMemory 的本轮 / 本会话

**Files:**
- Create: `backend/src/harbor/agents/preferences.py`
- Test: `backend/tests/test_preferences.py`

**Interfaces:**
- Produces: `Preference`（id/scope/text/created_at/updated_at）、`PreferenceMemory`
  的 `begin_turn` / `turn_items` / `session_items` / `add` / `update` / `delete`。

测试覆盖：

```python
def test_turn_scope_dies_at_next_begin_turn()
def test_session_scope_survives_begin_turn_and_snapshot()
def test_add_rejects_empty_long_and_question_text()
def test_add_dedupes_same_semantic_key_within_one_scope()
def test_update_changes_text_and_keeps_id_and_scope()
def test_delete_removes_by_id_across_scopes()
def test_update_and_delete_unknown_id_return_none()
```

- [x] **Step 1: 写失败测试**。
- [x] **Step 2: 运行看红**。
- [x] **Step 3: 实现**：清洗复用 `context_governance._normalize_preferences` 的
  `_QUESTION_CUES` / `_preference_key` / `MAX_PREFERENCES`；session 存
  `state.middle_context["harbor.preferences"]["items"]` 的纯 dict 列表。
- [x] **Step 4: 运行看绿**。

---

### Task 4: 长期作用域（向量 + 相似提醒）

**Files:**
- Modify: `backend/src/harbor/agents/preferences.py`
- Modify: `backend/tests/test_preferences.py`

**Interfaces:**
- Consumes: `SqliteMemoryStore`、`Embedder.embed(texts) -> list[list[float]]`（复用 `search/embedder.py`）。
- Produces: `add(..., scope="long_term")` 返回 `(Preference, similar: list[tuple[Preference, float]])`；
  `begin_turn` 异步重载长期列表；失败抛 `PreferenceWriteError`（工具层转成 `ok:false`）。

测试覆盖：

```python
def test_long_term_add_writes_text_and_vector(tmp_path)
def test_long_term_add_reuses_duplicate_without_embedding(tmp_path)  # 假 embedder 计数 == 0
def test_long_term_add_returns_similar_records(tmp_path)
def test_long_term_embedding_failure_writes_nothing(tmp_path)
def test_long_term_survives_a_new_memory_instance(tmp_path)
```

- [x] **Step 1: 写失败测试**。 **Step 2: 看红。** **Step 3: 实现。** **Step 4: 看绿。**

---

### Task 5: 三个工具

**Files:**
- Modify: `backend/src/harbor/agents/preferences.py`
- Test: `backend/tests/test_preference_tools.py`

**Interfaces:**
- Produces: `make_preference_tools(memory: PreferenceMemory) -> list[FunctionTool]`，工具名
  `add_preference` / `update_preference` / `delete_preference`，均 `is_state_injected=True`。

测试直接 `await tool.call(_agent_state=state, ...)`：

```python
def test_add_preference_tool_returns_id_and_counts()
def test_update_preference_tool_keeps_id()
def test_delete_preference_tool_reports_missing_id()
def test_add_preference_tool_reports_embedding_failure_without_raising()
def test_tools_are_state_injected_and_read_only_false()
```

- [x] **Step 1: 写失败测试**。 **Step 2: 看红。** **Step 3: 实现。** **Step 4: 看绿。**

---

### Task 6: 三作用域注入

**Files:**
- Modify: `backend/src/harbor/agents/context_governance.py`
- Modify: `backend/tests/test_context_governance.py`

**Interfaces:**
- Consumes: `PreferenceMemory`
- Produces: `ContextGovernanceMiddleware(..., preferences: PreferenceMemory | None = None)`；
  `on_reply` 开头 `await preferences.begin_turn(agent.state)`；
  `_with_preferences` 输出三段式 `UserMsg`，未接 memory 时保持 S7 行为。

测试覆盖：

```python
def test_injection_renders_three_scopes_in_order()
def test_injection_omits_empty_scopes()
def test_injection_falls_back_to_s7_when_no_memory()
def test_injection_does_not_touch_state_context()
def test_begin_turn_clears_only_turn_scope()
```

- [x] **Step 1: 写失败测试**。 **Step 2: 看红。** **Step 3: 实现。** **Step 4: 看绿。**

---

### Task 7: 会话偏好合并（交给人类）

**Files:**
- Modify: `backend/src/harbor/agents/preferences.py`
- Modify: `backend/tests/test_preferences.py`

- [x] **Step 1: 写失败测试**（三态：只有显式、只有启发式、两者同键）。
- [x] **Step 2: 留 `TODO(human)` 并请人类实现 `merged_session_items(explicit, heuristic)`。**
- [x] **Step 3: 人类实现后跑绿。**（用户指示由 Claude 代实现，已绿）

---

### Task 8: 装配（factory / config / prompts / identity）

**Files:**
- Modify: `backend/src/harbor/agents/factory.py`
- Modify: `backend/src/harbor/core/config.py`
- Modify: `backend/src/harbor/agents/prompts.py`
- Modify: `backend/src/harbor/features/chat/service.py`
- Modify: `backend/tests/test_factory.py`、`backend/tests/test_chat_service.py`

测试覆盖：

```python
def test_build_agent_governed_registers_preference_tools()
def test_build_agent_legacy_has_no_preference_tools()
def test_build_agent_without_identity_skips_long_term_tools()
def test_session_store_injects_buyer_identity_into_state()
```

- [x] **Step 1: 写失败测试**。 **Step 2: 看红。** **Step 3: 实现。** **Step 4: 看绿。**

---

### Task 9: 验收脚本与文档

**Files:**
- Create: `backend/scripts/accept_s8_real.py`
- Modify: `docs/s8-preference-memory-design-notes.md`（补真额度记录）
- Create: `docs/_build_log/2026-11-04-s8-acceptance-report.md`

- [x] **Step 1: 脚本**：假 embedder（零额度）+ 真实模型，跑
  「本轮忘掉 / 本会话记住 / 长期记住 → 新会话仍在 → update → delete」。
- [x] **Step 2: 跑通并记录输出。**
- [x] **Step 3: `make check` 全绿。**

---

## 验证

```bash
make lint && make typecheck && make test
uv run --directory backend python scripts/accept_s8_real.py
```
