# S6 模型上下文持久化层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 进程重启后，同一 `(buyer_id, shopping_session_id)` 能从磁盘快照恢复完整会话上下文并继续对话；每次成功回答把 `AgentState` 快照原子落盘、把一问一答两行写进全局流水。

**Architecture:** 新增 `SessionRepository`（文件系统读写真相源：快照 `data/sessions/snapshots/{key_hash}.json` + 流水 `data/sessions/journal.jsonl`）。`SessionStore` 降级为「磁盘上的 LRU 缓存」——未命中时从 repo 恢复 `AgentState` 传给 `build_agent(runtime, state=...)`；`run_turn` 成功时 `persist`（快照硬错误 500 + 流水软错误告警），任何失败路径 `evict`（把内存态滚回「最后一轮成功」）。

**Tech Stack:** Python 3.11 / FastAPI / AgentScope 2.0.11 / pydantic v2 / pytest + pytest tmp_path / `uv run --directory backend`。

**Spec:** [docs/s6-context-persistence-design-notes.md](../s6-context-persistence-design-notes.md) — 本计划从该设计问答展开，执行者两份一起读。

## Global Constraints

- 会话键是 `tuple[str, str]`（`buyer_id`, `shopping_session_id`），定义在 `repository.py` 为 `SessionKey`；`service.py` 从 repository import。
- 错误信封统一 `{"error": {"code", "message"}}`；新失败映射 `SnapshotWriteError → HTTPException(500, "会话快照写入失败")`。
- `agent_factory` 签名从 `Callable[[], Agent]` 改为 `Callable[[AgentState | None], Agent]`。
- 所有测试**离线**（不联网、不 embedding、不建真实索引）；文件系统用 pytest `tmp_path` 隔离，绝不写真实 `data/sessions/`。
- 序列化用 `AgentState.model_dump(mode="json")` / `AgentState.model_validate(...)`；JSON 一律 `ensure_ascii=False` + UTF-8（中文 round-trip）。
- 磁盘 I/O 不堵事件循环：repo 的同步方法经 `asyncio.to_thread` 调用。
- ruff（E501 行长 100）、mypy、`make test`/`make lint`/`make typecheck` 全绿才算完成。

## Review Focus

| 输入 / 失效模式 | 期望行为 | 归属测试 |
|---|---|---|
| 快照文件被截断 / 不是合法 JSON | `load` 返回 `None`（当新会话），warning 记录，不崩溃 | Task 1 `test_load_returns_none_when_corrupt` |
| 快照 `schema_version` ≠ 1（未来版本） | `load` 返回 `None`，不抛 | Task 1 `test_load_returns_none_on_unknown_schema_version` |
| 中文 query / 中文 summary 落盘再读 | 逐字一致（UTF-8 + `ensure_ascii=False`） | Task 2 `test_journal_roundtrips_chinese_text` |
| 同一会话并发两个请求 | 串行（每会话锁），无并发写 | Task 6 `test_lru_evict_skips_inflight_session` + 既有 `test_same_session_concurrent_turns_serialize` |
| 快照写失败后重试 | 500 + 会话被逐出，重试重建 Agent、不重复追加 user msg | Task 5 `test_run_turn_evicts_session_on_failure` + Task 7 `test_intents_snapshot_failure_returns_500` |

---

### Task 1: `SessionRepository` 快照 save/load

**Files:**
- Create: `backend/src/harbor/features/chat/repository.py`
- Test: `backend/tests/test_chat_repository.py`

**Interfaces:**
- Produces: `SessionKey = tuple[str, str]`; `class SnapshotWriteError(Exception)`; `class SessionRepository` with `__init__(self, root: Path)`, `snapshot_path(self, key: SessionKey) -> Path`, `load(self, key: SessionKey) -> AgentState | None`, `save(self, key: SessionKey, state: AgentState) -> None`.

- [ ] **Step 1: Write failing tests** in `tests/test_chat_repository.py`

