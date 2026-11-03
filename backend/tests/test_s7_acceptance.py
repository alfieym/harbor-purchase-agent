"""S7 acceptance checklist (7 items), deterministic and offline.

Each test pins one line of the acceptance list. The real AgentScope compression
pipeline runs; only the summary model is a stub, so no API call is made.
"""

import asyncio
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.agent import Agent, ContextConfig
from agentscope.message import (
    AssistantMsg,
    Msg,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    UserMsg,
)
from agentscope.model import StructuredResponse
from agentscope.tool import Toolkit

from harbor.agents.context_governance import (
    PROTECTED_FACTS_HEADER,
    ContextGovernanceMiddleware,
    ProductFact,
    SessionArchiveOffloader,
    append_protected_facts,
    facts_from_appendix,
    preferences_from_appendix,
)
from harbor.agents.prompts import SUMMARY_TEMPLATE

SUMMARY = {
    "task_overview": "用户要买宠物用品，预算 2000 以内",
    "current_state": "已推荐过商品",
    "important_discoveries": "",
    "next_steps": "",
    "context_to_preserve": "预算 2000 以内",
}


def _run(coro):
    return asyncio.run(coro)


class _SummaryModel:
    def __init__(self, context_size: int, summary: dict) -> None:
        self.context_size = context_size
        self.summary = summary

    async def count_tokens(self, messages, tools=None) -> int:
        size = sum(len(json.dumps(m.model_dump(mode="json"), ensure_ascii=False)) for m in messages)
        return size // 4 + 1

    async def generate_structured_output(self, messages, structured_model, **kwargs):
        return StructuredResponse(content=self.summary)


class _PinnedModel:
    context_size = 100_000

    async def count_tokens(self, messages, tools=None) -> int:
        return 10


