# S10 交易闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让模型只能创建「订单草稿」，由应用层的买家确认动作在 SQLite 里原子地创建订单；取消同样走应用层两步确认。

**Architecture:** 新增 `features/orders/`：`SqliteOrderStore` 管草稿/订单/取消请求/事件，`SqliteEvidenceStore` 存检索证据，`OrderService` 做校验、换汇、量化和状态迁移，`router.py` 暴露应用层动作。模型侧只在 `agents/order_tools.py` 获得 `create_order_draft` 与只读 `get_order`；`orders` 表永远只由确认事务写入。

**Tech Stack:** Python 3.11、FastAPI、Pydantic v2、SQLite（WAL + `BEGIN IMMEDIATE`）、AgentScope 2.0.11。

**Spec:** `docs/superpowers/specs/2026-11-05-s10-transaction-loop-design.md`

## Global Constraints

- 不新增运行时依赖；只用现有 `fastapi` / `pydantic` / `agentscope` / 标准库 `sqlite3`。
- 数据库路径 `data/sessions/orders.sqlite3`；`PRAGMA journal_mode=WAL`、`busy_timeout`、`BEGIN IMMEDIATE`、进程内 `asyncio.Lock`，与 `SqliteMemoryStore` 一致。
- **模型工具只写 `order_drafts` / `order_draft_items` / `order_events`；`orders` 只由应用层 `confirm_draft` 事务写入。**
- 草稿状态 `pending/confirmed/cancelled/superseded`；订单 `created/cancelled`；取消请求 `pending/confirmed/withdrawn/superseded`。
- 价格：服务端用 `fx.convert` 换算到草稿币种，再按 ISO-4217 小数位 `ROUND_HALF_UP` 量化；行小计 = 量化单价 × 数量，总额 = 各行小计之和。模型给的价格一律不采信。
- 本期不做草稿到确认之间的价格变动校验。
- 交易工具的参数/证据校验失败返回 **SUCCESS 工具块 + `ok=false`**；只有存储/程序异常才 ERROR。
- 测试全程离线：不调用远程 embedding、不访问网络。
- `ruff` 行宽 100；验证命令 `make test`、`make lint`、`make typecheck`。
- **除非用户明确要求，不执行任何 commit / merge / push。** 各任务末尾的 commit 步骤是有条件的。

## Review Focus

- **证据记录失败不能拖垮检索。** 模型调用 `product_search` 时，若证据表写入抛异常，检索仍须返回命中；否则本回合的 ERROR 会被 `run_turn` 放大成整轮 502（Task 7 `test_evidence_recording_failure_does_not_fail_search`）。
- **框架层参数校验不能变成 ERROR。** 模型可能一次都不传参数、或传 `items=null`；交易工具的所有 schema 字段必须可省略，校验在函数体内做（Task 8 `test_tool_call_with_no_arguments_is_a_validation_result`）。
- **重复/并发确认只能产生一张订单。** 双击确认、网络重试都不能多出订单（Task 2 `test_concurrent_confirm_creates_one_order`）。
- **跨买家必须当作不存在。** 别的 `buyer_id` 查草稿、确认草稿、查订单都返回 404 / `ok=false not_found`，不泄露存在性（Task 6 `test_other_buyer_cannot_see_or_confirm_draft`、Task 8 `test_get_order_hides_other_buyers_order`）。
- **S7 压缩后证据不能丢。** 上下文里已经没有检索明细时，草稿仍要能靠 `product_evidence` 或压缩摘要附录创建；附录没有 `product_id`，不能强制它存在（Task 5 `test_draft_uses_durable_evidence_when_context_is_empty`、`test_appendix_evidence_without_product_id_is_accepted`）。

---

## File Structure

**新增**

```
backend/src/harbor/features/orders/__init__.py
backend/src/harbor/features/orders/models.py      # 领域 dataclass + 状态常量
backend/src/harbor/features/orders/store.py       # 草稿/订单/取消/事件 SQLite
backend/src/harbor/features/orders/evidence.py    # 检索证据 + 保留期
backend/src/harbor/features/orders/service.py     # 校验/换汇/量化/动作编排
backend/src/harbor/features/orders/schemas.py     # HTTP 视图
backend/src/harbor/features/orders/router.py      # /commerce/orders/*
backend/src/harbor/agents/order_tools.py          # create_order_draft / get_order
backend/tests/test_money_quantization.py
backend/tests/test_orders_store.py
backend/tests/test_order_cancellations.py
backend/tests/test_evidence_store.py
backend/tests/test_order_service.py
backend/tests/test_order_service_actions.py
backend/tests/test_product_search_evidence.py
backend/tests/test_order_tools.py
backend/tests/test_order_prompt.py
backend/tests/test_orders_router.py
backend/tests/test_order_wiring.py
backend/tests/test_cli_orders.py
backend/tests/test_s10_acceptance.py
```

**修改**

```
backend/src/harbor/features/catalog/models/money.py
backend/src/harbor/agents/tools.py
backend/src/harbor/agents/grounding.py
backend/src/harbor/agents/prompts.py
backend/src/harbor/agents/factory.py
backend/src/harbor/agents/permissions.py
backend/src/harbor/agents/cli.py
backend/src/harbor/features/chat/schemas.py
backend/src/harbor/features/chat/service.py
backend/src/harbor/features/chat/router.py
backend/src/harbor/core/config.py
backend/src/harbor/main.py
```

---

### Task 1: 金额量化工具

**Files:**
- Modify: `backend/src/harbor/features/catalog/models/money.py`
- Test: `backend/tests/test_money_quantization.py`

**Interfaces:**
- Consumes: `_MINOR_UNIT_EXPONENTS`（已有）。
- Produces: `minor_unit_exponent(currency: str) -> int`；`quantize_major(amount: Decimal, currency: str) -> Decimal`（`ROUND_HALF_UP`）。后续 Task 5 用它们算草稿单价与总额。

- [ ] **Step 1: Write the failing test**

```python
from decimal import Decimal

from harbor.features.catalog.models.money import minor_unit_exponent, quantize_major


def test_jpy_quantizes_to_whole_yen() -> None:
    assert minor_unit_exponent("JPY") == 0
    assert quantize_major(Decimal("1234.6"), "JPY") == Decimal("1235")


def test_two_decimal_currencies_round_half_up() -> None:
    assert quantize_major(Decimal("1.005"), "CNY") == Decimal("1.01")
    assert quantize_major(Decimal("89.004"), "USD") == Decimal("89.00")


def test_unknown_currency_defaults_to_two_decimals() -> None:
    assert minor_unit_exponent("XYZ") == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/test_money_quantization.py -q`
Expected: FAIL — `ImportError: cannot import name 'minor_unit_exponent'`.

- [ ] **Step 3: Implement the helpers and reuse them in `Money`**

`minor_unit_exponent` returns `_MINOR_UNIT_EXPONENTS.get(currency, 2)`. `quantize_major` builds `Decimal(1).scaleb(-exponent)` and calls `amount.quantize(quantum, rounding=ROUND_HALF_UP)`. Change `Money.amount_in_minor_units` to call `minor_unit_exponent(self.currency)` instead of reading the dict directly.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/test_money_quantization.py tests/test_catalog_models.py -q`
Expected: PASS.

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add backend/src/harbor/features/catalog/models/money.py backend/tests/test_money_quantization.py
git commit -m "feat(s10): add currency quantization helpers"
```

---

### Task 2: 订单存储（草稿 + 订单 + 事件）

**Files:**
- Create: `backend/src/harbor/features/orders/__init__.py`
- Create: `backend/src/harbor/features/orders/models.py`
- Create: `backend/src/harbor/features/orders/store.py`
- Test: `backend/tests/test_orders_store.py`