```python
import json
import pytest
from agentscope.state import AgentState
from agentscope.message import Msg, TextBlock
from harbor.features.chat.repository import SessionRepository, SnapshotWriteError

def _state() -> AgentState:
    s = AgentState()
    s.context.append(Msg(name="user", role="user",
                         content=[TextBlock(type="text", text="预算300给猫买饮水器")]))
    s.context.append(Msg(name="purchase_agent", role="assistant",
                         content=[TextBlock(type="text", text="命中2款")]))
    return s

def test_save_then_load_roundtrips_state(tmp_path):
    repo = SessionRepository(tmp_path)
    repo.save(("u1", "s1"), _state())
    loaded = repo.load(("u1", "s1"))
    assert loaded is not None
    assert len(loaded.context) == 2
    assert loaded.context[0].role == "user"
    assert loaded.context[0].get_text_content() == "预算300给猫买饮水器"

def test_load_returns_none_when_missing(tmp_path):
    assert SessionRepository(tmp_path).load(("u1", "s1")) is None

def test_load_returns_none_when_corrupt(tmp_path):
    repo = SessionRepository(tmp_path)
    path = repo.snapshot_path(("u1", "s1"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert repo.load(("u1", "s1")) is None

def test_load_returns_none_on_unknown_schema_version(tmp_path):
    repo = SessionRepository(tmp_path)
    path = repo.snapshot_path(("u1", "s1"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 999, "state": {}}), encoding="utf-8")
    assert repo.load(("u1", "s1")) is None

def test_snapshot_paths_are_isolated_per_key(tmp_path):
    repo = SessionRepository(tmp_path)
    assert repo.snapshot_path(("u1", "s1")) != repo.snapshot_path(("u2", "s1"))

def test_save_leaves_no_tmp_file(tmp_path):
    repo = SessionRepository(tmp_path)
    repo.save(("u1", "s1"), _state())
    assert list(tmp_path.rglob("*.tmp")) == []

def test_save_raises_snapshot_write_error_when_unwritable(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    repo = SessionRepository(blocker)
    with pytest.raises(SnapshotWriteError):
        repo.save(("u1", "s1"), _state())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/test_chat_repository.py -v`
Expected: FAIL (import error — `harbor.features.chat.repository` does not exist).

- [ ] **Step 3: Implement `SessionRepository` snapshot path + save/load** in `repository.py`

要点（签名与测试已定，实现按这些写）：
- `snapshot_path(key)` = `self._root / "snapshots" / f"{sha256(key[0].encode() + b'\\x00' + key[1].encode()).hexdigest()}.json"`。
- `save`：信封 `{"schema_version": 1, "buyer_id": key[0], "shopping_session_id": key[1], "updated_at": datetime.now(timezone.utc).isoformat(), "state": state.model_dump(mode="json")}`；先 `path.parent.mkdir(parents=True, exist_ok=True)`，写同目录 `path.name + ".tmp"`，再 `os.replace(tmp, path)`；任何异常包成 `SnapshotWriteError` 再抛。
- `load`：文件不存在 → `None`；读入后 `schema_version != 1` 或 `AgentState.model_validate(doc["state"])` 抛 → `logger.warning` 后 `None`（不向上抛）。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/test_chat_repository.py -v`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/src/harbor/features/chat/repository.py backend/tests/test_chat_repository.py
git commit -m "feat(chat): add SessionRepository snapshot save/load"
```

---

### Task 2: `SessionRepository` 流水追加

**Files:**
- Modify: `backend/src/harbor/features/chat/repository.py`
- Test: `backend/tests/test_chat_repository.py`

**Interfaces:**
- Produces: `SessionRepository.append_journal(self, key: SessionKey, question: str, response: CommerceIntentResponse, turn: int) -> None`; `SessionRepository.journal_path(self) -> Path`.

- [ ] **Step 1: Write failing tests**

