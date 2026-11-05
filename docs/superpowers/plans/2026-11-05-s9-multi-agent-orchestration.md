# S9 多 Agent 编排 — 实现计划

> **For agentic workers:** 按任务顺序 TDD 执行：先写失败测试，看它红，再写最小实现，看它绿。

**Goal:** 新增 `task_dispatch` 工具与两套 prompt，让主 Agent 能把一批互不依赖、
链路够深的检索任务并发派给只读子 Agent，并只收回结论、证据与核验过的商品 id。

**Architecture:** 子 Agent 是每次新建的 AgentScope `Agent`，只有 `product_search`、
没有对话历史、不可递归；dispatch 层用 `asyncio.Semaphore` 限制并发，在工具层钉住
硬约束，用子 Agent 真实工具返回值核验 claim，并把批次计划写进
`AgentState.middle_context["harbor.dispatch"]`。

**Tech Stack:** Python 3.11、AgentScope 2.0.11、pydantic v2、pytest、ruff、mypy。

**Spec:** `docs/superpowers/specs/2026-11-05-s9-multi-agent-orchestration-design.md`

## Global Constraints

- 主上下文只能看到 `summary + evidence_refs + verified_product_ids`（外加
  `task_id` / `ok` 信封字段）；子 Agent 的原始工具结果一律不出现。
- `verified` 只由 dispatch 层根据真实 `product_search` 返回值计算，子 Agent 不能自证。
- 子 Agent 的 toolkit 只有 `product_search`：无偏好写工具、无 `read_archive`、
  无递归 `task_dispatch`。
- 硬约束（类目、预算、币种、重量、排除材质）在子 Agent 的工具层固定，模型改不动。
- 默认并发 3；批次截止时间 75s，必须小于 `turn_timeout_seconds`（120s）。
- 子 Agent 失败/超时返回 `{"ok": false, ...}` 的 SUCCESS ToolChunk，不得让整轮 502。
- `legacy` 策略下不注册 `task_dispatch`，也不追加派活指引。
- 测试零网络、零真实 embedding、零真实模型调用。

## Review Focus

- **claim 指向真实但不在本次工具返回里的 id**（例如上一轮记得的商品）：期望
  `verified=false`，且该 id 不出现在 `verified_product_ids`。
- **批次超时**：期望已完成任务的结果保留，未完成任务各自返回 `ok=false`，而不是丢结果或抛异常。
- **两个并发 `task_dispatch`**：期望两个批次的 `t1` 各自保留，计划块不互相覆盖。
- **子 Agent 传了与固定约束冲突的参数**（例如更大的预算）：期望固定值胜出，
  不能借模型参数放宽任务约束。
- **快照恢复后的计划**：期望 `build_agent` 同时恢复 `task_dispatch` 工具与
  「上次派发计划」提示，失败/未完成任务被点名。

---

### Task 1: `product_search` 支持固定约束

**Files:**
- Modify: `backend/src/harbor/agents/tools.py`
- Test: `backend/tests/test_product_search_tool.py`

**Interfaces:**
- Produces: `FixedSearchConstraints(category: str | None, price_max_major: float | None,
  currency: str, weight_max_kg: float | None, excluded_material_tags: tuple[str, ...])`
- Produces: `make_product_search_tool(service, memory=None, *,
  fixed_constraints: FixedSearchConstraints | None = None, description: str | None = None)
  -> FunctionTool`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_product_search_tool.py` 追加：

```python
from harbor.agents.tools import FixedSearchConstraints, make_product_search_tool


class _RecordingService:
    def __init__(self) -> None:
        self.specs: list[ProductSearchSpec] = []

    def search(self, query, spec, limit=10):
        self.specs.append(spec)
        return SearchResult(hits=[], total=0)


class _FakeMemory:
    def blacklisted_material_tags(self, _state):
        return ("玻璃",)


def test_fixed_constraints_override_model_arguments() -> None:
    service = _RecordingService()
    tool = make_product_search_tool(
        service,
        memory=_FakeMemory(),
        fixed_constraints=FixedSearchConstraints(
            category="母婴宠物",
            price_max_major=500.0,
            currency="CNY",
            weight_max_kg=1.0,
            excluded_material_tags=("塑料",),
        ),
    )

    asyncio.run(
        tool.call(
            _agent_state=AgentState(),
            query="饮水器",
            category="家用电器",
            price_max_major=9999.0,
            currency="USD",
            weight_max_kg=5.0,
            excluded_material_tags=["金属"],
        )
    )

    spec = service.specs[0]
    assert spec.category == "母婴宠物"
    assert spec.price_max_major == Decimal("500.0")
    assert spec.currency == "CNY"
    assert spec.weight_max_kg == 1.0
    assert set(spec.excluded_material_tags) == {"塑料", "玻璃", "金属"}


def test_unspecified_fixed_fields_fall_back_to_model_arguments() -> None:
    service = _RecordingService()
    tool = make_product_search_tool(
        service, fixed_constraints=FixedSearchConstraints(currency="CNY")
    )

    asyncio.run(tool.call(_agent_state=AgentState(), query="杯子", category="家居生活"))

    assert service.specs[0].category == "家居生活"
```

测试文件顶部补 `from decimal import Decimal`、`from agentscope.state import AgentState`、
`from harbor.features.catalog.search.schemas import SearchResult`。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --directory backend pytest tests/test_product_search_tool.py::test_fixed_constraints_override_model_arguments -v`
Expected: FAIL — `ImportError: cannot import name 'FixedSearchConstraints'`

- [ ] **Step 3: 实现**

在 `tools.py` 加 dataclass（`@dataclass(frozen=True)`），并改工厂签名：

```python
@dataclass(frozen=True)
class FixedSearchConstraints:
    category: str | None = None
    price_max_major: float | None = None
    currency: str = "CNY"
    weight_max_kg: float | None = None
    excluded_material_tags: tuple[str, ...] = ()


def make_product_search_tool(
    service,
    memory=None,
    *,
    fixed_constraints: FixedSearchConstraints | None = None,
    description: str | None = None,
) -> FunctionTool:
```

