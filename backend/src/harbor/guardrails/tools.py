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

from harbor.guardrails.circuit import CircuitBreaker
from harbor.guardrails.content import ContentFilter
from harbor.guardrails.events import GuardMetrics, log_guard_event
from harbor.guardrails.runtime import GuardSwitches
from harbor.guardrails.session import DEFAULT_SEQUENCE_RULES, GuardRule, GuardSession

# Success payloads the model's reasoning depends on. A tool that reports
# ``ok=true`` but omits its documented shape is a bug the model cannot
# diagnose from the payload — it reads the absence as "nothing found" — so
# the guard annotates the payload instead of raising (S11-6).
_REQUIRED_PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {"product_search": ("hits",)}

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
        switches: GuardSwitches | None = None,
        circuits: CircuitBreaker | None = None,
    ) -> None:
        super().__init__(middlewares=None)
        self._inner = inner
        self._session = session
        self._metrics = metrics
        self._content_filter = content_filter
        self._rules = tuple(rules)
        self._switches = switches or GuardSwitches()
        self._circuits = circuits
        for attr in _PROXIED_ATTRS:
            setattr(self, attr, getattr(inner, attr, None))

    async def check_permissions(self, *args: Any, **kwargs: Any):
        return await self._inner.check_permissions(*args, **kwargs)

    async def call(self, *args: Any, **kwargs: Any) -> Any:
        # AgentScope's documented extension point: ``ToolBase.__call__`` is
        # middleware plumbing and awaits ``call``, so guarding here covers both
        # ``tool(...)`` (Toolkit) and ``tool.call(...)`` (direct callers).
        if args:
            raise TypeError("GuardedTool must be called with keyword arguments only")
        name = getattr(self, "name", "unknown")

        if self._switches.sequence:
            rule = self._session.sequence_violation(name, self._rules)
            if rule is not None:
                self._metrics.incr("tool.sequence_blocked")
                log_guard_event("tool.sequence_blocked", tool=name, rule=rule.tool)
                return blocked_chunk("sequence_violation", rule.message)

        if self._switches.loop and self._session.would_loop(name, kwargs):
            self._metrics.incr("tool.loop_blocked")
            log_guard_event("tool.loop_blocked", tool=name)
            return blocked_chunk(
                "loop_detected",
                f"相同调用已出现 {self._session.loop_threshold - 1} 次，"
                "请更换查询条件或直接回答",
            )

        if (
            self._switches.circuit
            and self._circuits is not None
            and not self._circuits.allow(name)
        ):
            # Same shape as the loop/sequence blocks: the model gets a
            # correctable ``ok=false``, not a whole-turn 502.
            log_guard_event("circuit.skip", tool=name, reason="circuit_open")
            return blocked_chunk(
                "circuit_open",
                "该工具连续失败，已暂时熔断；请改用其他方式或直接基于已有信息回答",
            )

        try:
            result = await self._inner(**kwargs)
        except Exception:
            self._record_outcome(name, failed=True)
            raise
        if inspect.isasyncgen(result):
            return self._filter_stream(result)
        self._record_outcome(
            name, failed=getattr(result, "state", None) == ToolResultState.ERROR
        )

        # Only calls that actually executed enter the window; a rejected call
        # that did would reject every later retry too. The sequence rule reads
        # the same window, so either switch needs the observation.
        if self._switches.loop or self._switches.sequence:
            self._session.observe(name, kwargs)
        return self._filter_chunk(result)

    def _record_outcome(self, name: str, *, failed: bool) -> None:
        # A business rejection is ``SUCCESS`` + ``ok=false`` and is the tool
        # working as designed — only an ERROR state trips the breaker.
        if not self._switches.circuit or self._circuits is None:
            return
        if failed:
            self._circuits.record_failure(name)
        else:
            self._circuits.record_success(name)

    # -------------------------------------------------------------- filtering

    def _filter_chunk(self, chunk: ToolChunk) -> ToolChunk:
        if not isinstance(chunk, ToolChunk):
            return chunk
        hits: list[str] = []
        content: list[Any] = []
        changed = False
        for block in chunk.content:
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                content.append(block)
                continue
            new_text = text
            if self._switches.content_filter:
                result = self._content_filter.filter(text)
                hits.extend(result.hits)
                new_text = result.text
            if self._switches.structure:
                new_text = self._annotate_payload(new_text)
            if new_text == text:
                content.append(block)
            else:
                changed = True
                content.append(block.model_copy(update={"text": new_text}))
        if hits:
            self._metrics.incr("content.filtered", count=len(hits))
            for rule in dict.fromkeys(hits):
                log_guard_event("content.filtered", tool=self.name, rule=rule)
        if not changed:
            return chunk
        return chunk.model_copy(update={"content": content})

    def _annotate_payload(self, text: str) -> str:
        """Append a structural hint when a success payload lost a field (S11-6)."""
        required = _REQUIRED_PAYLOAD_FIELDS.get(self.name)
        if not required:
            return text
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return text
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            return text
        missing = [key for key in required if key not in payload]
        if not missing:
            return text
        log_guard_event(
            "tool.structure_anomaly", tool=self.name, rule=",".join(missing)
        )
        annotated = {
            **payload,
            "structure_anomaly": (
                f"工具返回缺少字段 {', '.join(missing)}；"
                "这不等于没有结果，请勿据此下结论"
            ),
        }
        return json.dumps(annotated, ensure_ascii=False)

    async def _filter_stream(self, stream: AsyncIterator[ToolChunk]) -> AsyncIterator[ToolChunk]:
        async for chunk in stream:
            yield self._filter_chunk(chunk)


def wrap_tools(
    tools: Sequence[Any],
    *,
    session: GuardSession | None,
    metrics: GuardMetrics | None,
    max_chars: int,
    switches: GuardSwitches | None = None,
    circuits: CircuitBreaker | None = None,
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
            switches=switches,
            circuits=circuits,
        )
        for tool in tools
    ]