```python
from harbor.features.chat.schemas import CommerceIntentResponse

def test_append_journal_writes_two_lines(tmp_path):
    repo = SessionRepository(tmp_path)
    response = CommerceIntentResponse(final_text="命中1条", cards=[])
    repo.append_journal(("u1", "s1"), "预算300给猫买饮水器", response, turn=1)
    lines = (tmp_path / "journal.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    first, second = json.loads(lines[0]), json.loads(lines[1])
    assert first["role"] == "user" and first["text"] == "预算300给猫买饮水器"
    assert first["turn"] == 1 and first["buyer_id"] == "u1" and first["shopping_session_id"] == "s1"
    assert second["role"] == "assistant" and second["text"] == "命中1条"
    assert "ts" in first and "ts" in second

def test_append_journal_appends_without_overwriting(tmp_path):
    repo = SessionRepository(tmp_path)
    repo.append_journal(("u1", "s1"), "q1", CommerceIntentResponse(final_text="a1", cards=[]), turn=1)
    repo.append_journal(("u1", "s1"), "q2", CommerceIntentResponse(final_text="a2", cards=[]), turn=2)
    lines = (tmp_path / "journal.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 4

def test_journal_roundtrips_chinese_text(tmp_path):
    repo = SessionRepository(tmp_path)
    repo.append_journal(("u1", "s1"), "预算300元给猫买饮水器", CommerceIntentResponse(final_text="命中2款饮水器", cards=[]), turn=1)
    lines = (tmp_path / "journal.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert json.loads(lines[0])["text"] == "预算300元给猫买饮水器"
    assert json.loads(lines[1])["text"] == "命中2款饮水器"

def test_append_journal_includes_cards(tmp_path):
    repo = SessionRepository(tmp_path)
    card = ProductCard(title="猫饮水器", brand="PetGo", category="母婴宠物", price_major=200.0,
                       currency="CNY", weight_kg=1.0, sku_id="X1-S1")
    repo.append_journal(("u1", "s1"), "q", CommerceIntentResponse(final_text="命中1条", cards=[card]), turn=1)
    second = json.loads((tmp_path / "journal.jsonl").read_text(encoding="utf-8").strip().split("\n")[1])
    assert second["cards"][0]["sku_id"] == "X1-S1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/test_chat_repository.py::test_append_journal_writes_two_lines -v`
Expected: FAIL (`SessionRepository` has no attribute `append_journal`).

- [ ] **Step 3: Implement `append_journal`** in `repository.py`

要点：`journal_path()` = `self._root / "journal.jsonl"`。每一行是
`{"role": "user"|"assistant", "buyer_id": key[0], "shopping_session_id": key[1], "ts": datetime.now(timezone.utc).isoformat(), "turn": turn, "text": …, ("cards": [c.model_dump(mode="json") for c in response.cards] 仅 assistant)}`。
两行**拼进一次 `open(path, "a", encoding="utf-8").write(...)`** 调用（避免并发把一问一答拆开），`ensure_ascii=False`。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/test_chat_repository.py -v`
Expected: PASS（Task 1 的 7 条 + 本任务 4 条全绿）。

- [ ] **Step 5: Commit**

```bash
git add backend/src/harbor/features/chat/repository.py backend/tests/test_chat_repository.py
git commit -m "feat(chat): append one-question-one-answer journal lines"
```

---

### Task 3: `SessionStore` 从仓库恢复

**Files:**
- Modify: `backend/src/harbor/features/chat/service.py`
- Test: `backend/tests/test_chat_service.py`

**Interfaces:**
- Consumes: `SessionKey`, `SessionRepository` (from Task 1/2).
- Produces: `SessionStore.__init__(self, agent_factory: Callable[[AgentState | None], Agent], ..., repository: SessionRepository | None = None)`; `get_or_create` 未命中时 `state = await asyncio.to_thread(self._repository.load, key)`，再 `self._agent_factory(state)`。

- [ ] **Step 1: Write failing tests** (加 `from agentscope.state import AgentState` 与 `from harbor.features.chat.repository import SessionRepository` 到 test 文件顶部)

```python
def _repo_state(tmp_path) -> AgentState:
    from agentscope.message import Msg, TextBlock
    s = AgentState()
    s.context.append(Msg(name="user", role="user", content=[TextBlock(type="text", text="hi")]))
    return s

