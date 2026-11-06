"""S11-16/20: run_turn gates on budget, resets the window, guards the reply."""

import asyncio

from agentscope.message import Msg, TextBlock

from harbor.features.chat.service import SessionStore, run_turn
from harbor.guardrails.budget import SqliteTokenBudget
from harbor.guardrails.circuit import CircuitBreaker
from harbor.guardrails.events import GuardMetrics
from harbor.guardrails.runtime import GuardRuntime
from harbor.guardrails.session import GuardSession


class _State:
    def __init__(self) -> None:
        self.context: list[Msg] = []


def _guard(tmp_path, *, cap: int = 999_999) -> GuardRuntime:
    return GuardRuntime(
        budget=SqliteTokenBudget(tmp_path / "budget.sqlite3", cap),
        circuits=CircuitBreaker(3, 60.0),
        metrics=GuardMetrics(),
    )


def _reply(summary: str) -> Msg:
    return Msg(
        name="purchase_agent",
        role="assistant",
        content=[TextBlock(type="text", text=summary)],
        structured_output={"cards": [], "summary": summary},
    )


class _NeverCalledAgent:
    def __init__(self) -> None:
        self.state = _State()
        self.replies = 0

    async def reply(self, msg, structured_schema=None):
        self.replies += 1
        raise AssertionError("budget fallback must not call the agent")


class _LoopAwareAgent:
    """Fails the turn if the previous turn's search history is still visible."""

    def __init__(self) -> None:
        self.state = _State()
        self.guard_session = GuardSession(loop_threshold=2)
        self.replies = 0

    async def reply(self, msg, structured_schema=None):
        self.replies += 1
        args = {"query": "猫饮水器"}
        if self.guard_session.would_loop("product_search", args):
            raise RuntimeError("loop detected")
        self.guard_session.observe("product_search", args)
        result = _reply("好的，找到了")
        self.state.context.append(result)
        return result


def test_budget_fallback_short_circuits_without_agent(tmp_path) -> None:
    guard = _guard(tmp_path)
    agent = _NeverCalledAgent()
    store = SessionStore(agent_factory=lambda state=None: agent, guard=guard)
    # Consume the whole day: the next turn's tier is fallback (ratio <= 5%).
    assert guard.budget.acquire(999_999).granted

    response = asyncio.run(run_turn(store, "b1", "s1", "你好", timeout_seconds=5))

    assert response.cards == []
    assert "额度" in response.final_text
    assert agent.replies == 0


def test_turn_resets_loop_window(tmp_path) -> None:
    guard = _guard(tmp_path)
    agent = _LoopAwareAgent()
    store = SessionStore(agent_factory=lambda state=None: agent, guard=guard)

    first = asyncio.run(run_turn(store, "b1", "s1", "搜猫饮水器", timeout_seconds=5))
    second = asyncio.run(run_turn(store, "b1", "s1", "再搜猫饮水器", timeout_seconds=5))

    assert first.final_text
    assert second.final_text
    assert agent.replies == 2