工具体内解析顺序：`fixed` 的字段非 `None` 时覆盖模型参数；`currency` 永远以
`fixed.currency` 为准（有 fixed 时）；`excluded` 取
`fixed.excluded_material_tags ∪ memory 黑名单 ∪ 模型参数` 去重保序。
`FunctionTool(..., description=description or 原文案)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --directory backend pytest tests/test_product_search_tool.py -v`
Expected: PASS，原有用例全绿。

- [ ] **Step 5: 提交**

```bash
git add backend/src/harbor/agents/tools.py backend/tests/test_product_search_tool.py
git commit -m "feat(s9): pin fixed search constraints at the tool layer"
```

---

### Task 2: dispatch 契约与证据核验

**Files:**
- Create: `backend/src/harbor/agents/dispatch.py`
- Test: `backend/tests/test_dispatch.py`

**Interfaces:**
- Consumes: `FixedSearchConstraints`（Task 1）
- Produces:
  - `TaskBrief` (frozen dataclass) 字段：`task_id, goal, category, price_max_major,
    currency, weight_max_kg, excluded_material_tags, known_product_ids`；
    方法 `fixed_constraints() -> FixedSearchConstraints`、`render() -> str`
  - `Claim(product_id: str, field: str | None = None, value: Any = None, statement: str = "")`
  - `SubAgentOutput(summary: str, claims: list[Claim])`
  - `EvidenceRef(product_id, field, value, statement, source="product_search", verified: bool)`
  - `DispatchResult(task_id: str, ok: bool, summary: str,
    evidence_refs: list[EvidenceRef], verified_product_ids: list[str])`
  - `SubAgentRun(output: SubAgentOutput, observed: dict[str, dict[str, Any]])`
  - `verify_claims(output: SubAgentOutput, observed: Mapping[str, Mapping[str, Any]])
    -> tuple[list[EvidenceRef], list[str]]`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_dispatch.py`：

```python
from harbor.agents.dispatch import Claim, SubAgentOutput, TaskBrief, verify_claims


def test_verify_marks_fabricated_product_unverified() -> None:
    output = SubAgentOutput(
        summary="找到一款", claims=[Claim(product_id="P404", statement="很合适")]
    )

    refs, verified_ids = verify_claims(output, {"P1": {"price_major": 89.0}})

    assert refs[0].verified is False
    assert verified_ids == ["P1"]


def test_verify_marks_wrong_field_value_unverified() -> None:
    output = SubAgentOutput(
        summary="89 元",
        claims=[Claim(product_id="P1", field="price_major", value=99.0)],
    )

    refs, _ = verify_claims(output, {"P1": {"price_major": 89.0}})

    assert refs[0].verified is False


def test_verify_accepts_matching_value_and_int_float_equivalence() -> None:
    output = SubAgentOutput(
        summary="89 元",
        claims=[
            Claim(product_id="P1", field="price_major", value=89),
            Claim(product_id="P1", statement="真实存在"),
        ],
    )

    refs, verified_ids = verify_claims(output, {"P1": {"price_major": 89.0}})

    assert [ref.verified for ref in refs] == [True, True]
    assert verified_ids == ["P1"]


def test_verify_marks_unknown_field_unverified() -> None:
    output = SubAgentOutput(
        summary="…",
        claims=[Claim(product_id="P1", field="stock", value=5)],
    )

    refs, _ = verify_claims(output, {"P1": {"price_major": 89.0}})

    assert refs[0].verified is False


def test_brief_renders_all_constraints_and_known_ids() -> None:
    brief = TaskBrief(
        task_id="t1",
        goal="母婴宠物类目找 500 元以内的饮水器",
        category="母婴宠物",
        price_max_major=500.0,
        currency="CNY",
        weight_max_kg=1.0,
        excluded_material_tags=("塑料",),
        known_product_ids=("P1",),
    )

    text = brief.render()

    assert "目标：母婴宠物类目找 500 元以内的饮水器" in text
    assert "- category: 母婴宠物" in text
    assert "- price_max_major: 500.0" in text
    assert "- excluded_material_tags: 塑料" in text
    assert "- known_product_ids（用户已见过，仅供参考）: P1" in text


def test_brief_marks_unspecified_fields() -> None:
    text = TaskBrief(task_id="t1", goal="找杯子").render()

    assert "- category: 未指定" in text
    assert "- excluded_material_tags: 无" in text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --directory backend pytest tests/test_dispatch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor.agents.dispatch'`

- [ ] **Step 3: 实现**

`dispatch.py` 中定义上列 dataclass / pydantic 模型与纯函数。核验规则：

- `verified_product_ids = sorted(observed)`。
- claim 的 `product_id` 不在 `observed` → `verified=False`。
- `field is None` 且在 `observed` → `verified=True`。
- `field` 存在但不在 `observed[product_id]` → `verified=False`。
- 值比较：两边都是 `int | float` 时按 `float` 比；否则 `==`。
- 每条 claim 都产出一条 `EvidenceRef`，`source="product_search"`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --directory backend pytest tests/test_dispatch.py -v`
Expected: PASS（6 项）。

- [ ] **Step 5: 提交**

```bash
git add backend/src/harbor/agents/dispatch.py backend/tests/test_dispatch.py
git commit -m "feat(s9): add dispatch contracts and evidence verification"
```

---

### Task 3: 批次并发、超时与计划账本

**Files:**
- Modify: `backend/src/harbor/agents/dispatch.py`
- Test: `backend/tests/test_dispatch.py`

**Interfaces:**
- Consumes: Task 2 的模型与 `TaskBrief`
- Produces:
  - `DISPATCH_STATE_KEY = "harbor.dispatch"`
  - `DispatchTaskArgs(goal, category, price_max_major, currency, weight_max_kg,
    excluded_material_tags, known_product_ids)`
  - `TaskDispatchArgs(tasks: list[DispatchTaskArgs])`
  - `build_briefs(tasks: Sequence[DispatchTaskArgs], blacklist: Sequence[str])
    -> list[TaskBrief]`
  - `read_dispatch_ledger(state: AgentState | None) -> dict[str, Any]`
  - `make_task_dispatch_tool(memory, runner, *, max_concurrency: int = 3,
    task_timeout_seconds: float = 90.0, batch_timeout_seconds: float = 75.0,
    max_tasks: int = 6) -> FunctionTool`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_dispatch.py` 追加：