def test_get_or_create_restores_state_from_repository(tmp_path):
    repo = SessionRepository(tmp_path)
    repo.save(("u1", "s1"), _repo_state(tmp_path))
    received: list[AgentState | None] = []
    def factory(state: AgentState | None = None) -> FakeAgent:
        received.append(state)
        return FakeAgent(output=_cards_dict())
    store = SessionStore(agent_factory=factory, repository=repo)
    asyncio.run(store.get_or_create("u1", "s1"))
    assert len(received) == 1 and received[0] is not None

def test_get_or_create_passes_none_when_no_snapshot(tmp_path):
    repo = SessionRepository(tmp_path)
    received: list[AgentState | None] = []
    def factory(state: AgentState | None = None) -> FakeAgent:
        received.append(state)
        return FakeAgent(output=_cards_dict())
    store = SessionStore(agent_factory=factory, repository=repo)
    asyncio.run(store.get_or_create("u1", "s1"))
    assert received == [None]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/test_chat_service.py::test_get_or_create_restores_state_from_repository -v`
Expected: FAIL（`SessionStore.__init__` 不接受 `repository`；`agent_factory` 被零参调用）。

- [ ] **Step 3: Implement repository-aware `SessionStore`** in `service.py`

要点：
- `__init__` 加 `repository: SessionRepository | None = None`，存 `self._repository`。
- `get_or_create` 的 `self._evict_lru()` 之后、建 `Session` 之前，若 `self._repository` 非 None，`state = await asyncio.to_thread(self._repository.load, key)`；否则 `state = None`；`session = Session(agent=self._agent_factory(state), …)`。
- `agent_factory` 类型标注改为 `Callable[[AgentState | None], Agent]`。
- 现有旧测试的工厂都是零参（`lambda: FakeAgent()` 或 `def factory():`），改后 `get_or_create` 会多传一个 `state` 使其崩溃，所以**本任务同时把 `test_chat_service.py` 与 `test_chat_router.py` 里所有 `agent_factory` 的工厂（无论 `lambda: …` 还是 `def factory():`）都改成接受一个可省略参数 `state=None`**（`lambda state=None: FakeAgent()` / `def factory(state=None):`）。`RaisingAgent`/`OverlapAgent` 类本身不用改——是包它们的 lambda 改成 `lambda state=None: RaisingAgent()`。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/test_chat_service.py tests/test_chat_router.py -v`
Expected: PASS（新增 2 条 + 既有全部仍绿）。

- [ ] **Step 5: Commit**

```bash
git add backend/src/harbor/features/chat/service.py backend/tests/test_chat_service.py backend/tests/test_chat_router.py
git commit -m "feat(chat): restore SessionStore sessions from repository snapshot"
```

---

### Task 4: `SessionStore.persist` + `SessionStore.evict`

**Files:**
- Modify: `backend/src/harbor/features/chat/service.py`
- Test: `backend/tests/test_chat_service.py`

**Interfaces:**
- Consumes: `SessionRepository.save` / `append_journal`, `SnapshotWriteError`（Task 1/2）。
- Produces: `SessionStore.persist(self, key, agent, question: str, response: CommerceIntentResponse) -> None`（async；`repository` 为 None 直接返回；先 `await asyncio.to_thread(repo.save, key, agent.state)` 再算 `turn = sum(1 for m in agent.state.context if m.role == "user")`，最后 `try: to_thread(repo.append_journal, …) except Exception: logger.warning`）；`SessionStore.evict(self, key) -> None`（同步 `self._sessions.pop(key, None)`）。

- [ ] **Step 1: Write failing tests**（加一个用真实 `AgentState` 的 stub agent）

