"""S8: the preference tools (add / confirm / cancel / update / delete)."""

import asyncio
import json
from pathlib import Path

from agentscope.message import ToolResultState
from agentscope.state import AgentState

from harbor.agents.preferences import PreferenceMemory, PreferenceWriteError, make_preference_tools
from harbor.features.memory.sqlite_store import SqliteMemoryStore


class FakeEmbedder:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("embedding down")
        return [[1.0, 0.0] for _ in texts]


def _memory(tmp_path: Path, embedder: FakeEmbedder | None = None) -> PreferenceMemory:
    store = SqliteMemoryStore(tmp_path / "memory.sqlite3")
    memory = PreferenceMemory(
        buyer_id="b1", store=store, embedder=embedder or FakeEmbedder(), vector_model="m1"
    )
    asyncio.run(memory.begin_turn(AgentState()))
    return memory


def _tools(memory: PreferenceMemory) -> dict:
    return {tool.name: tool for tool in make_preference_tools(memory)}


def _call(tool, state: AgentState, **kwargs) -> tuple[dict, ToolResultState]:
    chunk = asyncio.run(tool.call(_agent_state=state, **kwargs))
    return json.loads(chunk.content[0].text), chunk.state


def _add(tools: dict, state: AgentState, text: str, scope: str, **kwargs) -> dict:
    """add, then resolve the staged operation the way the model would."""
    staged, _ = _call(tools["add_preference"], state, text=text, scope=scope, **kwargs)
    if not staged.get("pending"):
        return staged
    confirmed, _ = _call(tools["confirm_preference"], state, operation_id=staged["operation"]["id"])
    return confirmed


def test_tools_have_the_expected_shape(tmp_path: Path) -> None:
    tools = _tools(_memory(tmp_path))
    assert set(tools) == {
        "add_preference",
        "confirm_preference",
        "cancel_preference",
        "update_preference",
        "delete_preference",
    }
    for tool in tools.values():
        assert tool.is_state_injected is True
        assert tool.is_read_only is False
        assert "_agent_state" not in tool.input_schema["properties"]