**Interfaces:**
- Produces（`models.py`）：
  - `DraftItem(line_no, product_id, sku_id, title, quantity, unit_price: Decimal, currency, evidence_unit_price: Decimal, evidence_currency)`
  - `NewDraftItem(product_id, sku_id, title, quantity, unit_price: Decimal, currency, evidence_unit_price: Decimal, evidence_currency)`（无 `line_no`）
  - `OrderDraft(draft_id, buyer_id, shopping_session_id, status, currency, total: Decimal, items: tuple[DraftItem, ...], created_at, updated_at)`
  - `Order(order_id, draft_id, buyer_id, shopping_session_id, status, currency, total: Decimal, items: tuple[DraftItem, ...], created_at, updated_at)`
  - `OrderEvent(event_id, subject_type, subject_id, event, from_status, to_status, actor, source, reason, metadata, created_at)`
  - `SearchEvidence(product_id: str | None, sku_id, title, unit_price: Decimal, currency)`
- Produces（`store.py`）：
  - `SqliteOrderStore(path: Path)`
  - `async create_draft(*, buyer_id, shopping_session_id, currency, items: Sequence[NewDraftItem]) -> tuple[OrderDraft, bool]`（第二项 `replayed`）
  - `async get_draft(draft_id: str) -> OrderDraft | None`
  - `async get_pending_draft(*, buyer_id, shopping_session_id) -> OrderDraft | None`
  - `async confirm_draft(*, draft_id, buyer_id) -> tuple[Order, bool] | None`
  - `async cancel_draft(*, draft_id, buyer_id) -> tuple[OrderDraft, bool] | None`
  - `async get_order(*, order_id, buyer_id) -> Order | None`
  - `async events(*, subject_type, subject_id) -> list[OrderEvent]`
  - 异常 `OrderStoreError`、`DraftNotPending(OrderStoreError)`（带 `.current_status`）。
- 事件名固定：`draft_created`、`draft_superseded`、`draft_confirmed`、`draft_cancelled`、`order_created`。`actor` 为 `model`（写链）或 `buyer`（决议链），`source` 为 `tool` 或 `app`。

- [ ] **Step 1: Write the failing tests**

```python
import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from harbor.features.orders.models import NewDraftItem
from harbor.features.orders.store import DraftNotPending, SqliteOrderStore


def _store(tmp_path: Path) -> SqliteOrderStore:
    return SqliteOrderStore(tmp_path / "orders.sqlite3")


def _item(sku: str = "P1-S1", qty: int = 1, price: str = "89.00") -> NewDraftItem:
    return NewDraftItem(
        product_id="P1", sku_id=sku, title="猫饮水器", quantity=qty,
        unit_price=Decimal(price), currency="CNY",
        evidence_unit_price=Decimal(price), evidence_currency="CNY",
    )


def test_new_draft_supersedes_the_previous_pending_one(tmp_path) -> None:
    async def scenario():
        store = _store(tmp_path)
        first, replayed = await store.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="CNY", items=[_item()]
        )
        second, replayed2 = await store.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="CNY", items=[_item(qty=2)]
        )
        return first, second, replayed, replayed2, await store.get_draft(first.draft_id)

    first, second, replayed, replayed2, old = asyncio.run(scenario())
    assert replayed is False and replayed2 is False
    assert second.draft_id != first.draft_id
    assert old.status == "superseded"


def test_identical_pending_draft_is_replayed(tmp_path) -> None:
    async def scenario():
        store = _store(tmp_path)
        first, _ = await store.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="CNY", items=[_item()]
        )
        second, replayed = await store.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="CNY", items=[_item()]
        )
        return first, second, replayed

    first, second, replayed = asyncio.run(scenario())
    assert replayed is True
    assert second.draft_id == first.draft_id
```

再加：

```python
def test_confirm_creates_order_and_is_idempotent(tmp_path) -> None:
    async def scenario():
        store = _store(tmp_path)
        draft, _ = await store.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="CNY",
            items=[_item(qty=2)],
        )
        order, idem = await store.confirm_draft(draft_id=draft.draft_id, buyer_id="b1")
        again, idem2 = await store.confirm_draft(draft_id=draft.draft_id, buyer_id="b1")
        return draft, order, again, idem, idem2, await store.events(
            subject_type="order", subject_id=order.order_id
        )

    draft, order, again, idem, idem2, events = asyncio.run(scenario())
    assert order.total == Decimal("178.00")
    assert order.items[0].quantity == 2
    assert idem is False and idem2 is True
    assert again.order_id == order.order_id
    assert [e.event for e in events] == ["order_created"]


def test_superseded_draft_cannot_be_confirmed(tmp_path) -> None:
    async def scenario():
        store = _store(tmp_path)
        old, _ = await store.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="CNY", items=[_item()]
        )
        await store.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="CNY", items=[_item(qty=2)]
        )
        with pytest.raises(DraftNotPending) as excinfo:
            await store.confirm_draft(draft_id=old.draft_id, buyer_id="b1")
        return excinfo.value

    error = asyncio.run(scenario())
    assert error.current_status == "superseded"


def test_concurrent_confirm_creates_one_order(tmp_path) -> None:
    async def scenario():
        store = _store(tmp_path)
        draft, _ = await store.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="CNY", items=[_item()]
        )
        results = await asyncio.gather(
            store.confirm_draft(draft_id=draft.draft_id, buyer_id="b1"),
            store.confirm_draft(draft_id=draft.draft_id, buyer_id="b1"),
        )
        return draft, results

    draft, results = asyncio.run(scenario())
    assert results[0][0].order_id == results[1][0].order_id
    assert sum(1 for _, idem in results if not idem) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --directory backend pytest tests/test_orders_store.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor.features.orders'`.

- [ ] **Step 3: Implement models + store**

`models.py` 用 `@dataclass(frozen=True)`；`OrderEvent(event_id, subject_type, subject_id, event, from_status, to_status, actor, source, reason, metadata: str, created_at)`。

`store.py` 的 `SCHEMA` 建 `order_drafts`、`order_draft_items`、`orders`、`order_events` 四张表，含：

```sql
CREATE UNIQUE INDEX IF NOT EXISTS ux_drafts_one_pending
ON order_drafts(buyer_id, shopping_session_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_orders_buyer_created ON orders(buyer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_subject ON order_events(subject_type, subject_id, created_at);
```

写入路径复用 `SqliteMemoryStore` 的形状：`_connect()` 设 `busy_timeout`，`_init_schema()` 设 WAL 并 `executescript`，`async _locked_write(operation)` 用 `asyncio.Lock` + `asyncio.to_thread`，`_write_sync` 里 `BEGIN IMMEDIATE` / `commit` / `rollback`。读路径用独立连接。

- `create_draft` 在**同一事务**里：查当前 pending → 若 items（按 `(product_id, sku_id, quantity)` 排序 + `currency`）完全一致，返回 `(existing, True)` → 否则旧 pending 置 `superseded` 并写 `draft_superseded` → 插入新草稿（`drf_` + `uuid4().hex[:12]`）、行项目、`draft_created`。
- `confirm_draft`：按 `(id, buyer_id)` 查；`confirmed` 时按 `draft_id` 返回已有订单（`True`）；非 `pending` 抛 `DraftNotPending`；否则插订单（`ord_` + 12 hex）、草稿置 `confirmed`、写 `draft_confirmed` + `order_created`。
- `cancel_draft`：`cancelled` 返回 `(draft, True)`；非 `pending` 抛 `DraftNotPending`；否则置 `cancelled` + `draft_cancelled`。
- 订单的 `items` 按 `draft_id` join `order_draft_items` 读回。
- `total` 在事务内由 `sum(unit_price * quantity)` 计算，不信任调用方。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --directory backend pytest tests/test_orders_store.py -q`
Expected: PASS（含并发确认只产生一张订单）。

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add backend/src/harbor/features/orders backend/tests/test_orders_store.py
git commit -m "feat(s10): add sqlite order drafts and orders"
```

---

### Task 3: 取消请求存储

**Files:**
- Modify: `backend/src/harbor/features/orders/models.py`
- Modify: `backend/src/harbor/features/orders/store.py`
- Test: `backend/tests/test_order_cancellations.py`

