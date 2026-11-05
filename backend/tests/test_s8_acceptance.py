"""S8 acceptance checklist (v2): confirmation, retraction, versioning, cap, filter.

One test per checklist item, each written against the tool surface the model
actually sees — so the test fails for a real behaviour gap, not a private API.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agentscope.state import AgentState

from harbor.agents.preferences import PreferenceMemory, make_preference_tools
from harbor.agents.tools import make_product_search_tool
from harbor.features.catalog.search.schemas import SearchResult
from harbor.features.memory.sqlite_store import SqliteMemoryStore


class FakeEmbedder:
    """No network: every text embeds to the same tiny vector."""

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [[1.0, 0.0] for _ in texts]


def _memory(tmp_path: Path, buyer: str = "u1") -> tuple[PreferenceMemory, SqliteMemoryStore]:
    store = SqliteMemoryStore(tmp_path / "memory.sqlite3")
    memory = PreferenceMemory(
        buyer_id=buyer, store=store, embedder=FakeEmbedder(), vector_model="m1"
    )
    return memory, store


def _tools(memory: PreferenceMemory) -> dict:
    return {tool.name: tool for tool in make_preference_tools(memory)}


def _call(tool, state: AgentState, **kwargs) -> dict:
    chunk = asyncio.run(tool.call(_agent_state=state, **kwargs))
    return json.loads(chunk.content[0].text)


def _state() -> AgentState:
    state = AgentState()
    return state


# ---------------------------------------------------------------------------
# S8-1 + S8-6: two-phase write, pending queue survives turns
# ---------------------------------------------------------------------------


def test_s8_1_a_long_term_add_is_written_only_after_confirmation(tmp_path: Path) -> None:
    memory, store = _memory(tmp_path)
    state = _state()
    asyncio.run(memory.begin_turn(state))
    tools = _tools(memory)

    staged = _call(tools["add_preference"], state, text="我以后只看官方店", scope="long_term")
    assert staged["ok"] is True
    assert staged["pending"] is True
    assert asyncio.run(store.list(buyer_id="u1", kind=None)) == []

    # S8-6: the unanswered operation survives into the next turn.
    asyncio.run(memory.begin_turn(state))
    pending = memory.pending_items(state)
    assert [item.text for item in pending] == ["我以后只看官方店"]

    confirmed = _call(tools["confirm_preference"], state, operation_id=pending[0].id)
    assert confirmed["ok"] is True
    assert [record.text for record in asyncio.run(store.list(buyer_id="u1"))] == [
        "我以后只看官方店"
    ]
    assert memory.pending_items(state) == []

    # A brand-new session of the same buyer sees it.
    fresh = PreferenceMemory(buyer_id="u1", store=store, embedder=FakeEmbedder(), vector_model="m1")
    asyncio.run(fresh.begin_turn(_state()))
    assert [item.text for item in fresh.long_term_items()] == ["我以后只看官方店"]


def test_s8_6_an_unconfirmed_add_can_be_cancelled(tmp_path: Path) -> None:
    memory, store = _memory(tmp_path)
    state = _state()
    asyncio.run(memory.begin_turn(state))
    tools = _tools(memory)

    staged = _call(tools["add_preference"], state, text="预算 500", scope="long_term")
    operation_id = staged["operation"]["id"]

    cancelled = _call(tools["cancel_preference"], state, operation_id=operation_id)
    assert cancelled["ok"] is True
    assert memory.pending_items(state) == []
    assert asyncio.run(store.list(buyer_id="u1", kind=None)) == []

    # A cancellation is not a write: confirming the dropped op finds nothing.
    late = _call(tools["confirm_preference"], state, operation_id=operation_id)
    assert late["ok"] is False
    assert asyncio.run(store.list(buyer_id="u1", kind=None)) == []


# ---------------------------------------------------------------------------
# S8-2: retracting a dislike
# ---------------------------------------------------------------------------


def test_s8_2_a_retracted_dislike_stops_filtering_that_material(tmp_path: Path) -> None:
    memory, _ = _memory(tmp_path)
    state = _state()
    asyncio.run(memory.begin_turn(state))
    tools = _tools(memory)

    added = asyncio.run(
        memory.add(
            state, "long_term", "以后不用避开塑料了", kind="dislike", material_tags=("塑料",)
        )
    )
    assert "塑料" in memory.blacklisted_material_tags(state)

    deleted = _call(tools["delete_preference"], state, preference_id=added.preference.id)

    assert deleted["ok"] is True
    assert "塑料" not in memory.blacklisted_material_tags(state)


def test_s8_2_a_dislike_round_trips_its_material_tags_through_sqlite(tmp_path: Path) -> None:
    memory, store = _memory(tmp_path)
    state = _state()
    asyncio.run(memory.begin_turn(state))
    asyncio.run(
        memory.add(
            state, "long_term", "不要塑料和玻璃的", kind="dislike", material_tags=("塑料", "玻璃")
        )
    )

    [record] = asyncio.run(store.list(buyer_id="u1", kind="dislike"))
    assert record.material_tags == ("塑料", "玻璃")


# ---------------------------------------------------------------------------
# S8-3: restatements collapse
# ---------------------------------------------------------------------------


def test_s8_3_the_same_sentence_twice_creates_one_record(tmp_path: Path) -> None:
    memory, store = _memory(tmp_path)
    state = _state()
    asyncio.run(memory.begin_turn(state))
    tools = _tools(memory)

    first = _call(tools["add_preference"], state, text="只看官方店", scope="long_term")
    _call(tools["confirm_preference"], state, operation_id=first["operation"]["id"])
    second = _call(tools["add_preference"], state, text="只看官方店", scope="long_term")
    duplicate = _call(tools["confirm_preference"], state, operation_id=second["operation"]["id"])

    assert duplicate["ok"] is True
    assert duplicate.get("duplicate") is True
    assert asyncio.run(store.count(buyer_id="u1")) == 1


# ---------------------------------------------------------------------------
# S8-4: stale version update
# ---------------------------------------------------------------------------


def test_s8_4_updating_with_a_stale_version_returns_409_without_overwriting(
    tmp_path: Path,
) -> None:
    memory, store = _memory(tmp_path)
    state = _state()
    asyncio.run(memory.begin_turn(state))
    tools = _tools(memory)

    added = asyncio.run(memory.add(state, "long_term", "预算 500"))
    preference_id = added.preference.id
    assert added.preference.version == 1

    fresh = _call(
        tools["update_preference"],
        state,
        preference_id=preference_id,
        text="预算 800",
        expected_version=1,
    )
    assert fresh["ok"] is True
    assert fresh["preference"]["version"] == 2

    stale = _call(
        tools["update_preference"],
        state,
        preference_id=preference_id,
        text="预算 900",
        expected_version=1,
    )
    assert stale["ok"] is False
    assert stale["code"] == "version_conflict"
    assert stale["status"] == 409
    assert stale["current"]["text"] == "预算 800"
    assert stale["current"]["version"] == 2

    stored = asyncio.run(store.get(buyer_id="u1", memory_id=preference_id))
    assert stored is not None and stored.text == "预算 800"
    assert stored.version == 2


# ---------------------------------------------------------------------------
# S8-5: 200-preference cap, dislikes exempt
# ---------------------------------------------------------------------------


def test_s8_5_the_201st_preference_is_rejected_and_dislikes_are_exempt(tmp_path: Path) -> None:
    memory, store = _memory(tmp_path)
    state = _state()
    asyncio.run(memory.begin_turn(state))
    tools = _tools(memory)

    for index in range(200):
        asyncio.run(memory.add(state, "long_term", f"偏好 {index}"))

    staged = _call(tools["add_preference"], state, text="偏好 200", scope="long_term")
    rejected = _call(tools["confirm_preference"], state, operation_id=staged["operation"]["id"])
    assert rejected["ok"] is False
    assert rejected["code"] == "limit_reached"
    assert rejected["limit"] == 200
    assert asyncio.run(store.count(buyer_id="u1")) == 200

    dislike = asyncio.run(
        memory.add(state, "long_term", "避开塑料", kind="dislike", material_tags=("塑料",))
    )
    assert dislike.preference.id.startswith("pref_")
    assert asyncio.run(store.count(buyer_id="u1", kind="dislike")) == 1


# ---------------------------------------------------------------------------
# S8-7: scope discipline
# ---------------------------------------------------------------------------


def test_s8_7_this_time_budget_stays_in_the_turn_scope(tmp_path: Path) -> None:
    memory, store = _memory(tmp_path)
    state = _state()
    asyncio.run(memory.begin_turn(state))
    tools = _tools(memory)

    body = _call(tools["add_preference"], state, text="这次预算 2000", scope="turn")

    assert body["ok"] is True
    assert body["preference"]["scope"] == "turn"
    assert "pending" not in body
    assert asyncio.run(store.list(buyer_id="u1", kind=None)) == []
    assert memory.pending_items(state) == []

    asyncio.run(memory.begin_turn(state))
    assert memory.turn_items() == []


# ---------------------------------------------------------------------------
# S8-8: material dislikes are a hard filter in code, not a model instruction
# ---------------------------------------------------------------------------


class RecordingService:
    def __init__(self) -> None:
        self.specs: list[object] = []

    def search(self, query, spec, limit=10) -> SearchResult:
        self.specs.append(spec)
        return SearchResult(hits=[])


def test_s8_8_material_dislikes_are_merged_into_the_search_spec(tmp_path: Path) -> None:
    memory, _ = _memory(tmp_path)
    state = _state()
    asyncio.run(memory.begin_turn(state))
    asyncio.run(memory.add(state, "session", "不要塑料的", kind="dislike", material_tags=("塑料",)))
    service = RecordingService()
    tool = make_product_search_tool(service, memory=memory)

    asyncio.run(tool.call(_agent_state=state, query="水杯", excluded_material_tags=[]))

    assert [spec.excluded_material_tags for spec in service.specs] == [["塑料"]]


def test_s8_8_the_model_cannot_override_a_material_dislike(tmp_path: Path) -> None:
    """A model that omits or contradicts the blacklist still gets it enforced."""
    memory, _ = _memory(tmp_path)
    state = _state()
    asyncio.run(memory.begin_turn(state))
    asyncio.run(
        memory.add(state, "long_term", "以后别买塑料的", kind="dislike", material_tags=("塑料",))
    )
    service = RecordingService()
    tool = make_product_search_tool(service, memory=memory)

    asyncio.run(tool.call(_agent_state=state, query="水杯", excluded_material_tags=["玻璃"]))

    [spec] = service.specs
    assert set(spec.excluded_material_tags) == {"塑料", "玻璃"}