def _cards_message(sku: str = "P3472-S1", price: float = 567.0, call_id: str = "call-1") -> Msg:
    return AssistantMsg(
        name="purchase_agent",
        content=[
            ToolCallBlock(
                id=call_id,
                name="GenerateStructuredOutput",
                input=json.dumps(
                    {
                        "summary": "推荐 1 件",
                        "cards": [
                            {
                                "title": "猫用自动饮水器",
                                "sku_id": sku,
                                "price_major": price,
                                "currency": "CNY",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
            )
        ],
    )


def _tool_turn(index: int, result_chars: int = 1200) -> list[Msg]:
    return [
        UserMsg(name="user", content=f"第{index}轮：给猫找个饮水器"),
        AssistantMsg(
            name="purchase_agent",
            content=[ToolCallBlock(id=f"search-{index}", name="product_search", input="{}")],
        ),
        AssistantMsg(
            name="purchase_agent",
            content=[
                ToolResultBlock(
                    id=f"search-{index}",
                    name="product_search",
                    output="命中结果 " + ("辅" * result_chars),
                )
            ],
        ),
        AssistantMsg(name="purchase_agent", content=[TextBlock(text="推荐如下 " + "答" * 200)]),
    ]


def _agent(
    tmp_path: Path, context: list[Msg]
) -> tuple[Agent, SessionArchiveOffloader, _SummaryModel]:
    offloader = SessionArchiveOffloader(tmp_path)
    middleware = ContextGovernanceMiddleware(offloader=offloader, protect_turns=2)
    model = _SummaryModel(context_size=8000, summary=dict(SUMMARY))
    agent = Agent(
        name="purchase_agent",
        system_prompt="You are a purchase agent. Stable prefix.",
        model=model,
        toolkit=Toolkit(),
        middlewares=[middleware],
        offloader=offloader,
        context_config=ContextConfig(
            trigger_ratio=0.6,
            reserve_ratio=0.1,
            compression_fallback_to_truncation=False,
            summary_template=SUMMARY_TEMPLATE,
        ),
    )
    agent.state.context = context
    return agent, offloader, model


def _count(agent: Agent) -> int:
    async def run() -> int:
        prepared = await agent._prepare_model_input()
        return await agent.model.count_tokens(prepared["messages"], prepared.get("tools"))

    return _run(run())


def _long_context() -> list[Msg]:
    context: list[Msg] = []
    for index in range(1, 7):
        context.extend(_tool_turn(index))
    context.append(_cards_message())
    return context


# ---------------------------------------------------------------------------
# S7-1
# ---------------------------------------------------------------------------


def test_s7_1_forty_long_turns_never_exceed_the_gate(tmp_path: Path) -> None:
    agent, offloader, _ = _agent(tmp_path, [])
    gate = int(agent.model.context_size * 0.85)

    for index in range(1, 41):
        agent.state.context.extend(_tool_turn(index))
        _run(agent.compress_context())
        assert _count(agent) <= gate, f"turn {index} exceeded the gate"

    archives = list(tmp_path.rglob("*.json"))
    assert archives, "40 turns produced no archive: compression never ran"
    assert len(archives) >= 2, "the context should have been compressed repeatedly"


# ---------------------------------------------------------------------------
# S7-2
# ---------------------------------------------------------------------------


def test_s7_2_summary_carries_the_five_fields(tmp_path: Path) -> None:
    agent, _, _ = _agent(tmp_path, _long_context())
    _run(agent.compress_context())

    summary = agent.state.summary
    assert isinstance(summary, str)
    for heading in ("# 目标", "# 当前进展", "# 关键事实", "# 下一步", "# 必须保留"):
        assert heading in summary


# ---------------------------------------------------------------------------
# S7-3
# ---------------------------------------------------------------------------


def test_s7_3_budget_and_product_survive_repeated_compression(tmp_path: Path) -> None:
    context = _long_context()
    context.insert(0, UserMsg(name="user", content="预算 2000 以内，只买母婴宠物类目。"))
    agent, _, _ = _agent(tmp_path, context)

    _run(agent.compress_context())
    first = agent.state.summary
    assert "预算 2000 以内" in first
    assert "P3472-S1" in first and "567.00" in first

    # Push the original budget message out of the retained tail, then compress
    # again: the appendix must carry both facts forward.
    for index in range(7, 15):
        agent.state.context.extend(_tool_turn(index))
    _run(agent.compress_context())

    second = agent.state.summary
    assert "预算 2000 以内" in second, "budget was lost on the second compression"
    assert "P3472-S1" in second, "product was lost on the second compression"


def test_s7_3_appendix_round_trips_without_duplicating() -> None:
    facts = [ProductFact("P3472-S1", "猫用自动饮水器", 567.0, "CNY")]
    first = append_protected_facts("摘要", facts, ["预算 2000 以内"])
    assert isinstance(first, str)

    recovered = facts_from_appendix(first)
    assert [(fact.sku_id, fact.price_major, fact.currency) for fact in recovered] == [
        ("P3472-S1", 567.0, "CNY")
    ]
    assert preferences_from_appendix(first) == ["预算 2000 以内"]

    second = append_protected_facts(
        "新的摘要",
        [*recovered, *facts],
        [*preferences_from_appendix(first), "预算 2000 以内"],
    )
    assert isinstance(second, str)
    assert second.count(PROTECTED_FACTS_HEADER) == 1
    assert "P3472-S1" in second and "预算 2000 以内" in second


# ---------------------------------------------------------------------------
# S7-4
# ---------------------------------------------------------------------------


def test_s7_4_compression_logs_tokens_and_duration(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    agent, _, _ = _agent(tmp_path, _long_context())
    caplog.set_level(logging.INFO, logger="harbor.agents.context_governance")

    _run(agent.compress_context())

    lines = [record.getMessage() for record in caplog.records]
    matched = [line for line in lines if "context compression ok" in line]
    assert matched, lines
    assert re.search(r"tokens \d+ -> \d+", matched[0])
    assert re.search(r"\d+\.\d ms", matched[0])


# ---------------------------------------------------------------------------
# S7-6
# ---------------------------------------------------------------------------


def test_s7_6_compression_keeps_system_prompt_and_tool_order(tmp_path: Path) -> None:
    agent, _, _ = _agent(tmp_path, _long_context())

    async def snapshot():
        prepared = await agent._prepare_model_input()
        return (
            prepared["messages"][0].get_text_content(),
            json.dumps(prepared.get("tools"), ensure_ascii=False),
        )

    _run(snapshot())  # warm-up: let AgentScope register any built-in tool once
    before = _run(snapshot())
    _run(agent.compress_context())
    after = _run(snapshot())

    assert before[0] == after[0], "compression changed the system prompt"
    assert before[1] == after[1], "compression changed the tool definitions or order"


# ---------------------------------------------------------------------------
# S7-7
# ---------------------------------------------------------------------------


def test_s7_7_preferences_injected_as_trailing_user_message() -> None:
    context = [UserMsg(name="user", content="预算 2000 以内，给猫找个饮水器")]
    agent = SimpleNamespace(
        state=SimpleNamespace(context=context, summary="", middle_context={}),
    )
    middleware = ContextGovernanceMiddleware(offloader=SessionArchiveOffloader(Path("/tmp/unused")))
    forwarded: dict[str, list] = {}

    async def next_handler(**kwargs):
        forwarded["messages"] = kwargs["messages"]
        return "ok"

    result = _run(
        middleware.on_model_call(
            agent=agent,
            input_kwargs={
                "current_model": _PinnedModel(),
                "messages": [UserMsg(name="user", content="current turn")],
                "tools": [],
            },
            next_handler=next_handler,
        )
    )

    assert result == "ok"
    injected = forwarded["messages"][-1]
    assert injected.role == "user"
    assert "预算 2000 以内" in (injected.get_text_content() or "")
    # Not appended to the persisted conversation.
    assert len(agent.state.context) == 1
    # Preferences never touched the system prompt (there is none in this stub),
    # and the original messages are still in place ahead of the injection.
    assert forwarded["messages"][0].get_text_content() == "current turn"