**Interfaces:**
- Produces: `CancellationRequest(request_id, order_id, buyer_id, status, reason, idempotency_key, created_at, decided_at)`
- Produces: `async request_cancellation(*, order_id, buyer_id, idempotency_key, reason: str | None = None) -> tuple[CancellationRequest, bool] | None`
- Produces: `async confirm_cancellation(*, order_id, buyer_id) -> tuple[Order, CancellationRequest, bool] | None`
- Produces: `async withdraw_cancellation(*, order_id, buyer_id) -> tuple[CancellationRequest, bool] | None`
- Produces: `async latest_cancellation(*, order_id) -> CancellationRequest | None`
- Produces: 异常 `OrderAlreadyCancelled`、`CancellationNotPending`、`NoCancellationRequest`。
- 事件名：`cancellation_requested`、`cancellation_superseded`、`cancellation_confirmed`、`cancellation_withdrawn`、`order_cancelled`。

- [ ] **Step 1: Write the failing tests**

```python
def test_request_keeps_order_created_then_confirm_cancels_it(tmp_path) -> None:
    async def scenario():
        store = SqliteOrderStore(tmp_path / "orders.sqlite3")
        draft, _ = await store.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="CNY", items=[_item()]
        )
        order, _ = await store.confirm_draft(draft_id=draft.draft_id, buyer_id="b1")
        request, idem = await store.request_cancellation(
            order_id=order.order_id, buyer_id="b1", idempotency_key="k1"
        )
        still = await store.get_order(order_id=order.order_id, buyer_id="b1")
        confirmed_order, confirmed_req, idem2 = await store.confirm_cancellation(
            order_id=order.order_id, buyer_id="b1"
        )
        return order, request, idem, still, confirmed_order, confirmed_req, idem2

    order, request, idem, still, confirmed_order, confirmed_req, idem2 = asyncio.run(scenario())
    assert request.status == "pending" and idem is False
    assert still.status == "created"
    assert confirmed_req.status == "confirmed"
    assert confirmed_order.status == "cancelled"
    assert idem2 is False


def test_idempotency_key_replays_after_order_is_cancelled(tmp_path) -> None:
    async def scenario():
        store = SqliteOrderStore(tmp_path / "orders.sqlite3")
        draft, _ = await store.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="CNY", items=[_item()]
        )
        order, _ = await store.confirm_draft(draft_id=draft.draft_id, buyer_id="b1")
        first, _ = await store.request_cancellation(
            order_id=order.order_id, buyer_id="b1", idempotency_key="k1"
        )
        await store.confirm_cancellation(order_id=order.order_id, buyer_id="b1")
        replay, idem = await store.request_cancellation(
            order_id=order.order_id, buyer_id="b1", idempotency_key="k1"
        )
        return first, replay, idem

    first, replay, idem = asyncio.run(scenario())
    assert idem is True
    assert replay.request_id == first.request_id
```

再加：`test_new_request_supersedes_old_pending`、`test_withdraw_keeps_order_created`、`test_confirm_after_withdraw_raises`、`test_request_on_cancelled_order_raises`、`test_confirm_cancellation_is_idempotent`。

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --directory backend pytest tests/test_order_cancellations.py -q`
Expected: FAIL — `AttributeError: 'SqliteOrderStore' object has no attribute 'request_cancellation'`.

- [ ] **Step 3: Implement the cancellation table and transitions**

把 `order_cancellation_requests` 加进 `SCHEMA`（`UNIQUE(buyer_id, idempotency_key)`，部分唯一索引 `ux_cancel_one_pending ... WHERE status='pending'`）。

`request_cancellation` 的顺序固定：**先**按 `(buyer_id, idempotency_key)` 查，命中直接返回 `(row, True)`，不看订单状态；未命中才按 `(id, buyer_id)` 查订单，订单 `cancelled` 抛 `OrderAlreadyCancelled`；旧 pending 置 `superseded` + 事件，再插新 pending（`can_` + 12 hex）+ 事件。

`confirm_cancellation` / `withdraw_cancellation` 都取**最新一条**请求（`ORDER BY created_at DESC, rowid DESC LIMIT 1`）。`confirm` 在 `request.status == "confirmed"` 或订单已 `cancelled` 时返回 `(order, request, True)`；`withdrawn/superseded` 抛 `CancellationNotPending`；pending 时同一事务里改请求 + 订单并写两个事件。`withdraw` 对已 `withdrawn` 返回 `(request, True)`。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --directory backend pytest tests/test_order_cancellations.py -q`
Expected: PASS.

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add backend/src/harbor/features/orders backend/tests/test_order_cancellations.py
git commit -m "feat(s10): add two-phase order cancellation storage"
```

---

### Task 4: 检索证据表 + 保留期

**Files:**
- Create: `backend/src/harbor/features/orders/evidence.py`
- Test: `backend/tests/test_evidence_store.py`

**Interfaces:**
- Consumes: `SearchEvidence`（Task 2）。
- Produces: `EvidenceRecord(buyer_id, shopping_session_id, sku_id, product_id, title, unit_price: Decimal, currency, observed_at)`
- Produces: `SqliteEvidenceStore(path: Path, *, retention_days: int = 30, clock: Callable[[], datetime] | None = None)`
  - `async record(*, buyer_id, shopping_session_id, records: Sequence[EvidenceRecord]) -> None`（同 SKU 覆盖）
  - `async find_many(*, buyer_id, shopping_session_id, sku_ids: Sequence[str]) -> dict[str, EvidenceRecord]`
  - `async prune() -> int`

- [ ] **Step 1: Write the failing tests**

```python
from datetime import UTC, datetime, timedelta

def test_latest_observation_wins(tmp_path) -> None:
    async def scenario():
        store = SqliteEvidenceStore(tmp_path / "orders.sqlite3")
        await store.record(buyer_id="b1", shopping_session_id="s1", records=[_record("89.00", 0)])
        await store.record(buyer_id="b1", shopping_session_id="s1", records=[_record("99.00", 1)])
        return await store.find_many(buyer_id="b1", shopping_session_id="s1", sku_ids=["P1-S1"])

    found = asyncio.run(scenario())["P1-S1"]
    assert found.unit_price == Decimal("99.00")


def test_evidence_is_session_scoped(tmp_path) -> None:
    async def scenario():
        store = SqliteEvidenceStore(tmp_path / "orders.sqlite3")
        await store.record(buyer_id="b1", shopping_session_id="s1", records=[_record("89.00", 0)])
        return await store.find_many(buyer_id="b1", shopping_session_id="s2", sku_ids=["P1-S1"])

    assert asyncio.run(scenario()) == {}


def test_prune_removes_only_expired_rows(tmp_path) -> None:
    now = [datetime(2026, 11, 5, tzinfo=UTC)]
    async def scenario():
        store = SqliteEvidenceStore(
            tmp_path / "orders.sqlite3", retention_days=30, clock=lambda: now[0]
        )
        await store.record(buyer_id="b1", shopping_session_id="s1", records=[_record("89.00", 0)])
        now[0] += timedelta(days=31)
        removed = await store.prune()
        return removed, await store.find_many(
            buyer_id="b1", shopping_session_id="s1", sku_ids=["P1-S1"]
        )

    removed, found = asyncio.run(scenario())
    assert removed == 1
    assert found == {}
```

`_record(price, age_days)` 的 `observed_at` 用固定基准 `datetime(2026, 11, 5, tzinfo=UTC) - timedelta(days=age_days)` 生成 ISO 字符串，保证与测试里的假时钟一致；不要用 `datetime.now()`。

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --directory backend pytest tests/test_evidence_store.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor.features.orders.evidence'`.

- [ ] **Step 3: Implement the evidence store**

表 `product_evidence` 主键 `(buyer_id, shopping_session_id, sku_id)`。`record` 用一条 `INSERT ... ON CONFLICT(...) DO UPDATE SET ...` 批量 upsert，在一个事务里完成。`prune` 删除 `observed_at < (clock() - retention_days).isoformat()`。

