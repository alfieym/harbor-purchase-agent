"""Behaviour-layer guardrails wrapped around every tool (spec §4.2, §6, §7.2).

``Toolkit`` has no global middleware hook, so assembly wraps each tool in this
proxy instead of editing every ``make_*`` factory. A blocked call returns the
same shape S10 uses for a business rejection — ``SUCCESS`` with ``ok=false`` —
because ``ToolResultState.ERROR`` is escalated to a whole-turn 502 and would
deny the model the chance to correct itself.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolBase, ToolChunk

from harbor.guardrails.content import ContentFilter
from harbor.guardrails.events import GuardMetrics, log_guard_event
from harbor.guardrails.session import DEFAULT_SEQUENCE_RULES, GuardRule, GuardSession

_PROXIED_ATTRS = (
    "name",
    "description",
    "input_schema",
    "is_concurrency_safe",
    "is_read_only",
    "is_state_injected",
    "is_external_tool",
    "is_mcp",
    "mcp_name",
    "metadata_schema",
)


def blocked_chunk(error: str, hint: str = "") -> ToolChunk:
    """Same shape as ``order_tools._result``: SUCCESS + ``ok=false``."""
    payload: dict[str, Any] = {"ok": False, "error": error}
    if hint:
        payload["hint"] = hint
    return ToolChunk(
        content=[TextBlock(text=json.dumps(payload, ensure_ascii=False))],
        state=ToolResultState.SUCCESS,
    )


class GuardedTool(ToolBase):
    def __init__(
        self,
        inner: Any,
        *,
        session: GuardSession,
        metrics: GuardMetrics,
        content_filter: ContentFilter,
        rules: Sequence[GuardRule] = DEFAULT_SEQUENCE_RULES,
    ) -> None:
        super().__init__(middlewares=None)
        self._inner = inner
        self._session = session
        self._metrics = metrics
        self._content_filter = content_filter
        self._rules = tuple(rules)
        for attr in _PROXIED_ATTRS:
            setattr(self, attr, getattr(inner, attr, None))

    async def check_permissions(self, *args: Any, **kwargs: Any):
        return await self._inner.check_permissions(*args, **kwargs)

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if args:
            raise TypeError("GuardedTool must be called with keyword arguments only")
        name = getattr(self, "name", "unknown")

        rule = self._session.sequence_violation(name, self._rules)
        if rule is not None:
            self._metrics.incr("tool.sequence_blocked")
            log_guard_event("tool.sequence_blocked", tool=name, rule=rule.tool)
            return blocked_chunk("sequence_violation", rule.message)

        if self._session.would_loop(name, kwargs):
            self._metrics.incr("tool.loop_blocked")
            log_guard_event("tool.loop_blocked", tool=name)
            return blocked_chunk(
                "loop_detected",
                f"相同调用已出现 {self._session.loop_threshold - 1} 次，"
                "请更换查询条件或直接回答",
            )

        result = await self._inner(**kwargs)
        if inspect.isasyncgen(result):
            return self._filter_stream(result)

        # Only calls that actually executed enter the window; a rejected call
        # that did would reject every later retry too.
        self._session.observe(name, kwargs)
        return self._filter_chunk(result)

    # -------------------------------------------------------------- filtering

    def _filter_chunk(self, chunk: ToolChunk) -> ToolChunk:
        if not isinstance(chunk, ToolChunk):
            return chunk
        hits: list[str] = []
        content: list[Any] = []
        for block in chunk.content:
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                content.append(block)
                continue
            result = self._content_filter.filter(text)
            hits.extend(result.hits)
            if result.text == text:
                content.append(block)
            else:
                content.append(block.model_copy(update={"text": result.text}))
        if hits:
            self._metrics.incr("content.filtered", count=len(hits))
            for rule in dict.fromkeys(hits):
                log_guard_event("content.filtered", tool=self.name, rule=rule)
        if not hits:
            return chunk
        return chunk.model_copy(update={"content": content})

    async def _filter_stream(self, stream: AsyncIterator[ToolChunk]) -> AsyncIterator[ToolChunk]:
        async for chunk in stream:
            yield self._filter_chunk(chunk)


def wrap_tools(
    tools: Sequence[Any],
    *,
    session: GuardSession | None,
    metrics: GuardMetrics | None,
    max_chars: int,
) -> Sequence[Any]:
    """Assembly seam shared by the main agent and S9 sub-agents."""
    if session is None:
        return tools
    content_filter = ContentFilter(max_chars)
    return [
        GuardedTool(
            tool,
            session=session,
            metrics=metrics or GuardMetrics(),
            content_filter=content_filter,
        )
        for tool in tools
    ]