```python
from agentscope.state import AgentState
from agentscope.message import Msg, TextBlock, UserMsg
from harbor.features.chat.schemas import CommerceIntentResponse

class PersistingAgent:
    """Stub backed by a real AgentState, so ``persist`` can serialize it."""
    def __init__(self, output: dict | None = None, text: str = ""):
        self.state = AgentState()
        self._output = output
        self._text = text
    async def reply(self, msg, structured_schema=None) -> Msg:
        self.state.context.append(msg)
        result = Msg(name="purchase_agent", role="assistant",
                     content=[TextBlock(type="text", text=self._text)],
                     structured_output=self._output)
        self.state.context.append(result)
        return result

def test_persist_writes_snapshot_and_journal(tmp_path):
    repo = SessionRepository(tmp_path)
    store = SessionStore(agent_factory=lambda state=None: FakeAgent(), repository=repo)
    agent = PersistingAgent(output=_cards_dict())
    agent.state.context.append(UserMsg(name="user", content="给猫买个饮水器"))
    response = CommerceIntentResponse(final_text="命中1条", cards=[])
    asyncio.run(store.persist(("u1", "s1"), agent, "给猫买个饮水器", response))
    assert repo.load(("u1", "s1")) is not None
    assert len((tmp_path / "journal.jsonl").read_text(encoding="utf-8").strip().split("\n")) == 2

def test_persist_is_noop_without_repository(tmp_path):
    store = SessionStore(agent_factory=lambda state=None: FakeAgent())
    agent = PersistingAgent(output=_cards_dict())
    response = CommerceIntentResponse(final_text="命中1条", cards=[])
    asyncio.run(store.persist(("u1", "s1"), agent, "hi", response))  # must not raise

def test_evict_removes_session():
    async def body() -> bool:
        store = SessionStore(agent_factory=lambda state=None: FakeAgent())
        key, _ = await store.get_or_create("u1", "s1")
        store.evict(key)
        return len(store) == 0
    assert asyncio.run(body())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/test_chat_service.py::test_persist_writes_snapshot_and_journal -v`
Expected: FAIL（`SessionStore` 无 `persist`）。

- [ ] **Step 3: Implement `persist` + `evict`** in `service.py`（签名见上 Interfaces；顺序必须是「先 save 后算 turn」，这样 save 失败时不会去读 `state.context`，兼容 FakeState）

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/test_chat_service.py -v`
Expected: PASS（新增 3 条 + 既有全绿）。

- [ ] **Step 5: Commit**

```bash
git add backend/src/harbor/features/chat/service.py backend/tests/test_chat_service.py
git commit -m "feat(chat): add SessionStore.persist and evict"
```

---

### Task 5: `run_turn` 成功持久化 + 失败逐出

**Files:**
- Modify: `backend/src/harbor/features/chat/service.py`
- Test: `backend/tests/test_chat_service.py`

**Interfaces:**
- Consumes: `SessionStore.persist` / `evict`（Task 4）。
- Produces: `run_turn` 成功路径在 `cards = ProductCardList.model_validate(structured)` 后 `response = …`、`await store.persist(key, session.agent, raw_query, response)`、`return response`；整个 `async with session.lock:` 块包在一个 `try/except Exception: store.evict(key); raise` 里，外圈 `finally: store.touch(key)`。

- [ ] **Step 1: Write failing tests**

```python
def test_run_turn_persists_snapshot_and_journal(tmp_path):
    repo = SessionRepository(tmp_path)
    store = SessionStore(agent_factory=lambda state=None: PersistingAgent(output=_cards_dict()), repository=repo)
    asyncio.run(run_turn(store, "u1", "s1", "给猫买个饮水器"))
    assert repo.load(("u1", "s1")) is not None
    assert len((tmp_path / "journal.jsonl").read_text(encoding="utf-8").strip().split("\n")) == 2