```python
import asyncio
import time

from harbor.agents.dispatch import (
    DISPATCH_STATE_KEY,
    DispatchTaskArgs,
    SubAgentRun,
    build_briefs,
    make_task_dispatch_tool,
    read_dispatch_ledger,
)


class _FakeRunner:
    def __init__(self, *, delay: float = 0.0,
                 observed: dict | None = None, claims: list | None = None) -> None:
        self.delay = delay
        self.observed = observed or {"P1": {"price_major": 89.0}}
        self.claims = claims or []

    async def run(self, brief):
        if self.delay:
            await asyncio.sleep(self.delay)
        return SubAgentRun(
            output=SubAgentOutput(summary=f"完成 {brief.goal}", claims=self.claims),
            observed=self.observed,
        )


def _dispatch(tool, state, tasks):
    return asyncio.run(tool.call(_agent_state=state, tasks=tasks))


def test_batch_runs_concurrently_not_serially() -> None:
    tool = make_task_dispatch_tool(None, _FakeRunner(delay=0.4), max_concurrency=3)

    started = time.perf_counter()
    payload = _dispatch(tool, AgentState(), [{"goal": "a"}, {"goal": "b"}])
    elapsed = time.perf_counter() - started

    assert [r["ok"] for r in payload["results"]] == [True, True]
    assert elapsed < 0.65  # serial would be >= 0.8


class _FlakyRunner:
    """Succeeds on the first call, raises on the second."""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, brief):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("boom")
        return SubAgentRun(
            output=SubAgentOutput(summary="ok", claims=[]),
            observed={"P1": {"price_major": 89.0}},
        )


def test_one_failing_task_does_not_fail_the_batch() -> None:
    tool = make_task_dispatch_tool(None, _FlakyRunner(), max_concurrency=3)

    payload = _dispatch(tool, AgentState(), [{"goal": "ok"}, {"goal": "fail"}])

    assert payload["results"][0]["ok"] is True
    assert payload["results"][1]["ok"] is False


def test_task_and_batch_timeouts_are_soft_failures() -> None:
    task_timeout_tool = make_task_dispatch_tool(
        None, _FakeRunner(delay=1.0),
        task_timeout_seconds=0.05, batch_timeout_seconds=2.0,
    )
    task = _dispatch(task_timeout_tool, AgentState(), [{"goal": "slow"}])
    assert task["results"][0]["ok"] is False
    assert "超时" in task["results"][0]["summary"]

    batch_timeout_tool = make_task_dispatch_tool(
        None, _FakeRunner(delay=1.0),
        task_timeout_seconds=5.0, batch_timeout_seconds=0.05,
    )
    batch = _dispatch(batch_timeout_tool, AgentState(), [{"goal": "slow"}])
    assert batch["results"][0]["ok"] is False
    assert "批次超时" in batch["results"][0]["summary"]


def test_batch_timeout_keeps_completed_results() -> None:
    class _StaggeredRunner:
        async def run(self, brief):
            if brief.goal == "slow":
                await asyncio.sleep(1.0)
            return SubAgentRun(
                output=SubAgentOutput(summary=f"完成 {brief.goal}", claims=[]),
                observed={"P1": {"price_major": 89.0}},
            )

    tool = make_task_dispatch_tool(
        None, _StaggeredRunner(),
        task_timeout_seconds=5.0, batch_timeout_seconds=0.05,
    )

    payload = _dispatch(tool, AgentState(), [{"goal": "fast"}, {"goal": "slow"}])

    assert payload["results"][0]["ok"] is True
    assert payload["results"][1]["ok"] is False
    assert "批次超时" in payload["results"][1]["summary"]


def test_too_many_tasks_is_a_soft_failure() -> None:
    tool = make_task_dispatch_tool(None, _FakeRunner(), max_tasks=2)

    payload = _dispatch(tool, AgentState(), [{"goal": "a"}, {"goal": "b"}, {"goal": "c"}])

    assert payload["ok"] is False
    assert payload["code"] == "too_many_tasks"


def test_run_writes_a_plan_into_middle_context() -> None:
    state = AgentState()
    tool = make_task_dispatch_tool(None, _FakeRunner())

    _dispatch(tool, state, [{"goal": "找饮水器"}])

    ledger = read_dispatch_ledger(state)
    entry = ledger["batches"]["b1"]["t1"]
    assert entry["goal"] == "找饮水器"
    assert entry["status"] == "ok"
    assert entry["verified_product_ids"] == ["P1"]


def test_two_concurrent_dispatches_keep_both_batches() -> None:
    state = AgentState()
    tool = make_task_dispatch_tool(None, _FakeRunner(delay=0.1))

    async def run():
        return await asyncio.gather(
            tool.call(_agent_state=state, tasks=[{"goal": "a"}]),
            tool.call(_agent_state=state, tasks=[{"goal": "b"}]),
        )

    asyncio.run(run())

    batches = read_dispatch_ledger(state)["batches"]
    assert set(batches) == {"b1", "b2"}
    assert batches["b1"]["t1"]["goal"] == "a"
    assert batches["b2"]["t1"]["goal"] == "b"


def test_build_briefs_assigns_ids_and_merges_blacklist() -> None:
    briefs = build_briefs(
        [DispatchTaskArgs(goal="a", excluded_material_tags=["塑料"]),
         DispatchTaskArgs(goal="b")],
        blacklist=("玻璃",),
    )

    assert [brief.task_id for brief in briefs] == ["t1", "t2"]
    assert set(briefs[0].excluded_material_tags) == {"塑料", "玻璃"}
    assert set(briefs[1].excluded_material_tags) == {"玻璃"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --directory backend pytest tests/test_dispatch.py -v`
