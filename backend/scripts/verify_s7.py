"""S7 手工验证：归档指针、摘要来源校验、回滚、容量闸门、轮次计数（离线）。

跑的是**真实 AgentScope 压缩管线**（真 ``Agent``、真切分、真 offloader、真 JSON 落盘），
只有「摘要模型」是桩——所以零 API 调用、零 Qdrant、零网络。

用法（仓库根目录）：

    uv run --directory backend python scripts/verify_s7.py

产物默认写到 ``data/sessions/archives/_manual-s7/``（每次运行前清空），
可以直接用编辑器或 ``jq`` 打开检查。
"""

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from agentscope.agent import Agent, ContextConfig
from agentscope.message import AssistantMsg, Msg, TextBlock, ToolCallBlock, UserMsg
from agentscope.model import StructuredResponse
from agentscope.tool import Toolkit

from harbor.agents.context_governance import (
    PROTECTED_FACTS_HEADER,
    ContextCapacityError,
    ContextGovernanceMiddleware,
    SessionArchiveOffloader,
    recorded_turn_count,
)
from harbor.core.config import Settings

SUMMARY = {
    "task_overview": "用户要买宠物用品",
    "current_state": "已推荐过商品",
    "important_discoveries": "",
    "next_steps": "",
    "context_to_preserve": "",
}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


class _SummaryModel:
    """Duck-typed chat model: real compression, canned summary, no network."""

    def __init__(self, summary: dict[str, Any], context_size: int = 4000) -> None:
        self.context_size = context_size
        self.summary = summary
        self.summary_calls = 0

    async def count_tokens(self, messages: list[Msg], tools: Any = None) -> int:
        size = sum(len(json.dumps(m.model_dump(mode="json"), ensure_ascii=False)) for m in messages)
        return size // 4 + 1

    async def generate_structured_output(
        self, messages: list[Msg], structured_model: Any, **_: Any
    ):
        self.summary_calls += 1
        return StructuredResponse(content=self.summary)


def _long_turn(label: str, role: str = "user") -> Msg:
    body = f"{label} " + ("补" * 900)
    if role == "user":
        return UserMsg(name="user", content=body)
    return AssistantMsg(name="purchase_agent", content=[TextBlock(text=body)])


