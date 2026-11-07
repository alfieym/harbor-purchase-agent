import asyncio
import json

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from harbor.guardrails.circuit import CircuitBreaker, CircuitState
from harbor.guardrails.content import ContentFilter
from harbor.guardrails.events import GuardMetrics
from harbor.guardrails.session import GuardSession
from harbor.guardrails.tools import GuardedTool


class EchoTool:
    name = "echo"
    description = "echo"
    input_schema = {"type": "object"}
    is_concurrency_safe = True
    is_read_only = True
    is_state_injected = False
    is_external_tool = False

    def __init__(self, name: str = "echo") -> None:
        self.name = name
        self.calls = 0

    async def __call__(self, **kwargs):
        self.calls += 1
        # 与 S10 的 order_tools._result 同形状
        return ToolChunk(
            content=[TextBlock(text=json.dumps({"ok": True, "echo": kwargs}, ensure_ascii=False))],
            state=ToolResultState.SUCCESS,
        )


def test_third_identical_call_is_blocked() -> None:
    inner = EchoTool()
    session = GuardSession(loop_window=8, loop_threshold=3)
    tool = GuardedTool(
        inner, session=session, metrics=GuardMetrics(), content_filter=ContentFilter()
    )
    for _ in range(2):
        asyncio.run(tool(x=1))
    out = asyncio.run(tool(x=1))
    assert out.state == ToolResultState.SUCCESS
    assert '"loop_detected"' in out.content[0].text
    assert inner.calls == 2


def test_different_args_do_not_loop() -> None:
    inner = EchoTool()
    session = GuardSession(loop_threshold=2)
    tool = GuardedTool(
        inner, session=session, metrics=GuardMetrics(), content_filter=ContentFilter()
    )
    for i in range(4):
        asyncio.run(tool(x=i))
    assert inner.calls == 4


def test_sequence_rule_blocks_second_draft() -> None:
    inner = EchoTool(name="create_order_draft")
    session = GuardSession()
    tool = GuardedTool(
        inner, session=session, metrics=GuardMetrics(), content_filter=ContentFilter()
    )
    asyncio.run(tool(items=[]))
    out = asyncio.run(tool(items=[]))
    assert "sequence_violation" in out.content[0].text
    assert inner.calls == 1


def test_blocked_call_does_not_enter_the_window() -> None:
    # 被拒绝的调用不记录，否则拒一次之后永远拒绝。
    inner = EchoTool()
    session = GuardSession(loop_threshold=2)
    tool = GuardedTool(
        inner, session=session, metrics=GuardMetrics(), content_filter=ContentFilter()
    )
    asyncio.run(tool(x=1))
    blocked = asyncio.run(tool(x=1))
    assert '"loop_detected"' in blocked.content[0].text
    assert len(session.recent_tool_calls) == 1


class HitsTool:
    """A well-formed ``product_search`` result: the shape the model relies on."""

    name = "product_search"
    description = "search"
    input_schema = {"type": "object"}
    is_concurrency_safe = True
    is_read_only = True
    is_state_external = False

    async def __call__(self, **kwargs):
        return ToolChunk(
            content=[TextBlock(text=json.dumps({"ok": True, "hits": []}, ensure_ascii=False))],
            state=ToolResultState.SUCCESS,
        )


def test_missing_hits_is_reported_without_raising() -> None:
    # S11-6: 工具声称成功却没给 hits —— raise 会把整轮变成 502，静默会让模型
    # 以为「搜到 0 条」。提示写回 payload（struct 字段），JSON 仍可解析。
    broken = GuardedTool(
        EchoTool(name="product_search"),
        session=GuardSession(),
        metrics=GuardMetrics(),
        content_filter=ContentFilter(),
    )
    out = asyncio.run(broken(query="猫饮水器"))
    payload = json.loads(out.content[0].text)
    assert out.state == ToolResultState.SUCCESS
    assert payload["ok"] is True
    assert "hits" in payload["structure_anomaly"]

    healthy = GuardedTool(
        HitsTool(),
        session=GuardSession(),
        metrics=GuardMetrics(),
        content_filter=ContentFilter(),
    )
    intact = json.loads(asyncio.run(healthy(query="猫饮水器")).content[0].text)
    assert "structure_anomaly" not in intact