Expected: FAIL — `ImportError: cannot import name 'make_task_dispatch_tool'`

- [ ] **Step 3: 实现**

- `build_briefs`：`task_id=f"t{i}"`；排除项 = 任务字段 ∪ `blacklist`，去重保序。
- 工具函数 `async def task_dispatch(tasks, _agent_state=None)`：
  1. 空列表或超过 `max_tasks` → 返回 `{"ok": false, "code": ..., "results": []}` 的 SUCCESS。
  2. 从 `_agent_state` 取买家材质黑名单（`memory.blacklisted_material_tags(state)`；
     `memory is None` 时为空）。
  3. 在模块级 `_LEDGER_LOCK = asyncio.Lock()` 内领取 `revision`，写入本批次
     `b{revision}` 的 `running` 条目；释放锁后再跑子任务。
  4. 每个任务：先算批次剩余时间，`asyncio.wait_for` 包住
     `semaphore -> asyncio.wait_for(runner.run(brief), task_timeout)`；超时分别
     返回 `summary="子任务超时"` / `"批次超时未完成"`。
  5. 用 `verify_claims` 把 `SubAgentRun` 转成 `DispatchResult`。
  6. 锁内回写状态与结果，`revision = max(旧值, 本批 revision)`，只保留最近 5 个批次。
- 返回 `{"dispatched", "concurrency", "elapsed_ms", "results"}`；`results` 每项
  只含 `task_id / ok / summary / evidence_refs / verified_product_ids`。
- `read_dispatch_ledger(state)` 返回 `state.middle_context.get(DISPATCH_STATE_KEY, {})`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --directory backend pytest tests/test_dispatch.py -v`
Expected: PASS（14 项）。

- [ ] **Step 5: 提交**

```bash
git add backend/src/harbor/agents/dispatch.py backend/tests/test_dispatch.py
git commit -m "feat(s9): run dispatch batches concurrently with a state ledger"
```

---

### Task 4: 两套 prompt 与恢复提示

**Files:**
- Modify: `backend/src/harbor/agents/prompts.py`
- Test: `backend/tests/test_dispatch_prompts.py`

**Interfaces:**
- Produces:
  - `DISPATCH_GUIDANCE: str`
  - `SUBAGENT_SYSTEM_PROMPT: str`
  - `system_prompt_for(categories, *, archive_enabled=True, preferences_enabled=False,
    dispatch_enabled=False, dispatch_ledger: Mapping[str, Any] | None = None) -> str`
  - `subagent_system_prompt(*, brief_block: str, categories: Sequence[str],
    memory_block: str = "") -> str`
  - `dispatch_ledger_prompt_block(ledger: Mapping[str, Any] | None) -> str`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_dispatch_prompts.py`：

```python
from harbor.agents.prompts import (
    dispatch_ledger_prompt_block,
    subagent_system_prompt,
    system_prompt_for,
)


def test_dispatch_guidance_is_opt_in() -> None:
    base = system_prompt_for(["家用电器"])
    enabled = system_prompt_for(["家用电器"], dispatch_enabled=True)

    assert "task_dispatch" not in base
    assert "task_dispatch" in enabled
    for phrase in ("可并行", "上下文隔离", "链路够深", "任一不成立"):
        assert phrase in enabled


def test_ledger_block_names_unfinished_tasks() -> None:
    ledger = {
        "revision": 2,
        "batches": {
            "b2": {
                "t1": {"goal": "找饮水器", "status": "failed"},
                "t2": {"goal": "找猫砂", "status": "ok"},
            }
        },
    }

    block = dispatch_ledger_prompt_block(ledger)

    assert "上次派发计划" in block
    assert "找饮水器" in block and "failed" in block
    assert "找猫砂" not in block


def test_subagent_prompt_has_no_user_and_a_fixed_output_shape() -> None:
    prompt = subagent_system_prompt(
        brief_block="目标：找杯子", categories=["家用电器"], memory_block=""
    )

    assert "没有用户" in prompt
    assert '"summary"' in prompt and '"claims"' in prompt
    assert "家用电器" in prompt
    assert "add_preference" not in prompt


def test_subagent_prompt_carries_memory_and_brief() -> None:
    prompt = subagent_system_prompt(
        brief_block="目标：找杯子",
        categories=["家用电器"],
        memory_block="[长期·不喜欢] 不要塑料",
    )

    assert "目标：找杯子" in prompt
    assert "不要塑料" in prompt
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --directory backend pytest tests/test_dispatch_prompts.py -v`
Expected: FAIL — `ImportError: cannot import name 'dispatch_ledger_prompt_block'`

- [ ] **Step 3: 实现**

`DISPATCH_GUIDANCE` 精确文案：

```text
## 什么时候派人去做
你有 task_dispatch：一次调用可以派一批互不依赖的子任务，并发执行。
只在下面这类场景考虑它：多目标比价、多品类并行寻货、大范围搜索。
派活前必须同时满足三条：
1. 可并行：任务之间没有先后依赖，拆开不改变结论。
2. 需要上下文隔离：中间过程很长，留在主上下文会挤掉重要信息。
3. 链路够深：子任务需要多轮检索、换词或筛选。
任一不成立，就自己做——一次 product_search 能完成的，不要派。
订单/「查单」能力尚未接入，不要派交易类任务。

## 怎么把活说明白
- goal 必须自包含：子 Agent 没有用户，也没有这段对话，它不知道「刚才那个」。
- 预算、类目、币种、重量、排除材质、已知商品 id 用字段填，不要只写在 goal 里。
- 一个任务只放一个目标；两个品类就派两个任务。
- 不要在同一批里放有依赖关系的任务。

## 怎么读回执
- 只把 evidence_refs 里 verified=true 的记录当作事实。
- summary 是子 Agent 的转述，不是证据；引用商品 id 前先看它是否在 verified_product_ids 里。
- verified=false 的商品必须标「未核实」，或者干脆不提。
- 某个任务 ok=false 时，自己决定重派、自己做或向用户说明缺口；不得假装成功。
```