def test_add_preference_tool_returns_id_and_counts(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    state = AgentState()

    body = _add(_tools(memory), state, "只考虑母婴宠物类目", "session")

    assert body["ok"] is True
    assert body["preference"]["scope"] == "session"
    assert body["preference"]["id"].startswith("pref_")
    assert body["counts"] == {"turn": 0, "session": 1, "long_term": 0}


def test_a_session_add_is_staged_before_it_is_written(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    state = AgentState()

    body, tool_state = _call(
        _tools(memory)["add_preference"], state, text="只考虑母婴宠物类目", scope="session"
    )

    assert tool_state == ToolResultState.SUCCESS
    assert body["ok"] is True
    assert body["pending"] is True
    assert body["operation"]["scope"] == "session"
    assert body["counts"]["session"] == 0
    assert memory.session_items(state) == []


def test_turn_scope_is_written_without_confirmation(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    state = AgentState()

    body, _ = _call(_tools(memory)["add_preference"], state, text="这次预算 2000", scope="turn")

    assert "pending" not in body
    assert body["preference"]["scope"] == "turn"
    assert [item.text for item in memory.turn_items()] == ["这次预算 2000"]


def test_add_preference_tool_reports_a_duplicate(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    state = AgentState()
    tools = _tools(memory)

    first = _add(tools, state, "预算 2000 以内", "session")
    second = _add(tools, state, "预算 2000 以内", "session")

    assert second["ok"] is True
    assert second["duplicate"] is True
    assert second["preference"]["id"] == first["preference"]["id"]


def test_confirm_preference_reports_an_unknown_operation(tmp_path: Path) -> None:
    body, _ = _call(
        _tools(_memory(tmp_path))["confirm_preference"],
        AgentState(),
        operation_id="op_missing",
    )
    assert body == {"ok": False, "error": "没有这条待确认操作"}


def test_cancel_preference_drops_the_staged_add(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    state = AgentState()
    tools = _tools(memory)
    staged, _ = _call(tools["add_preference"], state, text="预算 2000", scope="session")

    body, _ = _call(tools["cancel_preference"], state, operation_id=staged["operation"]["id"])

    assert body["ok"] is True
    assert body["cancelled"]["id"] == staged["operation"]["id"]
    assert memory.session_items(state) == []


def test_update_preference_tool_keeps_the_id(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    state = AgentState()
    tools = _tools(memory)
    added = _add(tools, state, "预算 2000 以内", "session")

    body, tool_state = _call(
        tools["update_preference"],
        state,
        preference_id=added["preference"]["id"],
        text="预算 1500 以内",
    )

    assert tool_state == ToolResultState.SUCCESS
    assert body["ok"] is True
    assert body["preference"]["id"] == added["preference"]["id"]
    assert body["preference"]["text"] == "预算 1500 以内"


def test_update_preference_tool_reposts_a_stale_version_as_409(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    state = AgentState()
    tools = _tools(memory)
    added = _add(tools, state, "预算 2000 以内", "long_term")
    preference_id = added["preference"]["id"]

    _call(tools["update_preference"], state, preference_id=preference_id, text="预算 1500")
    stale, _ = _call(
        tools["update_preference"],
        state,
        preference_id=preference_id,
        text="预算 1000",
        expected_version=1,
    )

    assert stale["ok"] is False
    assert stale["code"] == "version_conflict"
    assert stale["status"] == 409
    assert stale["current"]["text"] == "预算 1500"


def test_update_preference_tool_reports_missing_id(tmp_path: Path) -> None:
    body, tool_state = _call(
        _tools(_memory(tmp_path))["update_preference"],
        AgentState(),
        preference_id="pref_missing",
        text="预算 1500",
    )
    assert tool_state == ToolResultState.SUCCESS
    assert body == {"ok": False, "error": "偏好不存在"}


def test_delete_preference_tool_removes_the_record(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    state = AgentState()
    tools = _tools(memory)
    added = _add(tools, state, "预算 2000 以内", "session")

    body, _ = _call(tools["delete_preference"], state, preference_id=added["preference"]["id"])

    assert body["ok"] is True
    assert body["preference"]["id"] == added["preference"]["id"]
    assert body["counts"]["session"] == 0


def test_delete_preference_tool_reports_missing_id(tmp_path: Path) -> None:
    body, _ = _call(
        _tools(_memory(tmp_path))["delete_preference"],
        AgentState(),
        preference_id="pref_missing",
    )
    assert body == {"ok": False, "error": "偏好不存在"}


def test_add_preference_tool_rejects_a_question(tmp_path: Path) -> None:
    body, tool_state = _call(
        _tools(_memory(tmp_path))["add_preference"],
        AgentState(),
        text="我预算多少？",
        scope="session",
    )
    assert tool_state == ToolResultState.SUCCESS
    assert body["ok"] is False
    assert "偏好" in body["error"]


def test_a_dislike_must_carry_material_tags(tmp_path: Path) -> None:
    body, _ = _call(
        _tools(_memory(tmp_path))["add_preference"],
        AgentState(),
        text="不要塑料的",
        scope="long_term",
        kind="dislike",
    )
    assert body["ok"] is False
    assert "material_tags" in body["error"]


def test_add_preference_tool_reports_embedding_failure_without_raising(
    tmp_path: Path,
) -> None:
    memory = _memory(tmp_path, FakeEmbedder(fail=True))
    state = AgentState()
    tools = _tools(memory)
    staged, _ = _call(tools["add_preference"], state, text="预算 500 以内", scope="long_term")

    body, tool_state = _call(
        tools["confirm_preference"], state, operation_id=staged["operation"]["id"]
    )
    assert tool_state == ToolResultState.SUCCESS
    assert body["ok"] is False
    assert body["retryable"] is True


def test_add_long_term_tool_surfaces_similar_records(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    state = AgentState()
    tools = _tools(memory)
    _add(tools, state, "预算 500", "long_term")

    body = _add(tools, state, "预算 600", "long_term")
    assert body["ok"] is True
    assert body["similar"][0]["text"] == "预算 500"
    assert 0.0 <= body["similar"][0]["score"] <= 1.0


def test_tool_failure_maps_retryable_flag(tmp_path: Path, monkeypatch) -> None:
    memory = _memory(tmp_path)
    state = AgentState()
    tools = _tools(memory)
    staged, _ = _call(tools["add_preference"], state, text="预算 500", scope="long_term")

    async def _boom(*args, **kwargs):
        raise PreferenceWriteError("写不进去", retryable=False)

    monkeypatch.setattr(memory, "_add_long_term", _boom)
    body, _ = _call(tools["confirm_preference"], state, operation_id=staged["operation"]["id"])
    assert body == {"ok": False, "error": "写不进去", "retryable": False}