服务启动时清理一次（构造函数里 `_prune_sync`），之后每天第一次 `record` 时惰性清理一次：保存 `_last_prune_date: date | None`，比较 `clock().date()`，同一日期只清一次。所有写入走与 `SqliteMemoryStore` 相同的 `asyncio.Lock` + `BEGIN IMMEDIATE` 形状。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --directory backend pytest tests/test_evidence_store.py -q`
Expected: PASS.

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add backend/src/harbor/features/orders/evidence.py backend/tests/test_evidence_store.py
git commit -m "feat(s10): add durable product evidence with retention"
```

---

### Task 5: OrderService 草稿创建

**Files:**
- Create: `backend/src/harbor/features/orders/service.py`
- Test: `backend/tests/test_order_service.py`

**Interfaces:**
- Consumes: Task 1 `quantize_major`；Task 2 `SqliteOrderStore`、`SearchEvidence`；Task 4 `SqliteEvidenceStore`；`fx.convert`。
- Produces:
  - `DraftRequestItem(product_id: str, sku_id: str, quantity: int = 1)`
  - `DraftCreation(draft: OrderDraft, replayed: bool)`
  - `OrderValidationError(code: str, message: str, line: int | None = None)`
  - `OrderService(store: SqliteOrderStore, evidence: SqliteEvidenceStore)`
  - `async create_draft(*, buyer_id, shopping_session_id, items: Sequence[DraftRequestItem], currency: str = "CNY", context_evidence: Sequence[SearchEvidence] = (), appendix_evidence: Sequence[SearchEvidence] = ()) -> DraftCreation`
  - `async get_pending_draft(*, buyer_id, shopping_session_id) -> OrderDraft | None`
  - `async record_search_hits(*, buyer_id, shopping_session_id, evidence: Sequence[SearchEvidence]) -> None`
- 校验错误码（机器可读）：`missing_identity`、`empty_items`、`too_many_items`（>20）、`invalid_item`（缺 product_id/sku_id）、`invalid_quantity`（不在 1–99）、`duplicate_sku`、`invalid_currency`（不在 `fx.CNY_PER_UNIT`）、`evidence_missing`、`evidence_mismatch`。

- [ ] **Step 1: Write the failing tests**

```python
def _service(tmp_path) -> OrderService:
    path = tmp_path / "orders.sqlite3"
    return OrderService(SqliteOrderStore(path), SqliteEvidenceStore(path))


def _evidence(price="89.00", currency="CNY", product_id="P1", sku="P1-S1"):
    return SearchEvidence(
        product_id=product_id, sku_id=sku, title="猫饮水器",
        unit_price=Decimal(price), currency=currency,
    )


def test_draft_is_priced_from_evidence_not_from_model(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        return await service.create_draft(
            buyer_id="b1", shopping_session_id="s1",
            items=[DraftRequestItem("P1", "P1-S1", 2)],
            context_evidence=[_evidence()],
        )

    creation = asyncio.run(scenario())
    assert creation.replayed is False
    assert creation.draft.total == Decimal("178.00")
    assert creation.draft.items[0].unit_price == Decimal("89.00")
    assert creation.draft.items[0].evidence_currency == "CNY"


def test_jpy_draft_has_no_fractional_yen(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        return await service.create_draft(
            buyer_id="b1", shopping_session_id="s1", currency="JPY",
            items=[DraftRequestItem("P1", "P1-S1", 2)],
            context_evidence=[_evidence("7.20", "CNY")],
        )

    creation = asyncio.run(scenario())
    assert creation.draft.items[0].unit_price == Decimal("150")
    assert creation.draft.total == Decimal("300")


def test_draft_uses_durable_evidence_when_context_is_empty(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        await service.record_search_hits(
            buyer_id="b1", shopping_session_id="s1", evidence=[_evidence()]
        )
        return await service.create_draft(
            buyer_id="b1", shopping_session_id="s1",
            items=[DraftRequestItem("P1", "P1-S1", 1)], context_evidence=[],
        )

    assert asyncio.run(scenario()).draft.total == Decimal("89.00")


def test_identical_draft_is_replayed(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        kwargs = dict(
            buyer_id="b1", shopping_session_id="s1",
            items=[DraftRequestItem("P1", "P1-S1", 1)],
            context_evidence=[_evidence()],
        )
        first = await service.create_draft(**kwargs)
        second = await service.create_draft(**kwargs)
        return first, second

    first, second = asyncio.run(scenario())
    assert second.replayed is True
    assert second.draft.draft_id == first.draft.draft_id
```

再加参数化校验测试：`@pytest.mark.parametrize` 覆盖空 items、21 行、数量 0/100、重复 SKU、未知币种、无证据、`product_id` 与证据不符；每条断言 `exc.value.code`。附录证据单独一条：

```python
def test_appendix_evidence_without_product_id_is_accepted(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        return await service.create_draft(
            buyer_id="b1", shopping_session_id="s1",
            items=[DraftRequestItem("P1", "P1-S1", 1)],
            appendix_evidence=[_evidence(product_id=None)],
        )

    assert asyncio.run(scenario()).draft.total == Decimal("89.00")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --directory backend pytest tests/test_order_service.py -q`
Expected: FAIL — `ImportError: cannot import name 'OrderService'`.

- [ ] **Step 3: Implement draft creation**

按顺序校验：身份 → 币种 → 行数 → 每行字段/数量/重复 SKU → 证据解析。证据来源顺序固定为 **证据表 → `context_evidence` → `appendix_evidence`**；同一 SKU 只取第一个命中的来源。`context_evidence` 里的 `product_id` 必须等于行项目；`appendix_evidence` 的 `product_id is None` 时不校验。命中价用 `fx.convert(evidence.unit_price, evidence.currency, currency)` 换算，再 `quantize_major(..., currency)`；`evidence_unit_price` / `evidence_currency` 原样存证据值。总额交给 store 计算。

`record_search_hits` 把 `SearchEvidence` 转成 `EvidenceRecord`（`observed_at` 用 `datetime.now(UTC).isoformat()`）写证据表。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --directory backend pytest tests/test_order_service.py -q`
Expected: PASS.

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add backend/src/harbor/features/orders/service.py backend/tests/test_order_service.py
git commit -m "feat(s10): add order draft creation with evidence pricing"
```

---

### Task 6: OrderService 应用层动作

**Files:**
- Modify: `backend/src/harbor/features/orders/service.py`
- Test: `backend/tests/test_order_service_actions.py`

**Interfaces:**
- Produces: `OrderServiceError`、`OrderNotFound`、`OrderConflict`（带 `.current_status`）、`OrderStorageError`。
- Produces:
  - `async confirm_draft(*, draft_id, buyer_id) -> tuple[Order, bool]`
  - `async cancel_draft(*, draft_id, buyer_id) -> tuple[OrderDraft, bool]`
  - `async get_order(*, order_id, buyer_id) -> tuple[Order, CancellationRequest | None]`
  - `async request_cancellation(*, order_id, buyer_id, idempotency_key, reason: str | None = None) -> tuple[CancellationRequest, bool]`
  - `async confirm_cancellation(*, order_id, buyer_id) -> tuple[Order, CancellationRequest, bool]`
  - `async withdraw_cancellation(*, order_id, buyer_id) -> tuple[CancellationRequest, bool]`
- 映射：store 返回 `None` → `OrderNotFound`；`DraftNotPending` / `CancellationNotPending` / `OrderAlreadyCancelled` / `NoCancellationRequest` → `OrderConflict`；其他 `OrderStoreError` → `OrderStorageError`。

- [ ] **Step 1: Write the failing tests**