`SUBAGENT_SYSTEM_PROMPT` 精确文案：

```text
你是 Harbor 采购助手的子任务执行者。

你没有用户，也没有对话历史；你只有下面这一条任务说明。不要提问、不要等待确认、
不要派活，也不要引用任务说明之外的信息。

你只能调用 product_search。任务里的硬约束已经固定在工具层：即使你把参数传成别的值，
也只会按固定约束检索。若在约束内找不到商品，如实报告缺口，不要自行放宽条件。

完成后只输出一个 JSON 对象：
{"summary": "一段结论，不超过 200 字", "claims": [{"product_id": "...", "field": "price_major", "value": 89.0, "statement": "最低价 SKU 89 元"}]}
- claims 只写你从 product_search 返回结果里真实看到的商品；不要编造 product_id。
- field 只能是 product_search 返回里的字段：price_major / title / brand / weight_kg /
  selected_sku_id / score 之一。
- 没有找到就返回 {"summary": "……", "claims": []}。
```

`subagent_system_prompt` = 上述常量 + `brief_block` + 类目词表 + `memory_block`，
空块省略。

`dispatch_ledger_prompt_block`：只列 `status != "ok"` 的条目，格式：

```text
<system-info>上次派发计划（未完成或失败）：
- b2.t1 failed：找饮水器
重派、自己做，或向用户说明缺口；不要当作已完成。
</system-info>
```

`system_prompt_for` 在 `dispatch_enabled` 时把 `DISPATCH_GUIDANCE` 追加到 prompt 尾部；
`dispatch_ledger` 非空时再追加 ledger 块。注意现有实现里「无类目」有一条 early return，
把追加逻辑放在它之前（或统一到最后拼接），保证 `categories=[]` 时派活指引仍然出现。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --directory backend pytest tests/test_dispatch_prompts.py -v`
Expected: PASS（4 项）。

- [ ] **Step 5: 提交**

```bash
git add backend/src/harbor/agents/prompts.py backend/tests/test_dispatch_prompts.py
git commit -m "feat(s9): add dispatch and subagent prompts"
```

---

### Task 5: 真子 Agent runner 与只读工具面

**Files:**
- Modify: `backend/src/harbor/agents/dispatch.py`
- Modify: `backend/src/harbor/agents/permissions.py`
- Test: `backend/tests/test_dispatch.py`

**Interfaces:**
- Consumes: Task 1 的固定约束、Task 2/3 的 `TaskBrief` / `SubAgentRun`、Task 4 的
  `subagent_system_prompt`
- Produces:
  - `AgentSubAgentRunner(*, model, service, categories, memory=None, agent_factory=None)`
    方法 `async run(brief: TaskBrief) -> SubAgentRun`；`agent_factory` 按位置调用：
    `agent_factory(name, prompt, search_tool)`
  - `allow_tool(agent: Agent, tool_name: str) -> None`
  - `allow_product_search_tool(agent)` 改为 `allow_tool(agent, "product_search")` 的包装
  - `allow_dispatch_tool(agent)` 等于 `allow_tool(agent, "task_dispatch")`
  - `SUBAGENT_SEARCH_DESCRIPTION: str`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_dispatch.py` 追加：

```python
from types import SimpleNamespace

from harbor.agents.dispatch import AgentSubAgentRunner
from harbor.features.catalog.models import Product, ProductSearchSpec
from harbor.features.catalog.search.schemas import SearchHit, SearchResult


class _HitService:
    def __init__(self) -> None:
        self.product = _product("P1", price=89.0)

    def search(self, query, spec, limit=10):
        return SearchResult(
            hits=[SearchHit(product=self.product, score=0.9, selected_sku=self.product.skus[0])],
            total=1,
        )


class _FakeAgent:
    def __init__(self, tool, structured, seen):
        self.tool = tool
        self.structured = structured
        self.state = AgentState()
        self.seen = seen

    async def reply(self, msg, structured_schema=None):
        self.seen.append([tool.name for tool in self.tool])
        await self.tool[0].call(query="饮水器")
        return SimpleNamespace(structured_output=self.structured)


def test_runner_exposes_only_product_search_and_records_hits() -> None:
    seen: list[list[str]] = []
    runner = AgentSubAgentRunner(
        model=object(),
        service=_HitService(),
        categories=["家居生活"],
        agent_factory=lambda name, prompt, tool: _FakeAgent(
            [tool], {"summary": "ok", "claims": []}, seen
        ),
    )
    brief = TaskBrief(task_id="t1", goal="找饮水器", category="家居生活")

    run = asyncio.run(runner.run(brief))

    assert run.output.summary == "ok"
    assert "P1" in run.observed
    assert run.observed["P1"]["price_major"] == 89.0
    assert seen == [["product_search"]]


def test_runner_raises_when_structured_output_is_missing() -> None:
    runner = AgentSubAgentRunner(
        model=object(),
        service=_HitService(),
        categories=["家居生活"],
        agent_factory=lambda name, prompt, tool: _FakeAgent([tool], None, []),
    )

    try:
        asyncio.run(runner.run(TaskBrief(task_id="t1", goal="找饮水器")))
    except RuntimeError as exc:
        assert "结构化输出" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
```