def test_run_turn_evicts_session_on_failure(tmp_path):
    repo = SessionRepository(tmp_path)
    created: list[FakeAgent] = []
    def factory(state=None):
        agent = FakeAgent(tool_state=ToolResultState.ERROR)
        created.append(agent)
        return agent
    store = SessionStore(agent_factory=factory, repository=repo)
    with pytest.raises(ToolFailureError):
        asyncio.run(run_turn(store, "u1", "s1", "hi"))
    assert len(store) == 0            # 会话被逐出
    with pytest.raises(ToolFailureError):
        asyncio.run(run_turn(store, "u1", "s1", "hi"))
    assert len(created) == 2          # 重试新建 Agent，而非复用失败的那个

def test_run_turn_does_not_persist_failed_turn(tmp_path):
    repo = SessionRepository(tmp_path)
    store = SessionStore(agent_factory=lambda state=None: FakeAgent(tool_state=ToolResultState.ERROR), repository=repo)
    with pytest.raises(ToolFailureError):
        asyncio.run(run_turn(store, "u1", "s1", "hi"))
    assert repo.load(("u1", "s1")) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/test_chat_service.py::test_run_turn_persists_snapshot_and_journal -v`
Expected: FAIL（`run_turn` 不调用 `persist`，`repo.load` 为 None）。

- [ ] **Step 3: Implement the `run_turn` persist/evict wiring** in `service.py`（结构见 Interfaces；注意 `except Exception` 只包住 `async with session.lock:` 块并 `evict` 后 `raise`，`touch` 放最外 `finally`）

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/test_chat_service.py tests/test_chat_router.py -v`
Expected: PASS（新增 3 条 + 既有 `test_run_turn_*` 与 `test_same_session_concurrent_turns_serialize` 全绿——它们无 repository，`persist` 是 no-op）。

- [ ] **Step 5: Commit**

```bash
git add backend/src/harbor/features/chat/service.py backend/tests/test_chat_service.py
git commit -m "feat(chat): persist successful turns, evict failed turns"
```

---

### Task 6: `_evict_lru` 跳过在飞会话

**Files:**
- Modify: `backend/src/harbor/features/chat/service.py`
- Test: `backend/tests/test_chat_service.py`

**Interfaces:**
- Produces: `_evict_lru` 逐出时**跳过 `lock.locked()` 为真的会话**；若全部在飞则提前 break。

- [ ] **Step 1: Write failing test**

```python
def test_lru_evict_skips_inflight_session() -> None:
    async def body() -> None:
        store = SessionStore(agent_factory=lambda state=None: FakeAgent(), max_sessions=3)
        _, s_a = await store.get_or_create("u1", "a")
        await store.get_or_create("u1", "b")
        await store.get_or_create("u1", "c")
        await s_a.lock.acquire()          # 模拟 a 正在 reply 中（持锁）
        try:
            await store.get_or_create("u1", "d")   # 触发逐出，必须跳过锁住的 a
        finally:
            s_a.lock.release()
        assert ("u1", "a") in store._sessions
        assert len(store) == 3
    asyncio.run(body())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -q tests/test_chat_service.py::test_lru_evict_skips_inflight_session -v`
Expected: FAIL（a 是 LRU 最旧，被逐出 → `("u1","a") not in store._sessions`）。

- [ ] **Step 3: Implement the skip-locked fix** in `service.py`

```python
def _evict_lru(self) -> None:
    while len(self._sessions) >= self._max:
        candidates = [k for k in self._sessions if not self._sessions[k].lock.locked()]
        if not candidates:          # 全部在飞：再逐出会破坏同会话串行，安全停手
            break
        oldest = min(candidates, key=lambda k: self._sessions[k].last_active)
        del self._sessions[oldest]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -q tests/test_chat_service.py -v`
Expected: PASS（含既有 `test_lru_evicts_least_recently_used_when_over_capacity` 仍绿）。

- [ ] **Step 5: Commit**

```bash
git add backend/src/harbor/features/chat/service.py backend/tests/test_chat_service.py
git commit -m "fix(chat): skip in-flight sessions during LRU eviction"
```

---

