"""Per-session guard state: loop window and sequence history (spec §4.3, §6).

This is deliberately separate from ``GuardRuntime``: the model object is built
once and serves every session, so budget and circuit state must be global,
while "what did this agent call this turn" must not leak between buyers or
between a parent agent and its sub-agents.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GuardRule:
    """A declarative sequence assertion.

    ``forbidden_before`` names tools that must not already have run in this
    turn for ``tool`` to be allowed. Keep the default table small: a rule that
    fires on a legitimate call is worse than a missed detection.
    """

    tool: str
    forbidden_before: tuple[str, ...] = ()
    message: str = ""


DEFAULT_SEQUENCE_RULES: tuple[GuardRule, ...] = (
    GuardRule(
        tool="create_order_draft",
        forbidden_before=("create_order_draft",),
        message="本轮已创建过草稿，请修改已有草稿或直接询问买家，不要重复创建",
    ),
)


def _signature(tool: str, args: dict[str, Any]) -> tuple[str, str]:
    # ``_agent_state`` is injected by AgentScope, is not serializable and
    # differs on every call, so it must not participate in the hash.
    payload = {key: value for key, value in args.items() if key != "_agent_state"}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return tool, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class GuardSession:
    def __init__(self, loop_window: int = 8, loop_threshold: int = 3) -> None:
        self.loop_window = loop_window
        self.loop_threshold = loop_threshold
        self.recent_tool_calls: deque[tuple[str, str]] = deque(maxlen=loop_window)
        self.turn_calls: list[str] = []

    def begin_turn(self) -> None:
        self.recent_tool_calls.clear()
        self.turn_calls.clear()

    def observe(self, tool: str, args: dict[str, Any]) -> None:
        self.recent_tool_calls.append(_signature(tool, args))
        self.turn_calls.append(tool)

    def would_loop(self, tool: str, args: dict[str, Any]) -> bool:
        signature = _signature(tool, args)
        return self.recent_tool_calls.count(signature) + 1 >= self.loop_threshold

    def sequence_violation(self, tool: str, rules: tuple[GuardRule, ...]) -> GuardRule | None:
        seen = set(self.turn_calls)
        for rule in rules:
            if rule.tool != tool:
                continue
            if any(name in seen for name in rule.forbidden_before):
                return rule
        return None