def _cards_message(sku: str = "P3472-S1", price: float = 567.0) -> Msg:
    return AssistantMsg(
        name="purchase_agent",
        content=[
            ToolCallBlock(
                id="call-1",
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


def _seed_context() -> list[Msg]:
    return [
        _long_turn("第1轮问题"),
        _long_turn("第1轮回答", role="assistant"),
        _long_turn("第2轮问题"),
        _cards_message(),
        _long_turn("第2轮回答", role="assistant"),
        _long_turn("第3轮问题"),
        _long_turn("第3轮回答", role="assistant"),
        _long_turn("第4轮问题"),
        _long_turn("第4轮回答", role="assistant"),
    ]


def _real_agent(
    root: Path, summary: dict[str, Any]
) -> tuple[Agent, SessionArchiveOffloader, _SummaryModel]:
    offloader = SessionArchiveOffloader(root)
    middleware = ContextGovernanceMiddleware(offloader=offloader, protect_turns=2)
    model = _SummaryModel(summary)
    agent = Agent(
        name="purchase_agent",
        system_prompt="You are a purchase agent.",
        model=model,
        toolkit=Toolkit(),
        middlewares=[middleware],
        offloader=offloader,
        context_config=ContextConfig(
            trigger_ratio=0.6,
            reserve_ratio=0.1,
            compression_fallback_to_truncation=False,
        ),
    )
    agent.state.context = _seed_context()
    return agent, offloader, model


def _summary_text(summary: Any) -> str:
    if isinstance(summary, str):
        return summary
    return "\n".join(getattr(block, "text", "") for block in summary)


def _longest_kept(text: str, limit: int = 300) -> str:
    flat = " ".join(text.split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


# ---------------------------------------------------------------------------
# gate fixture
# ---------------------------------------------------------------------------


class _PinnedModel:
    context_size = 1000

    def __init__(self, tokens: int) -> None:
        self.tokens = tokens
        self.model_calls = 0

    async def count_tokens(self, messages: Any, tools: Any = None) -> int:
        return self.tokens


class _GateAgent:
    """Only the surface ``on_model_call`` touches."""

    def __init__(self) -> None:
        self.state = type(
            "State",
            (),
            {"session_id": "s7-gate", "middle_context": {}, "context": [], "summary": ""},
        )()
        self.context_config = ContextConfig(trigger_ratio=0.6, reserve_ratio=0.2)
        self.model = _PinnedModel(tokens=950)
        self.compress_calls = 0

    async def _prepare_model_input(self) -> dict[str, Any]:
        return {"messages": [UserMsg(name="user", content="x")], "tools": []}

    async def compress_context(self, context_config: Any = None, instructions: Any = None) -> None:
        self.compress_calls += 1


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------


async def scenario_archive(root: Path) -> None:
    agent, offloader, _ = _real_agent(root, dict(SUMMARY))
    await agent.compress_context()

    record = offloader.last_batch
    assert record is not None, "压缩没有触发（fixture 的上下文没超过触发阈值）"
    files = sorted(root.rglob("*.json"))
    assert files, "没有写出归档文件"
    summary = _summary_text(agent.state.summary)

    assert PROTECTED_FACTS_HEADER in summary, "摘要缺少受保护事实附录"
    assert "P3472-S1" in summary and "567.00" in summary, "附录丢了 SKU 或价格"
    assert record.archive_id in summary, "摘要里没有归档指针，模型无法回查原文"
    kept = "\n".join(m.get_text_content() or "" for m in agent.state.context)
    assert "第3轮问题" in kept and "第4轮问题" in kept, "最近两轮被压掉了"

    print(f"  ✓ 归档文件：{files[0]}")
    print(f"  ✓ 指针：{record.archive_id}（{len(record.messages)} 条消息）")
    print(f"  ✓ 保留消息：{len(agent.state.context)} 条（最后两轮完整）")
    print("  ✓ 摘要附录：")
    for line in summary.splitlines():
        if line.strip().startswith(("##", "- ")):
            print(f"      {line}")

    payload = json.loads(files[0].read_text(encoding="utf-8"))
    print("  ✓ 归档文件结构：")
    print(f"      archive_id={payload['archive_id']!r} message_count={payload['message_count']}")

    rendered = offloader.read(record.archive_id)
    first = json.loads(rendered)["messages"][0]
    excerpt = _longest_kept(first["text"], 40)
    print(f"  ✓ read_archive 回查第一条：role={first['role']!r} text={excerpt!r}")


async def scenario_rollback(root: Path) -> None:
    invented = dict(SUMMARY)
    invented["current_state"] = "之前推荐过 P9999-S1（¥320.00）"
    agent, offloader, _ = _real_agent(root, invented)
    before_context = list(agent.state.context)
    before_summary = agent.state.summary

    await agent.compress_context()

    assert agent.state.context == before_context, "编造 SKU 后上下文没有恢复"
    assert agent.state.summary == before_summary, "编造 SKU 后摘要没有恢复"
    assert offloader.last_batch is None, "回滚后仍然保留了 last_batch"
    files = sorted(root.rglob("*.json"))
    print(f"  ✓ 摘要被驳回，context/summary 已回滚（{len(before_context)} 条消息原样保留）")
    print(f"  ✓ 归档文件仍留在磁盘上（可审计的孤儿）：{[f.name for f in files[-1:]]}")


async def scenario_gate() -> None:
    offloader = SessionArchiveOffloader(Path("/tmp/_s7_gate_unused"))
    middleware = ContextGovernanceMiddleware(offloader=offloader, gate_ratio=0.85)
    agent = _GateAgent()
    forwarded = {"called": False}

    async def next_handler(**_: Any) -> Any:
        forwarded["called"] = True
        return None

    try:
        await middleware.on_model_call(
            agent=agent,
            input_kwargs={
                "current_model": agent.model,
                "messages": [UserMsg(name="user", content="x")],
                "tools": [],
            },
            next_handler=next_handler,
        )
    except ContextCapacityError as exc:
        print(f"  ✓ 闸门触发：{exc.tokens} tokens > {exc.limit}")
    else:
        raise AssertionError("超限请求没有被拦下")

    assert agent.compress_calls == 1, "应当只强制压缩一次"
    assert forwarded["called"] is False, "超限请求居然被转发给了模型"

    try:
        await middleware.on_model_call(
            agent=agent,
            input_kwargs={
                "current_model": agent.model,
                "messages": [UserMsg(name="user", content="x")],
                "tools": [],
            },
            next_handler=next_handler,
        )
    except ContextCapacityError:
        pass
    else:
        raise AssertionError("重试没有被立即拦下")

    assert agent.compress_calls == 1, "重试路径又做了一次压缩"
    assert forwarded["called"] is False
    print("  ✓ 重试立即失败：没有重复压缩，模型调用次数为 0")


async def scenario_turn_count(root: Path) -> None:
    agent, _, _ = _real_agent(root, dict(SUMMARY))

    async def no_events(**_: Any) -> Any:
        if False:
            yield None

    middleware = ContextGovernanceMiddleware(offloader=SessionArchiveOffloader(root))
    for _ in range(2):
        async for _event in middleware.on_reply(agent, {"inputs": None}, no_events):
            pass

    before = recorded_turn_count(agent.state)
    await agent.compress_context()
    after = recorded_turn_count(agent.state)
    users_left = sum(1 for m in agent.state.context if getattr(m, "role", None) == "user")

    assert before > 0, "turn_count 没有被记录"
    assert after == before, "压缩后 turn_count 倒退了"
    print(f"  ✓ 压缩前 turn_count={before}，压缩后={after}")
    print(f"  ✓ context 里的 user 消息压缩后只剩 {users_left} 条")


async def main() -> None:
    settings = Settings()
    root = settings.context_archive_dir / "_manual-s7"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    print("S7 手工验证（真实 AgentScope 压缩管线，零 API）\n")
    print(f"归档根目录：{root}\n")

    print("[1/4] 正常压缩：归档 + 保护边界 + 事实附录")
    await scenario_archive(root / "archive")

    print("\n[2/4] 摘要编造 SKU：整份回滚")
    await scenario_rollback(root / "rollback")

    print("\n[3/4] 容量闸门：超限不发给模型")
    await scenario_gate()

    print("\n[4/4] 轮次计数：压缩后不倒退")
    await scenario_turn_count(root / "turn-count")

    print("\n全部通过。可以打开归档文件逐条检查：")
    for path in sorted(root.rglob("*.json")):
        print(f"  {path}")


if __name__ == "__main__":
    asyncio.run(main())