```python
def test_other_buyer_cannot_see_or_confirm_draft(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        draft = (await service.create_draft(
            buyer_id="b1", shopping_session_id="s1",
            items=[DraftRequestItem("P1", "P1-S1", 1)],
            context_evidence=[_evidence()],
        )).draft
        with pytest.raises(OrderNotFound):
            await service.confirm_draft(draft_id=draft.draft_id, buyer_id="b2")
        with pytest.raises(OrderNotFound):
            await service.get_order(order_id="ord_missing", buyer_id="b2")
        return draft

    asyncio.run(scenario())


def test_conflict_reports_current_status(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        old = (await service.create_draft(
            buyer_id="b1", shopping_session_id="s1",
            items=[DraftRequestItem("P1", "P1-S1", 1)],
            context_evidence=[_evidence()],
        )).draft
        await service.create_draft(
            buyer_id="b1", shopping_session_id="s1",
            items=[DraftRequestItem("P1", "P1-S1", 2)],
            context_evidence=[_evidence()],
        )
        return old

    old = asyncio.run(scenario())
    with pytest.raises(OrderConflict) as excinfo:
        asyncio.run(_service(tmp_path).confirm_draft(draft_id=old.draft_id, buyer_id="b1"))
    assert excinfo.value.current_status == "superseded"
```

再加：`test_cancel_draft_is_idempotent`、`test_cancellation_request_then_confirm_and_repeat`、`test_withdraw_then_order_remains_created`、`test_request_on_cancelled_order_conflicts`、`test_get_order_returns_latest_cancellation`。

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --directory backend pytest tests/test_order_service_actions.py -q`
Expected: FAIL — `ImportError: cannot import name 'OrderNotFound'`.

- [ ] **Step 3: Implement the service actions**

每个方法都是一层薄翻译：调用 store → 把 `None` 和状态异常翻成服务层异常。不要在服务层重新实现状态机。`confirm_cancellation` / `withdraw_cancellation` 的“最新请求”语义由 store 保证。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --directory backend pytest tests/test_order_service_actions.py -q`
Expected: PASS.

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add backend/src/harbor/features/orders/service.py backend/tests/test_order_service_actions.py
git commit -m "feat(s10): add application-layer order actions"
```

---

### Task 7: `product_search` 记录证据（best-effort）

**Files:**
- Modify: `backend/src/harbor/agents/tools.py`
- Test: `backend/tests/test_product_search_evidence.py`

**Interfaces:**
- Consumes: `SearchEvidence`（Task 2）、`IDENTITY_STATE_KEY`。
- Produces: `make_product_search_tool(..., evidence_recorder: EvidenceRecorder | None = None)`，其中
  `EvidenceRecorder = Callable[..., Awaitable[None]]`，调用形状为
  `await evidence_recorder(buyer_id=..., shopping_session_id=..., evidence=[SearchEvidence, ...])`。
- 证据值来自 `_search_result_to_dict` 的 `hits`（`price_major` / `currency` 已经是检索目标币种）。

- [ ] **Step 1: Write the failing test**

测试文件复制 `tests/test_product_search_tool.py` 里的 `_product(pid, title)` 辅助函数，并导入 `SearchHit`。

```python
class _OneHitService:
    def __init__(self, product: Product) -> None:
        self.product = product

    def search(self, query, spec, limit=10):
        return SearchResult(
            hits=[SearchHit(product=self.product, score=1.0, selected_sku=self.product.skus[0])],
            total=1,
        )


def test_search_records_hits_for_the_session() -> None:
    calls = []

    async def recorder(**kwargs):
        calls.append(kwargs)

    state = AgentState()
    state.middle_context[IDENTITY_STATE_KEY] = {"buyer_id": "b1", "shopping_session_id": "s1"}
    tool = make_product_search_tool(_OneHitService(_product("P1", "猫饮水器")), evidence_recorder=recorder)

    asyncio.run(tool.call(_agent_state=state, query="饮水器"))

    assert calls[0]["buyer_id"] == "b1"
    assert calls[0]["shopping_session_id"] == "s1"
    assert calls[0]["evidence"][0].sku_id == "P1-S1"
    assert calls[0]["evidence"][0].currency == "CNY"


def test_search_without_identity_does_not_record() -> None:
    calls = []

    async def recorder(**kwargs):
        calls.append(kwargs)

    tool = make_product_search_tool(
        _OneHitService(_product("P1", "猫饮水器")), evidence_recorder=recorder
    )
    asyncio.run(tool.call(_agent_state=AgentState(), query="饮水器"))
    assert calls == []


def test_evidence_recording_failure_does_not_fail_search() -> None:
    async def recorder(**kwargs):
        raise RuntimeError("disk full")

    state = AgentState()
    state.middle_context[IDENTITY_STATE_KEY] = {"buyer_id": "b1", "shopping_session_id": "s1"}
    tool = make_product_search_tool(_OneHitService(_product("P1", "猫饮水器")), evidence_recorder=recorder)

    chunk = asyncio.run(tool.call(_agent_state=state, query="饮水器"))

    assert chunk.state == ToolResultState.SUCCESS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/test_product_search_evidence.py -q`
Expected: FAIL — `TypeError: make_product_search_tool() got an unexpected keyword argument 'evidence_recorder'`.

- [ ] **Step 3: Implement the optional recorder**

在 `product_search` 里先算出 `payload`，再：

```python
if evidence_recorder is not None and _agent_state is not None:
    identity = _agent_state.middle_context.get(IDENTITY_STATE_KEY)
    if isinstance(identity, dict) and identity.get("buyer_id") and identity.get("shopping_session_id"):
        evidence = [... from payload["hits"] ...]
        try:
            await evidence_recorder(
                buyer_id=..., shopping_session_id=..., evidence=evidence
            )
        except Exception:  # noqa: BLE001 — evidence is best-effort
            logger.warning("failed to record product evidence", exc_info=True)
```

`evidence` 里 `unit_price=Decimal(str(hit["price_major"]))`、`currency=hit["currency"]`、`product_id=hit["product_id"]`、`sku_id=hit["selected_sku_id"]`、`title=hit["title"]`。不要让 recorder 的异常冒泡。

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/test_product_search_evidence.py tests/test_product_search_tool.py -q`
Expected: PASS.

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add backend/src/harbor/agents/tools.py backend/tests/test_product_search_evidence.py
git commit -m "feat(s10): record search hits as durable evidence"
```

---

### Task 8: 模型交易工具 `create_order_draft` / `get_order`

**Files:**
- Create: `backend/src/harbor/agents/order_tools.py`
- Modify: `backend/src/harbor/agents/grounding.py`
- Test: `backend/tests/test_order_tools.py`

**Interfaces:**
- Consumes: Task 5/6 的 `OrderService` 方法；`facts_from_appendix`、`PROTECTED_FACTS_HEADER`。
- Produces: `make_order_tools(service: OrderService) -> list[FunctionTool]`（顺序固定 `[create_order_draft, get_order]`）。
- Produces（`grounding.py`）：`search_evidence_from_context(context: Sequence[Msg]) -> list[SearchEvidence]`，只读 SUCCESS 的 `product_search` 回执，同一 SKU 后出现的观测覆盖先出现的。
- 两个工具都用 `is_state_injected=True`；身份只从 `state.middle_context[IDENTITY_STATE_KEY]` 读，不接受模型参数。

- [ ] **Step 1: Write the failing tests**

```python
def _state(buyer="b1", session="s1") -> AgentState:
    state = AgentState()
    state.middle_context[IDENTITY_STATE_KEY] = {
        "buyer_id": buyer, "shopping_session_id": session
    }
    return state


def _state_with_search_hit(state: AgentState, price: float = 89.0) -> AgentState:
    state.context.append(
        Msg(
            name="purchase_agent", role="assistant",
            content=[
                ToolResultBlock(
                    id="call-1", name="product_search",
                    output=[TextBlock(type="text", text=json.dumps({"hits": [{
                        "product_id": "P1", "title": "猫饮水器",
                        "selected_sku_id": "P1-S1", "price_major": price,
                        "currency": "CNY",
                    }]}))],
                    state=ToolResultState.SUCCESS,
                )
            ],
        )
    )
    return state


