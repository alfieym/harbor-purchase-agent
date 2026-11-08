"""S14: the run manager the lifespan builds completes a real turn offline.

No network: the chat store is a fake agent, and the search tool's output is a
plain tool_result block. What is under test is the wiring — a run through
``run_turn`` ends in a ``STATE_SNAPSHOT`` whose cards came from the structured
response, never from parsing the text.
"""

import asyncio
import json
from contextlib import aclosing
from pathlib import Path

from agentscope.message import Msg, TextBlock, ToolResultBlock, ToolResultState
from agentscope.state import AgentState

from harbor.core.config import Settings
from harbor.features.chat.service import SessionStore
from harbor.main import build_run_manager

_HIT = {"product_id": "X1", "title": "台灯", "selected_sku_id": "X1-S1"}
_SUMMARY = "找到 1 件台灯"
_CARDS = {
    "cards": [
        {
            "title": "台灯",
            "brand": "Lumo",
            "category": "家居",
            "price_major": 99.0,
            "currency": "CNY",
            "weight_kg": 0.4,
            "material_tags": [],
            "highlights": [],
            "sku_id": "X1-S1",
            "score": None,
        }
    ],
    "summary": _SUMMARY,
}


class _Agent:
    def __init__(self) -> None:
        self.state = AgentState()

    async def reply(self, msg, structured_schema=None) -> Msg:
        self.state.context.append(msg)
        self.state.context.append(
            Msg(
                name="purchase_agent",
                role="assistant",
                content=[
                    ToolResultBlock(
                        id="call-1",
                        name="product_search",
                        output=[
                            TextBlock(
                                type="text",
                                text=json.dumps({"hits": [_HIT]}, ensure_ascii=False),
                            )
                        ],
                        state=ToolResultState.SUCCESS,
                    )
                ],
            )
        )
        reply = Msg(
            name="purchase_agent",
            role="assistant",
            content=[TextBlock(type="text", text=_SUMMARY)],
            structured_output=_CARDS,
        )
        self.state.context.append(reply)
        return reply


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        openai_api_key="sk-test",
        sessions_dir=tmp_path / "sessions",
        orders_db_path=tmp_path / "orders.sqlite3",
        runs_db_path=tmp_path / "runs.sqlite3",
        **overrides,
    )


def test_without_a_chat_store_there_is_no_run_manager(tmp_path: Path) -> None:
    assert build_run_manager(_settings(tmp_path), None, None) is None


def test_the_kill_switch_removes_the_manager(tmp_path: Path) -> None:
    store = SessionStore(agent_factory=lambda state=None: _Agent())

    assert build_run_manager(_settings(tmp_path, agui_enabled=False), store, None) is None


def test_a_wired_run_rebuilds_cards_from_structured_state(tmp_path: Path) -> None:
    manager = build_run_manager(
        _settings(tmp_path),
        SessionStore(agent_factory=lambda state=None: _Agent()),
        None,
    )
    assert manager is not None

    async def scenario():
        await manager.startup()
        handle = await manager.start_run(buyer_id="b1", session_id="s1", text="找台灯")
        events: list[tuple[int, dict]] = []
        async with aclosing(manager.events(handle.run_id)) as stream:
            async for item in stream:
                if item is None:
                    continue
                events.append(item)
                if item[1]["type"] in ("RUN_FINISHED", "RUN_ERROR"):
                    break
        await manager.shutdown()
        return events

    events = asyncio.run(scenario())

    types = [event["type"] for _, event in events]
    assert types[0] == "RUN_STARTED"
    assert "TOOL_CALL_START" in types
    assert types[-1] == "RUN_FINISHED"
    snapshot = next(event for _, event in events if event["type"] == "STATE_SNAPSHOT")
    assert snapshot["snapshot"]["final_text"] == _SUMMARY
    assert snapshot["snapshot"]["cards"][0]["sku_id"] == "X1-S1"
