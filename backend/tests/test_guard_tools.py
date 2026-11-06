import asyncio
import json

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

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