def test_draft_tool_creates_a_draft_but_no_order(tmp_path) -> None:
    service = _service(tmp_path)
    tool = make_order_tools(service)[0]

    chunk = asyncio.run(tool.call(
        _agent_state=_state_with_search_hit(_state()),
        items=[{"product_id": "P1", "sku_id": "P1-S1", "quantity": 2}],
        currency="CNY",
    ))
    payload = json.loads(chunk.content[0].text)

    assert chunk.state == ToolResultState.SUCCESS
    assert payload["ok"] is True and payload["kind"] == "order_draft"
    assert payload["draft"]["total"] == "178.00"


def test_tool_call_with_no_arguments_is_a_validation_result(tmp_path) -> None:
    tool = make_order_tools(_service(tmp_path))[0]
    chunk = asyncio.run(tool.call(_agent_state=_state()))
    payload = json.loads(chunk.content[0].text)

    assert chunk.state == ToolResultState.SUCCESS
    assert payload["ok"] is False
    assert payload["error"] == "empty_items"


@pytest.mark.parametrize(
    "items, code",
    [
        ([{"product_id": "P1", "sku_id": "P1-S1", "quantity": 0}], "invalid_quantity"),
        ([{"product_id": "P1", "sku_id": "P1-S1"}, {"product_id": "P1", "sku_id": "P1-S1"}], "duplicate_sku"),
        ([{"product_id": "P9", "sku_id": "P9-S1"}], "evidence_missing"),
    ],
)
def test_bad_items_are_ok_false_not_error(tmp_path, items, code) -> None:
    tool = make_order_tools(_service(tmp_path))[0]
    chunk = asyncio.run(tool.call(_agent_state=_state_with_search_hit(_state()), items=items))
    assert chunk.state == ToolResultState.SUCCESS
    assert json.loads(chunk.content[0].text)["error"] == code