### Task 7: 装配接线（build_agent / config / main / router / gitignore）

**Files:**
- Modify: `backend/src/harbor/agents/factory.py`（`build_agent` 加 `state` 参数）
- Modify: `backend/src/harbor/core/config.py`（`sessions_dir` 字段 + `from pathlib import Path`）
- Modify: `backend/src/harbor/main.py`（建 repo + 传入 store + factory 带 state）
- Modify: `backend/src/harbor/features/chat/router.py`（映射 `SnapshotWriteError → 500`）
- Modify: `.gitignore`（加 `data/sessions/`）
- Test: `backend/tests/test_chat_router.py`, `backend/tests/test_config.py`

**Interfaces:**
- Produces: `build_agent(runtime: AgentRuntime, state: AgentState | None = None) -> Agent`（透传给 `Agent(..., state=state, ...)`，其余不变）；`Settings.sessions_dir: Path = Path(__file__).resolve().parents[4] / "data" / "sessions"`（可用 `HARBOR_SESSIONS_DIR` 覆盖）；`main.py` lifespan 里 `repo = SessionRepository(settings.sessions_dir)`、`agent_factory=lambda state=None: build_agent(runtime, state=state)`、`SessionStore(agent_factory=…, repository=repo)`。

- [ ] **Step 1: Write failing tests**

```python
# tests/test_chat_router.py —— 追加
from harbor.features.chat.repository import SnapshotWriteError

class _FailingRepo:
    def load(self, key): return None
    def save(self, key, state): raise SnapshotWriteError()
    def append_journal(self, *args, **kwargs): pass

def test_intents_snapshot_failure_returns_500() -> None:
    store = SessionStore(agent_factory=lambda state=None: FakeAgent(output=_cards_dict()),
                         repository=_FailingRepo())
    with _client(store) as client:
        resp = client.post("/commerce/intents",
            json={"buyer_id": "u1", "shopping_session_id": "s1", "raw_query": "hi"})
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "http_500"

# tests/test_config.py —— 新建
from harbor.core.config import Settings

def test_sessions_dir_defaults_to_absolute_repo_root() -> None:
    s = Settings(_env_file=None)
    assert s.sessions_dir.is_absolute()
    assert s.sessions_dir.name == "sessions"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/test_chat_router.py::test_intents_snapshot_failure_returns_500 tests/test_config.py -v`
Expected: FAIL（`SessionStore` 无 `repository` 参数 / `Settings` 无 `sessions_dir` / router 无 500 映射）。

- [ ] **Step 3: Implement the wiring** in `factory.py`、`config.py`、`main.py`、`router.py`、`.gitignore`（签名见 Interfaces；`router.py` 在 `except` 链里加 `except SnapshotWriteError as exc: raise HTTPException(status_code=500, detail="会话快照写入失败") from exc`；`build_agent` 无独立单测，由 Task 3 的 factory 契约测试 + S6 冒烟脚本端到端覆盖）

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/test_chat_router.py tests/test_config.py -v`
Expected: PASS（新增 2 条 + 既有 router 测试全绿）。

- [ ] **Step 5: Full suite + lint + typecheck + commit**

```bash
uv run pytest -q                      # 全量离线测试全绿
uv run ruff check .                   # lint 绿
uv run mypy src                        # typecheck 绿
git add -A
git commit -m "feat(chat): wire context persistence into app assembly"
```

---

## 附：S6 冒烟验证（写实现之后手动跑，非测试步骤）

`make test` 全绿后，可停掉 `make dev-api`（Qdrant 目录锁）后跑一次真模型冒烟确认端到端恢复：

```bash
uv run --directory backend python scripts/verify_s5.py   # 回归 S5 九条仍绿
# 新 S6 冒烟：同 (buyer_id, shopping_session_id) 连问两轮，检查 data/sessions/ 下快照+流水落盘，
# 重启进程再问第三轮，确认能接着上文（可先做最小两轮 probe，别全量跑——embedding 额度稀缺）。
```