`_product` 使用现有测试文件里的 helper（同 Task 1 的 `_product`）。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --directory backend pytest tests/test_dispatch.py::test_runner_exposes_only_product_search_and_records_hits -v`
Expected: FAIL — `ImportError: cannot import name 'AgentSubAgentRunner'`

- [ ] **Step 3: 实现**

- `_RecordingSearchService(inner, observed)`：委托 `inner.search`，把每条 hit 的
  `product_id / title / brand / category / price_major（按 spec.currency 换算）/
  currency / weight_kg / material_tags / selected_sku_id / score` 写进 `observed`。
- `AgentSubAgentRunner.run`：
  1. 建 `_RecordingSearchService` 与 `make_product_search_tool(..., fixed_constraints=brief.fixed_constraints(), description=SUBAGENT_SEARCH_DESCRIPTION)`。
  2. `prompt = subagent_system_prompt(brief_block=brief.render(), categories=self._categories, memory_block=_memory_block(self._memory))`。
  3. `agent = agent_factory(name=f"dispatch_{brief.task_id}", prompt, search_tool)`（默认工厂建真 `Agent`，`ReActConfig(max_iters=6, interruption_raise_cancelled_error=True)`）。
  4. `allow_product_search_tool(agent)`；`reply = await agent.reply(UserMsg(name="user", content=brief.goal), structured_schema=SubAgentOutput)`。
  5. `structured_output is None` → `raise RuntimeError("子 Agent 未返回结构化输出")`；
     否则 `SubAgentOutput.model_validate(...)`。
- `SUBAGENT_SEARCH_DESCRIPTION` 精确文案：

```text
在商品目录里检索满足条件的商品。query 是自然语言描述。本任务的类目、预算、币种、
重量与排除材质已经固定在工具层，你传的值不会放宽它们；返回命中商品与被过滤的原因。
```

- `_memory_block(memory)`：`long_term_items("preference")` 与 `long_term_items("dislike")`
  各取前 24 条，渲染成 `<system-info>买家的长期偏好（只读）…</system-info>`；空时返回 `""`。
- `permissions.py`：新增 `allow_tool(agent, tool_name)`；原
  `allow_product_search_tool` 改为调用它；再补 `allow_dispatch_tool`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --directory backend pytest tests/test_dispatch.py -v`
Expected: PASS（16 项）。

- [ ] **Step 5: 提交**

```bash
git add backend/src/harbor/agents/dispatch.py backend/src/harbor/agents/permissions.py backend/tests/test_dispatch.py
git commit -m "feat(s9): run read-only subagents with recorded search evidence"
```

---

### Task 6: 配置与工厂接线（含 S9-7 恢复）

**Files:**
- Modify: `backend/src/harbor/core/config.py`
- Modify: `backend/src/harbor/agents/factory.py`
- Test: `backend/tests/test_config.py`
- Test: `backend/tests/test_factory.py`

**Interfaces:**
- Consumes: `make_task_dispatch_tool`、`AgentSubAgentRunner`、`read_dispatch_ledger`、
  `allow_dispatch_tool`
- Produces: `Settings.dispatch_enabled`、`dispatch_max_concurrency`、
  `dispatch_task_timeout_seconds`、`dispatch_batch_timeout_seconds`、`dispatch_max_tasks`；
  `build_agent` 在 governed 且 enabled 时注册 `task_dispatch`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_config.py` 追加：

```python
def test_dispatch_defaults(clean_env: Callable[..., Settings]) -> None:
    settings = clean_env()

    assert settings.dispatch_enabled is True
    assert settings.dispatch_max_concurrency == 3
    assert settings.dispatch_task_timeout_seconds == 90.0
    assert settings.dispatch_batch_timeout_seconds == 75.0
    assert settings.dispatch_batch_timeout_seconds < settings.turn_timeout_seconds


def test_dispatch_batch_budget_must_fit_inside_the_turn(
    clean_env: Callable[..., Settings],
) -> None:
    with pytest.raises(ValidationError):
        clean_env(dispatch_batch_timeout_seconds=130.0)
```

顶部补 `from pydantic import ValidationError`。

`backend/tests/test_factory.py` 修改与追加：

```python
# 现有 test_build_agent_wires_s7_governance 的期望集合加入 "task_dispatch"
assert names == {
    "product_search",
    "task_dispatch",
    "read_archive",
    "add_preference",
    "confirm_preference",
    "cancel_preference",
    "update_preference",
    "delete_preference",
}


def test_build_agent_can_turn_dispatch_off(monkeypatch, tmp_path) -> None:
    runtime = _offline_runtime(monkeypatch, tmp_path, dispatch_enabled=False)

    agent = build_agent(runtime)

    assert "task_dispatch" not in _tool_names(agent)
    assert "task_dispatch" not in asyncio.run(agent._get_system_prompt())


def test_build_agent_restores_dispatch_plan_after_restart(monkeypatch, tmp_path) -> None:
    from harbor.agents.dispatch import DISPATCH_STATE_KEY

    runtime = _offline_runtime(monkeypatch, tmp_path)
    state = _identity_state("b1")
    state.middle_context[DISPATCH_STATE_KEY] = {
        "revision": 2,
        "batches": {
            "b2": {"t1": {"goal": "找饮水器", "status": "failed"}}
        },
    }
    restored = AgentState.model_validate(state.model_dump())

    agent = build_agent(runtime, state=restored)

    assert "task_dispatch" in _tool_names(agent)
    assert "task_dispatch" in agent.state.permission_context.allow_rules
    prompt = asyncio.run(agent._get_system_prompt())
    assert "上次派发计划" in prompt and "找饮水器" in prompt
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --directory backend pytest tests/test_config.py tests/test_factory.py -v`
Expected: FAIL — `Settings` 无 `dispatch_enabled`；tool 集合里没有 `task_dispatch`。

- [ ] **Step 3: 实现**

`config.py` 增加五个字段（默认值如 Spec §11），并用
`@model_validator(mode="after")` 校验 `dispatch_batch_timeout_seconds < turn_timeout_seconds`。

`factory.py`：

```python
dispatch_enabled = governed and settings.dispatch_enabled
ledger = read_dispatch_ledger(state) if dispatch_enabled else None
prompt = system_prompt_for(
    runtime.categories,
    archive_enabled=governed,
    preferences_enabled=governed,
    dispatch_enabled=dispatch_enabled,
    dispatch_ledger=ledger,
)
```

governed 分支先把现有三组工具收进一个列表，再按需追加：

```python
tools = [tool, make_read_archive_tool(offloader), *make_preference_tools(memory)]
```

`dispatch_enabled` 时追加：

```python
runner = AgentSubAgentRunner(
    model=runtime.model,
    service=runtime.service,
    categories=runtime.categories,
    memory=memory,
)
tools.append(
    make_task_dispatch_tool(
        memory,
        runner,
        max_concurrency=settings.dispatch_max_concurrency,
        task_timeout_seconds=settings.dispatch_task_timeout_seconds,
        batch_timeout_seconds=settings.dispatch_batch_timeout_seconds,
        max_tasks=settings.dispatch_max_tasks,
    )
)
```

`allow_product_search_tool(agent)` 之后调用 `allow_dispatch_tool(agent)`（仅 enabled 时）。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --directory backend pytest tests/test_config.py tests/test_factory.py -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/src/harbor/core/config.py backend/src/harbor/agents/factory.py \
  backend/tests/test_config.py backend/tests/test_factory.py
git commit -m "feat(s9): wire task_dispatch into config and agent assembly"
```