class FlakyTool:
    name = "flaky"
    description = "flaky"
    input_schema = {"type": "object"}
    is_concurrency_safe = True
    is_read_only = True

    def __init__(self) -> None:
        self.calls = 0
        self.fail = True

    async def __call__(self, **kwargs):
        self.calls += 1
        if self.fail:
            return ToolChunk(
                content=[TextBlock(text=json.dumps({"ok": False, "error": "storage"}))],
                state=ToolResultState.ERROR,
            )
        return ToolChunk(
            content=[TextBlock(text=json.dumps({"ok": True}))],
            state=ToolResultState.SUCCESS,
        )


def test_tool_breaker_opens_after_three_failures_and_closes_on_probe() -> None:
    # S11-1（工具面）：连续 3 次失败 → open；冷却后放一个探针；成功一次 → closed。
    clock = [0.0]
    breaker = CircuitBreaker(3, 60.0, clock=lambda: clock[0])
    inner = FlakyTool()
    tool = GuardedTool(
        inner,
        session=GuardSession(),
        metrics=GuardMetrics(),
        content_filter=ContentFilter(),
        circuits=breaker,
    )

    # 参数各不相同，避免先被循环检测拦下。
    for i in range(3):
        out = asyncio.run(tool(x=i))
        assert out.state == ToolResultState.ERROR
    assert breaker.state("flaky") is CircuitState.OPEN
    assert inner.calls == 3

    # 熔断期间不再执行工具，模型收到可纠正的 ok=false，而不是整轮 502。
    blocked = asyncio.run(tool(x=3))
    payload = json.loads(blocked.content[0].text)
    assert blocked.state == ToolResultState.SUCCESS
    assert payload["error"] == "circuit_open"
    assert inner.calls == 3

    # 冷却结束后放一个探针；成功即关闭。
    clock[0] = 61.0
    inner.fail = False
    probe = asyncio.run(tool(x=4))
    assert probe.state == ToolResultState.SUCCESS
    assert breaker.state("flaky") is CircuitState.CLOSED


class RejectingTool:
    name = "stock_check"
    description = "rejects"
    input_schema = {"type": "object"}
    is_concurrency_safe = True
    is_read_only = False

    async def __call__(self, **kwargs):
        return ToolChunk(
            content=[TextBlock(text=json.dumps({"ok": False, "error": "evidence_missing"}))],
            state=ToolResultState.SUCCESS,
        )


def test_business_rejection_does_not_trip_the_tool_breaker() -> None:
    # ok=false 的 SUCCESS chunk 是业务拒绝（如缺检索证据），不是工具故障。
    clock = [0.0]
    breaker = CircuitBreaker(2, 60.0, clock=lambda: clock[0])
    tool = GuardedTool(
        RejectingTool(),
        session=GuardSession(loop_threshold=99),
        metrics=GuardMetrics(),
        content_filter=ContentFilter(),
        circuits=breaker,
    )
    for i in range(4):
        asyncio.run(tool(x=i))
    assert breaker.state("stock_check") is CircuitState.CLOSED


def test_second_dispatch_in_a_turn_is_blocked() -> None:
    inner = EchoTool(name="task_dispatch")
    tool = GuardedTool(
        inner, session=GuardSession(), metrics=GuardMetrics(), content_filter=ContentFilter()
    )
    asyncio.run(tool(tasks=[]))
    out = asyncio.run(tool(tasks=[]))
    assert "sequence_violation" in out.content[0].text
    assert inner.calls == 1