def test_get_order_hides_other_buyers_order(tmp_path) -> None:
    service = _service(tmp_path)
    draft = asyncio.run(service.create_draft(
        buyer_id="b1", shopping_session_id="s1",
        items=[DraftRequestItem("P1", "P1-S1", 1)],
        context_evidence=[_evidence()],
    )).draft
    order, _ = asyncio.run(service.confirm_draft(draft_id=draft.draft_id, buyer_id="b1"))

    chunk = asyncio.run(make_order_tools(service)[1].call(
        _agent_state=_state(buyer="b2"), order_id=order.order_id
    ))
    payload = json.loads(chunk.content[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "not_found"
```

再加：`test_missing_identity_is_ok_false`、`test_tool_reads_appendix_evidence`、`test_get_order_returns_latest_cancellation`、`test_missing_order_id_is_ok_false`。

测试文件复用 Task 5 测试里的 `_service(tmp_path)` 与 `_evidence()` 辅助函数；`_state_with_search_hit` 里的回执形状与 `tests/test_card_grounding.py` 的 `_result` 相同。

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --directory backend pytest tests/test_order_tools.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor.agents.order_tools'`.

- [ ] **Step 3: Implement the tools**

Pydantic 参数模型全部可空：`DraftItemArgs.product_id/sku_id/quantity` 默认 `None`，`CreateOrderDraftArgs.items: list[DraftItemArgs] | None = None`，`currency: str | None = None`，`GetOrderArgs.order_id: str | None = None`。函数签名也用 `| None = None`，确保框架层永远不报校验错。

`FunctionTool.call` 在测试/部分调用路径里可能把 `items` 直接传成 `list[dict]`，所以函数体先归一化：

```python
parsed = [item if isinstance(item, DraftItemArgs) else DraftItemArgs.model_validate(item)
          for item in (items or [])]
```

`create_order_draft` 流程：读身份（缺失 → `ok=false missing_identity`）→ `search_evidence_from_context(state.context)` → 扫描 `state.context` 里最后一个含 `PROTECTED_FACTS_HEADER` 的文本块，用 `facts_from_appendix` 得到附录证据（`product_id=None`）→ 调 `service.create_draft` → 组装回执。`OrderValidationError` 转 `{"ok": False, "kind": "order_draft", "error": code, "message": message, "line": line?}`；`OrderStorageError` 返回 `ToolResultState.ERROR`。

`get_order` 流程：缺 `order_id` → `ok=false invalid_argument`；`OrderNotFound` → `ok=false not_found`；成功返回订单字段 + `cancellation`。金额用 `str(Decimal)` 序列化（保持 `"178.00"`）。

回执形状固定：

```json
{"ok": true, "kind": "order_draft", "replayed": false,
 "draft": {"draft_id": "drf_...", "status": "pending", "currency": "CNY",
           "total": "178.00",
           "items": [{"product_id": "P1", "sku_id": "P1-S1", "title": "猫饮水器",
                      "quantity": 2, "unit_price": "89.00", "currency": "CNY"}]}}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --directory backend pytest tests/test_order_tools.py -q`
Expected: PASS.

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add backend/src/harbor/agents/order_tools.py backend/src/harbor/agents/grounding.py backend/tests/test_order_tools.py
git commit -m "feat(s10): expose draft and order tools to the model"
```

---

### Task 9: 交易 prompt

**Files:**
- Modify: `backend/src/harbor/agents/prompts.py`
- Test: `backend/tests/test_order_prompt.py`

**Interfaces:**
- Produces: `ORDER_GUIDANCE`；`system_prompt_for(..., orders_enabled: bool = False)`。
- `DISPATCH_GUIDANCE` 里的“订单/查单能力尚未接入”改为占位符 `{transaction_note}`，由 `system_prompt_for` 按 `orders_enabled` 替换。

- [ ] **Step 1: Write the failing tests**

```python
from harbor.agents.prompts import system_prompt_for


def test_order_guidance_only_when_enabled() -> None:
    assert "create_order_draft" not in system_prompt_for(["母婴宠物"])
    assert "create_order_draft" in system_prompt_for(["母婴宠物"], orders_enabled=True)


def test_order_guidance_forbids_claiming_an_order_was_created() -> None:
    prompt = system_prompt_for(["母婴宠物"], orders_enabled=True)
    assert "你没有确认能力" in prompt
    assert "已下单" in prompt


def test_dispatch_guidance_stops_saying_orders_are_unwired() -> None:
    prompt = system_prompt_for(
        ["母婴宠物"], dispatch_enabled=True, orders_enabled=True
    )
    assert "尚未接入" not in prompt
    assert "子 Agent 没有买家身份" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/test_order_prompt.py -q`
Expected: FAIL — `TypeError: system_prompt_for() got an unexpected keyword argument 'orders_enabled'`.

- [ ] **Step 3: Implement the prompt block**

`ORDER_GUIDANCE` 必须包含：仅在明确购买意图时建草稿；草稿行只来自本会话 `product_search` 命中；数量默认 1；草稿不是订单，模型没有确认能力，禁止说「已下单」「订单已创建」；`ok=false` 时解释原因；取消引导到应用界面；仍遵守「summary 只描述本轮」。仅 `orders_enabled=True` 时追加。替换 dispatch 占位符：开启交易时写“子 Agent 没有买家身份，不能下单；交易任务不要派。”，否则保留原文。

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/test_order_prompt.py tests/test_prompts.py tests/test_dispatch_prompts.py -q`
Expected: PASS.

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add backend/src/harbor/agents/prompts.py backend/tests/test_order_prompt.py
git commit -m "feat(s10): add order drafting prompt guidance"
```

---

### Task 10: HTTP 视图与 `/commerce/orders` 路由

**Files:**
- Create: `backend/src/harbor/features/orders/schemas.py`
- Create: `backend/src/harbor/features/orders/router.py`
- Test: `backend/tests/test_orders_router.py`

**Interfaces:**
- Produces（`schemas.py`）：
  - `OrderItemView`、`OrderDraftView.from_draft(draft)`、`OrderView.from_order(order)`、`OrderDetailView`、`CancellationView.from_request(request)`
  - `BuyerRequest(buyer_id)`、`CancellationCreateRequest(buyer_id, reason=None, idempotency_key)`
  - `ConfirmDraftResponse(order, idempotent)`、`CancelDraftResponse(draft)`、`RequestCancellationResponse(cancellation, idempotent)`、`ConfirmCancellationResponse(order, cancellation, idempotent)`、`WithdrawCancellationResponse(cancellation)`
- Produces: `router = APIRouter(prefix="/orders", tags=["orders"])`；服务从 `request.app.state.order_service` 取，缺失返回 503。
- 错误映射：`OrderNotFound` → 404；`OrderConflict` → 409；`OrderStorageError` / 其他 `OrderServiceError` → 500。

- [ ] **Step 1: Write the failing tests**

```python
from fastapi import FastAPI
from fastapi.testclient import TestClient

def _client(tmp_path) -> tuple[TestClient, OrderService]:
    service = _service(tmp_path)
    app = FastAPI()
    app.state.order_service = service
    app.include_router(router, prefix="/commerce")
    return TestClient(app), service


def test_confirm_route_returns_order_and_conflicts_on_superseded(tmp_path) -> None:
    client, service = _client(tmp_path)
    old = asyncio.run(service.create_draft(
        buyer_id="b1", shopping_session_id="s1",
        items=[DraftRequestItem("P1", "P1-S1", 1)], context_evidence=[_evidence()],
    )).draft
    asyncio.run(service.create_draft(
        buyer_id="b1", shopping_session_id="s1",
        items=[DraftRequestItem("P1", "P1-S1", 2)], context_evidence=[_evidence()],
    ))

    response = client.post(f"/commerce/orders/drafts/{old.draft_id}/confirm", json={"buyer_id": "b1"})

    assert response.status_code == 409
    assert response.json()["detail"]["current_status"] == "superseded"


def test_pending_draft_route_survives_a_page_reload(tmp_path) -> None:
    client, service = _client(tmp_path)
    draft = asyncio.run(service.create_draft(
        buyer_id="b1", shopping_session_id="s1",
        items=[DraftRequestItem("P1", "P1-S1", 1)], context_evidence=[_evidence()],
    )).draft

    response = client.get(
        "/commerce/orders/drafts",
        params={"buyer_id": "b1", "shopping_session_id": "s1"},
    )

    assert response.status_code == 200
    assert response.json()["draft_id"] == draft.draft_id
    assert response.json()["total"] == "89.00"


def test_missing_service_returns_503(tmp_path) -> None:
    app = FastAPI()
    app.include_router(router, prefix="/commerce")
    response = TestClient(app).get(
        "/commerce/orders/drafts", params={"buyer_id": "b1", "shopping_session_id": "s1"}
    )
    assert response.status_code == 503
```

再加：确认 200 + `idempotent`、非本人 404、取消请求/确认/撤回 200、`GET /orders/{id}` 返回 `cancellation`、404/409 映射。

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --directory backend pytest tests/test_orders_router.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor.features.orders.router'`.

- [ ] **Step 3: Implement schemas + router**

路由声明顺序：`/drafts` 在前，`/drafts/{draft_id}/...` 次之，`/{order_id}` 最后，避免路径吞并。每个端点用 `try/except` 翻译服务异常；409 的 `detail` 用 `{"message": str(exc), "current_status": exc.current_status}`。`Decimal` 字段让 Pydantic 序列化为字符串（测试断言 `"89.00"`）。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --directory backend pytest tests/test_orders_router.py -q`
Expected: PASS.

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add backend/src/harbor/features/orders/schemas.py backend/src/harbor/features/orders/router.py backend/tests/test_orders_router.py
git commit -m "feat(s10): expose application-layer order endpoints"
```

---

### Task 11: 装配（settings、身份、factory、intents、main）

**Files:**
- Modify: `backend/src/harbor/core/config.py`
- Modify: `backend/src/harbor/main.py`
- Modify: `backend/src/harbor/features/chat/schemas.py`
- Modify: `backend/src/harbor/features/chat/service.py`
- Modify: `backend/src/harbor/features/chat/router.py`
- Modify: `backend/src/harbor/agents/factory.py`
- Modify: `backend/src/harbor/agents/permissions.py`
- Test: `backend/tests/test_order_wiring.py`

**Interfaces:**
- Settings 新增：`transaction_enabled: bool = True`、`orders_db_path: Path = data/sessions/orders.sqlite3`、`evidence_retention_days: int = Field(default=30, ge=1)`。
- `SessionStore.get_or_create` 注入 `{"buyer_id": ..., "shopping_session_id": ...}`。
- `build_agent(runtime, state=None, *, buyer_id=None, shopping_session_id=None, order_service=None)`；`orders_enabled = order_service is not None and settings.transaction_enabled`。
- `make_product_search_tool(..., evidence_recorder=order_service.record_search_hits if orders_enabled else None)`。
- `run_turn(..., order_service=None)`；完成后按 `(buyer, session)` 填 `CommerceIntentResponse.pending_draft`。
- `CommerceIntentResponse.pending_draft: OrderDraftView | None = None`。
- `main.lifespan` 无论模型是否配置都创建 `OrderService` 并放到 `app.state.order_service`。

- [ ] **Step 1: Write the failing tests**

```python
def test_session_identity_carries_both_ids() -> None:
    seen = {}

    def factory(state):
        seen["state"] = state
        return _FakeAgent()

    store = SessionStore(agent_factory=factory)
    asyncio.run(store.get_or_create("b1", "s1"))
    assert seen["state"].middle_context[IDENTITY_STATE_KEY] == {
        "buyer_id": "b1", "shopping_session_id": "s1"
    }


def test_build_agent_registers_order_tools_only_with_a_service(monkeypatch, tmp_path) -> None:
    runtime = _offline_runtime(monkeypatch, tmp_path)
    service = _service(tmp_path)

    with_service = build_agent(runtime, order_service=service)
    without = build_agent(runtime)

    names = {tool.name for tool in with_service.toolkit.tool_groups[0].tools}
    assert {"create_order_draft", "get_order"} <= names
    assert "create_order_draft" not in {t.name for t in without.toolkit.tool_groups[0].tools}


def test_run_turn_returns_the_draft_from_the_database(tmp_path) -> None:
    service = _service(tmp_path)
    asyncio.run(service.create_draft(
        buyer_id="b1", shopping_session_id="s1",
        items=[DraftRequestItem("P1", "P1-S1", 1)], context_evidence=[_evidence()],
    ))
    store = SessionStore(agent_factory=lambda state: _FakeAgentWithCards())

    response = asyncio.run(run_turn(store, "b1", "s1", "随便聊聊", order_service=service))

    assert response.pending_draft is not None
    assert response.pending_draft.draft_id.startswith("drf_")
```

`_offline_runtime` 复用 `test_factory.py` 的 monkeypatch 形状（fake embedder、`FastembedReranker` 为 `None`、`QdrantClient(":memory:")`、临时 fingerprint）。测试里的两个 stub：

```python
class _FakeAgent:
    def __init__(self) -> None:
        self.state = AgentState()


class _FakeAgentWithCards:
    def __init__(self) -> None:
        self.state = AgentState()

    async def reply(self, msg, structured_schema=None):
        self.state.context.append(msg)
        reply = Msg(
            name="purchase_agent", role="assistant",
            content=[TextBlock(type="text", text="好的")],
            structured_output={"summary": "好的", "cards": []},
        )
        self.state.context.append(reply)
        return reply
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --directory backend pytest tests/test_order_wiring.py -q`
Expected: FAIL — `TypeError: build_agent() got an unexpected keyword argument 'order_service'`.

- [ ] **Step 3: Implement the wiring**

`build_agent` 同时解析 `buyer_id` 与 `shopping_session_id`（显式参数优先，其次 `IDENTITY_STATE_KEY`）；两者都有时写回 `state.middle_context`，必要时创建 `AgentState()`。evidence recorder 只在 `orders_enabled` 时传入主 Agent 的搜索工具；S9 子 Agent 不传。工具列表在 legacy / governed 两个分支都追加 `make_order_tools(order_service)` 并调用 `allow_order_tools(agent)`。`main.py` 用同一 `orders_db_path` 构造两个 store，`SessionStore` 工厂捕获 `order_service`。`chat/router.py` 把 `http_request.app.state.order_service` 传给 `run_turn`。

`run_turn` 在 `persist` 之前查 `get_pending_draft`，用 `CommerceIntentResponse.model_copy(update={"pending_draft": ...})` 返回；模型文本完全不参与。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --directory backend pytest tests/test_order_wiring.py tests/test_factory.py tests/test_chat_service.py tests/test_chat_router.py -q`
Expected: PASS（现有 S5–S9 测试不回归）。

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add backend/src/harbor/core/config.py backend/src/harbor/main.py backend/src/harbor/features/chat backend/src/harbor/agents/factory.py backend/src/harbor/agents/permissions.py backend/tests/test_order_wiring.py
git commit -m "feat(s10): wire transaction service into chat and agent"
```

---

### Task 12: CLI 交易命令

**Files:**
- Modify: `backend/src/harbor/agents/cli.py`
- Test: `backend/tests/test_cli_orders.py`

**Interfaces:**
- Produces: `async handle_command(text: str, service: OrderService | None, *, buyer_id: str, shopping_session_id: str) -> str | None` — 不是命令返回 `None`，是命令返回要打印的文本。
- 命令：`/draft`、`/confirm`、`/cancel-draft`、`/order`、`/cancel-order`、`/confirm-cancel`、`/withdraw-cancel`。
- CLI 身份固定 `buyer_id="cli"`、`shopping_session_id="cli"`。

- [ ] **Step 1: Write the failing test**

```python
def test_confirm_command_reports_the_order(tmp_path) -> None:
    service = _service(tmp_path)
    draft = asyncio.run(service.create_draft(
        buyer_id="cli", shopping_session_id="cli",
        items=[DraftRequestItem("P1", "P1-S1", 1)], context_evidence=[_evidence()],
    )).draft

    output = asyncio.run(handle_command(
        f"/confirm {draft.draft_id}", service,
        buyer_id="cli", shopping_session_id="cli",
    ))

    assert "订单" in output
    assert "ord_" in output


def test_non_command_text_is_left_to_the_model() -> None:
    assert asyncio.run(handle_command("买个饮水器", None, buyer_id="cli", shopping_session_id="cli")) is None
```

再加：`/draft` 空时提示、未知命令返回用法提示（仍返回字符串）、服务未配置时提示。

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory backend pytest tests/test_cli_orders.py -q`
Expected: FAIL — `ImportError: cannot import name 'handle_command'`.

- [ ] **Step 3: Implement the commands**

`main()` 构造 `OrderService` 并 `build_agent(runtime, buyer_id="cli", shopping_session_id="cli", order_service=service)`。`run_repl` 每轮先 `handle_command`，返回非 `None` 就打印并 `continue`；模型回合结束后查 `get_pending_draft`，有就打印草稿摘要和 `/confirm <draft_id>`。`/cancel-order` 的幂等键用 `uuid.uuid4().hex`。

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory backend pytest tests/test_cli_orders.py -q`
Expected: PASS.

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add backend/src/harbor/agents/cli.py backend/tests/test_cli_orders.py
git commit -m "feat(s10): add order commands to the CLI"
```

---

### Task 13: 验收清单 + 全量验证

**Files:**
- Test: `backend/tests/test_s10_acceptance.py`
- Modify: nothing（只加验收测试）

**Interfaces:**
- Consumes: 前 12 个任务的全部公开接口。
- 验收清单映射（spec §9）：
  - 1 → `test_intents_exposes_pending_draft_without_creating_order`
  - 2 → `test_confirm_creates_one_order_and_repeat_is_idempotent`
  - 3/4 → Task 2/6 的冲突与跨买家测试
  - 5 → `test_cancelling_a_draft_creates_no_order`
  - 6/7 → Task 3 的取消链测试
  - 8 → Task 8 的 `ok=false` 测试
  - 9 → Task 9 的 prompt 测试
  - 10 → Task 2/5 的 replay 测试
  - 11 → Task 10/11 的 `pending_draft` 测试
  - 12 → Task 5 的总额/JPY 测试
  - 13/14 → Task 4/5 的证据测试
  - 15 → 本任务的全量命令

- [ ] **Step 1: Write the acceptance tests**

```python
import sqlite3


def test_intents_exposes_pending_draft_without_creating_order(tmp_path) -> None:
    # 假 Agent 的回复里完全不提草稿；pending_draft 必须来自数据库。
    service = _service(tmp_path)
    asyncio.run(service.create_draft(
        buyer_id="b1", shopping_session_id="s1",
        items=[DraftRequestItem("P1", "P1-S1", 1)], context_evidence=[_evidence()],
    ))
    store = SessionStore(agent_factory=lambda state: _FakeAgentWithCards())

    response = asyncio.run(run_turn(store, "b1", "s1", "谢谢", order_service=service))

    assert response.pending_draft.total == Decimal("89.00")
    with sqlite3.connect(tmp_path / "orders.sqlite3") as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_full_two_phase_flow(tmp_path) -> None:
    async def scenario():
        service = _service(tmp_path)
        draft = (await service.create_draft(
            buyer_id="b1", shopping_session_id="s1",
            items=[DraftRequestItem("P1", "P1-S1", 2)], context_evidence=[_evidence()],
        )).draft
        order, _ = await service.confirm_draft(draft_id=draft.draft_id, buyer_id="b1")
        request, _ = await service.request_cancellation(
            order_id=order.order_id, buyer_id="b1", idempotency_key="acceptance-1"
        )
        before = await service.get_order(order_id=order.order_id, buyer_id="b1")
        cancelled, request2, _ = await service.confirm_cancellation(
            order_id=order.order_id, buyer_id="b1"
        )
        return draft, order, request, before, cancelled, request2

    draft, order, request, before, cancelled, request2 = asyncio.run(scenario())
    assert draft.status == "confirmed"
    assert order.total == Decimal("178.00")
    assert before[0].status == "created" and before[1].status == "pending"
    assert cancelled.status == "cancelled" and request2.status == "confirmed"
```

上面第一条测试直接用 `sqlite3` 断言 `orders` 表为空；两条测试都复用 Task 5 的 `_service` / `_evidence`；`_FakeAgentWithCards` 从 Task 11 的测试文件复制过来（测试目录不是包，不要跨文件 import）。

- [ ] **Step 2: Run acceptance tests**

Run: `uv run --directory backend pytest tests/test_s10_acceptance.py -q`
Expected: PASS.

- [ ] **Step 3: Run the full suite**

Run: `make test`
Expected: 全部 PASS；没有任何测试访问网络（测试用 fake embedder / `QdrantClient(":memory:")`）。

- [ ] **Step 4: Lint and typecheck**

Run: `make lint && make typecheck`
Expected: `ruff check` 与 `ruff format --check` 无输出；`mypy src` 无错误。

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add backend/tests/test_s10_acceptance.py
git commit -m "test(s10): cover the transaction-loop acceptance list"
```

---

## 计划自检

- **Spec 覆盖**：§3 三条链 → Task 2/3/6；§4 数据模型 → Task 2/3/4；§5 契约 → Task 5/6/8；§6 接口 → Task 10/11/12；§7 Agent 侧 → Task 7/8/9/11；§8 关键决策 → 分散到各任务；§9 验收 → Task 13。
- **类型一致性**：`SearchEvidence` 只在 `orders/models.py` 定义一次；store 方法、service 方法、工具、路由使用同一组名字；`OrderDraft`/`Order` 的 `items` 都是 `tuple[DraftItem, ...]`。
- **Review Focus 五条**均已绑定到具体测试（Task 7/8/2/6/5）。
- **比例**：计划只固定签名、测试断言、状态迁移顺序和错误映射，不复写函数体。