---

### Task 7: S9 验收清单逐条测试

**Files:**
- Create: `backend/tests/test_s9_acceptance.py`

**Interfaces:**
- Consumes: 前六个任务的全部公开面
- Produces: 7 个测试，名字以 `test_s9_<n>_` 开头，一条对应一条清单

- [ ] **Step 1: 写测试（全部先红）**

```python
"""S9 acceptance checklist: one test per item, against the tool surface."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date
from pathlib import Path

from agentscope.state import AgentState

from harbor.agents.dispatch import (
    DISPATCH_STATE_KEY,
    AgentSubAgentRunner,
    Claim,
    SubAgentOutput,
    SubAgentRun,
    TaskBrief,
    make_task_dispatch_tool,
    read_dispatch_ledger,
)
from harbor.features.catalog.models import Money, Product, Sku
from harbor.features.catalog.search.schemas import SearchHit, SearchResult

# `_product` 与 `_HitService` 原样复制自 Task 5 的测试文件（测试 helper，不进生产代码）。


class _Runner:
    def __init__(self, *, delay=0.0, fail_at=None, claims=None, observed=None):
        self.delay = delay
        self.fail_at = fail_at
        self.calls = 0
        self.claims = claims or []
        self.observed = observed or {"P1": {"price_major": 89.0}}

    async def run(self, brief):
        self.calls += 1
        if self.fail_at == self.calls:
            raise RuntimeError("boom")
        if self.delay:
            await asyncio.sleep(self.delay)
        return SubAgentRun(
            output=SubAgentOutput(summary=f"完成 {brief.goal}", claims=self.claims),
            observed=self.observed,
        )


def _run(tool, state, tasks):
    return asyncio.run(tool.call(_agent_state=state, tasks=tasks))


def test_s9_1_batch_is_faster_than_serial() -> None:
    tool = make_task_dispatch_tool(None, _Runner(delay=0.4), max_concurrency=3)

    started = time.perf_counter()
    payload = _run(tool, AgentState(), [{"goal": "a"}, {"goal": "b"}])
    elapsed = time.perf_counter() - started

    assert all(result["ok"] for result in payload["results"])
    assert elapsed < 0.65  # serial: 2 × 0.4 = 0.8


def test_s9_2_parent_sees_only_summary_and_evidence() -> None:
    tool = make_task_dispatch_tool(None, _Runner())
    payload = _run(tool, AgentState(), [{"goal": "a"}])

    result = payload["results"][0]
    assert set(result) == {
        "task_id",
        "ok",
        "summary",
        "evidence_refs",
        "verified_product_ids",
    }
    text = json.dumps(payload, ensure_ascii=False)
    assert "recall_strategy" not in text
    assert '"hits"' not in text


def test_s9_3_claims_are_verified_against_real_tool_results() -> None:
    runner = _Runner(
        claims=[
            Claim(product_id="P1", field="price_major", value=89.0),
            Claim(product_id="P404", statement="不存在"),
            Claim(product_id="P1", field="price_major", value=99.0),
        ]
    )
    tool = make_task_dispatch_tool(None, runner)

    result = _run(tool, AgentState(), [{"goal": "a"}])["results"][0]

    assert result["verified_product_ids"] == ["P1"]
    assert [ref["verified"] for ref in result["evidence_refs"]] == [True, False, False]


def test_s9_4_one_failure_does_not_fail_the_turn() -> None:
    tool = make_task_dispatch_tool(None, _Runner(fail_at=1))

    payload = _run(tool, AgentState(), [{"goal": "fail"}, {"goal": "ok"}])

    assert payload["results"][0]["ok"] is False
    assert payload["results"][1]["ok"] is True


def test_s9_5_simple_request_gets_no_dispatch_guidance_pressure() -> None:
    from harbor.agents.prompts import system_prompt_for

    prompt = system_prompt_for(["家用电器"], dispatch_enabled=True)

    assert "一次 product_search 能完成的，不要派" in prompt


def test_s9_6_subagents_are_read_only_and_plans_do_not_collide() -> None:
    seen: list[list[str]] = []

    class _Agent:
        def __init__(self, tool):
            self.tool = [tool]
            self.state = AgentState()

        async def reply(self, msg, structured_schema=None):
            seen.append([tool.name for tool in self.tool])
            return type("Reply", (), {"structured_output": {"summary": "ok", "claims": []}})()

    runner = AgentSubAgentRunner(
        model=object(),
        service=_HitService(),
        categories=["家居生活"],
        agent_factory=lambda name, prompt, tool: _Agent(tool),
    )
    asyncio.run(runner.run(TaskBrief(task_id="t1", goal="找饮水器")))
    assert seen == [["product_search"]]

    state = AgentState()
    tool = make_task_dispatch_tool(None, _Runner(delay=0.1))
    asyncio.run(
        asyncio.gather(
            tool.call(_agent_state=state, tasks=[{"goal": "a"}]),
            tool.call(_agent_state=state, tasks=[{"goal": "b"}]),
        )
    )
    assert set(read_dispatch_ledger(state)["batches"]) == {"b1", "b2"}


def test_s9_7_restart_restores_tools_and_plan(monkeypatch, tmp_path: Path) -> None:
    # 复用 test_factory 的离线 runtime 装配（假 embedder、内存索引、fixture 目录）。
    import harbor.agents.factory as factory
    from qdrant_client import QdrantClient

    from harbor.agents.factory import build_agent, build_runtime
    from harbor.core.config import Settings

    fixture = Path(__file__).parent / "fixtures" / "catalog-vacuum-demo.jsonl"
    settings = Settings(
        _env_file=None,
        openai_api_key="sk-test",
        memory_db_path=tmp_path / "memory.sqlite3",
    )
    monkeypatch.setattr(factory, "OpenAIEmbedder", lambda _s: type(
        "E", (), {"embed": lambda self, texts: [[0.0] * 8 for _ in texts]}
    )())
    monkeypatch.setattr(factory, "FastembedReranker", lambda _m: None)
    monkeypatch.setattr(
        factory, "_open_index",
        lambda *_a, **_k: (QdrantClient(":memory:"), "fp-123"),
    )
    monkeypatch.setattr(factory, "FINGERPRINT_PATH", tmp_path / "fingerprint.txt")
    runtime = build_runtime(settings, data_path=fixture)

    state = AgentState()
    state.middle_context[DISPATCH_STATE_KEY] = {
        "revision": 2,
        "batches": {"b2": {"t1": {"goal": "找饮水器", "status": "failed"}}},
    }
    restored = AgentState.model_validate(state.model_dump())

    agent = build_agent(runtime, state=restored)

    names = {tool.name for tool in agent.toolkit.tool_groups[0].tools}
    assert "task_dispatch" in names
    prompt = asyncio.run(agent._get_system_prompt())
    assert "上次派发计划" in prompt and "找饮水器" in prompt
```

`_product` / `_HitService` / `_Runner` 都是测试 helper，不放进生产模块；从
`tests/test_dispatch.py` 原样复制到验收测试顶部，避免跨测试文件 import。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --directory backend pytest tests/test_s9_acceptance.py -v`
Expected: FAIL — `task_dispatch` 尚未在 governed 装配里注册 / 相关名称不存在。

- [ ] **Step 3: 让测试通过**

只补装配缺口，不改机制。若某个测试暴露真实缺陷，按 systematic-debugging 找根因，
不要改断言迁就。

- [ ] **Step 4: 跑全量测试**

Run: `uv run --directory backend pytest -q`
Expected: PASS，S8 及以前全部用例不回归。

- [ ] **Step 5: 提交**

```bash
git add backend/tests/test_s9_acceptance.py
git commit -m "test(s9): cover the seven acceptance items"
```

---

### Task 8: 手工验证脚本

**Files:**
- Create: `backend/scripts/verify_s9.py`
- Create: `backend/scripts/accept_s9_real.py`

**Interfaces:**
- Consumes: 所有公开面
- Produces: 两个可重复运行的脚本；`verify_s9.py` 零 API、零 embedding，
  `accept_s9_real.py` 只用真实聊天模型、embedding 用假向量

- [ ] **Step 1: 写 `verify_s9.py`**

结构照 `backend/scripts/verify_s8.py`：`record(item, ok, detail)`、`show(label, body)`、
七个 `section_s9_*`，写入 `data/sessions/_manual-s9/summary.json`，打印 7 条 ✓/✗。

覆盖：

1. 并发耗时 < 串行（fake runner）。
2. 工具输出字段白名单。
3. 编造 id / 错价格 → `verified=false`。
4. 单任务异常 → 整批仍返回。
5. prompt 含三条判据与「不要派」。
6. 子 Agent toolkit 只有 `product_search`；两个并发 dispatch 的 `b1`/`b2` 都在。
7. `AgentState.model_dump()` 往返后计划块可读。

- [ ] **Step 2: 跑 `verify_s9.py`**

Run: `uv run --directory backend python scripts/verify_s9.py`
Expected: 打印 7 条 ✓，退出码 0；`summary.json` 里 `remote_embedding_calls: 0`。

- [ ] **Step 3: 写 `accept_s9_real.py`**

用真实模型（`build_chat_model(settings)`）与确定性假 embedder（零 embedding）：

- S9-1：两个类目任务一次 `task_dispatch`，记录 `elapsed_ms`；再串行跑两次
  `runner.run` 作为对照，断言并行明显更快。
- S9-2：打印主 Agent 看到的 JSON 键。
- S9-3：让子 Agent 自由返回，核对 `verified_product_ids` 与 `evidence_refs`。
- S9-4：注入一个必失败的 fake runner，确认另一任务仍成功。
- S9-5：真实模型问「帮我找保温杯」，检查消息历史里没有 `task_dispatch` 调用。
- S9-6：断言子 Agent 工具面；并发 dispatch 计划块。
- S9-7：`AgentState` 往返 + `build_agent`。

- [ ] **Step 4: 跑 `accept_s9_real.py`（需要真实 API key，不打印 key）**

Run: `uv run --directory backend python scripts/accept_s9_real.py`
Expected: 7 条 ✓；`embedding_calls` 只来自本地假实现；不出现远程 embedding 请求。

- [ ] **Step 5: 跑 lint / 类型 / 全量测试**

Run: `make check`
Expected: ruff、mypy、pytest 全绿。

- [ ] **Step 6: 提交**

```bash
git add backend/scripts/verify_s9.py backend/scripts/accept_s9_real.py
git commit -m "test(s9): add offline and real-model verification scripts"
```

---

## 实现顺序与依赖

Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6 → Task 7 → Task 8。
Task 2/3 共用 `dispatch.py`，Task 4 的 prompt 常量被 Task 5 使用；Task 6 才把工具接进
`build_agent`。每个任务结束时测试全绿再进入下一个。
