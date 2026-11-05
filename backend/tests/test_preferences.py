"""S8: preference memory — turn / session scopes (no database, no network)."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from agentscope.state import AgentState

from harbor.agents.preferences import (
    PREFERENCES_STATE_KEY,
    Preference,
    PreferenceMemory,
    PreferenceRejected,
    PreferenceWriteError,
    merged_session_items,
)
from harbor.features.memory.sqlite_store import SqliteMemoryStore


def _memory() -> PreferenceMemory:
    return PreferenceMemory(buyer_id=None, store=None, embedder=None, vector_model="m1")


def _state() -> AgentState:
    return AgentState()


def test_turn_scope_dies_at_next_begin_turn() -> None:
    memory = _memory()
    state = _state()

    async def body():
        await memory.begin_turn(state)
        added = await memory.add(state, "turn", "这次先看 2000 以内的")
        before = memory.turn_items()
        await memory.begin_turn(state)
        return added, before, memory.turn_items()

    added, before, after = asyncio.run(body())
    assert [item.text for item in before] == ["这次先看 2000 以内的"]
    assert added.preference.scope == "turn"
    assert after == []


def test_session_scope_survives_begin_turn_and_snapshot() -> None:
    memory = _memory()
    state = _state()

    async def body():
        await memory.begin_turn(state)
        await memory.add(state, "session", "只考虑母婴宠物类目")
        await memory.begin_turn(state)
        return memory.session_items(state)

    items = asyncio.run(body())
    assert [item.text for item in items] == ["只考虑母婴宠物类目"]

    # S6 persists ``state.model_dump()``; the preference must round-trip verbatim.
    restored = AgentState.model_validate(state.model_dump(mode="json"))
    assert [item.text for item in memory.session_items(restored)] == ["只考虑母婴宠物类目"]


def test_add_rejects_empty_long_and_question_text() -> None:
    memory = _memory()
    state = _state()

    async def body():
        await memory.begin_turn(state)
        for text in ("", "   ", "我预算多少？", "呢", "x" * 201):
            with pytest.raises(PreferenceRejected):
                await memory.add(state, "session", text)

    asyncio.run(body())
    assert memory.session_items(state) == []


def test_add_dedupes_same_semantic_key_within_one_scope() -> None:
    memory = _memory()
    state = _state()

    async def body():
        await memory.begin_turn(state)
        first = await memory.add(state, "session", "预算 2000 以内")
        second = await memory.add(state, "session", "第14轮：预算 2000 以内")
        return first, second

    first, second = asyncio.run(body())
    assert second.duplicate is True
    assert second.preference.id == first.preference.id
    assert [item.text for item in memory.session_items(state)] == ["预算 2000 以内"]


def test_dedupe_does_not_cross_scopes() -> None:
    memory = _memory()
    state = _state()

    async def body():
        await memory.begin_turn(state)
        await memory.add(state, "turn", "预算 2000 以内")
        return await memory.add(state, "session", "预算 2000 以内")

    session_item = asyncio.run(body())
    assert session_item.duplicate is False
    assert len(memory.turn_items()) == 1
    assert len(memory.session_items(state)) == 1


def test_update_changes_text_and_keeps_id_and_scope() -> None:
    memory = _memory()
    state = _state()

    async def body():
        await memory.begin_turn(state)
        added = await memory.add(state, "session", "预算 2000 以内")
        return added, await memory.update(state, added.preference.id, "预算 1500 以内")

    added, updated = asyncio.run(body())
    assert updated is not None
    assert updated.id == added.preference.id
    assert updated.scope == "session"
    assert updated.text == "预算 1500 以内"
    assert [item.text for item in memory.session_items(state)] == ["预算 1500 以内"]


def test_update_rejects_invalid_text() -> None:
    memory = _memory()
    state = _state()

    async def body():
        await memory.begin_turn(state)
        added = await memory.add(state, "session", "预算 2000 以内")
        with pytest.raises(PreferenceRejected):
            await memory.update(state, added.preference.id, "多少？")

    asyncio.run(body())


def test_delete_removes_by_id_across_scopes() -> None:
    memory = _memory()
    state = _state()

    async def body():
        await memory.begin_turn(state)
        turn_item = await memory.add(state, "turn", "这次看 2000 的")
        session_item = await memory.add(state, "session", "只考虑母婴宠物")
        deleted_turn = await memory.delete(state, turn_item.preference.id)
        deleted_session = await memory.delete(state, session_item.preference.id)
        return deleted_turn, deleted_session

    deleted_turn, deleted_session = asyncio.run(body())
    assert deleted_turn is not None and deleted_turn.scope == "turn"
    assert deleted_session is not None and deleted_session.scope == "session"
    assert memory.turn_items() == []
    assert memory.session_items(state) == []


def test_update_and_delete_unknown_id_return_none() -> None:
    memory = _memory()
    state = _state()

    async def body():
        await memory.begin_turn(state)
        return (
            await memory.update(state, "pref_missing", "预算 1"),
            await memory.delete(state, "pref_missing"),
        )

    assert asyncio.run(body()) == (None, None)


def test_session_items_ignore_malformed_state_entries() -> None:
    state = _state()
    state.middle_context[PREFERENCES_STATE_KEY] = {
        "items": [
            {"id": "pref_1", "scope": "session", "text": "有效"},
            {"id": "pref_2"},
            "not-a-dict",
        ]
    }
    items = _memory().session_items(state)
    assert [item.text for item in items] == ["有效"]


def test_ids_are_unique_and_stamped() -> None:
    memory = _memory()
    state = _state()

    async def body():
        await memory.begin_turn(state)
        return [await memory.add(state, "session", f"偏好 {i}") for i in range(5)]

    results = asyncio.run(body())
    ids = [result.preference.id for result in results]
    assert len(set(ids)) == 5
    assert all(item.startswith("pref_") for item in ids)
    for result in results:
        created = datetime.fromisoformat(result.preference.created_at)
        assert created.tzinfo == UTC


# ---------------------------------------------------------------------------
# long-term scope: SQLite + embeddings
# ---------------------------------------------------------------------------


class FakeEmbedder:
    """Deterministic vectors; counts calls so we can prove the duplicate path."""

    def __init__(self, vectors: dict[str, list[float]] | None = None, fail: bool = False):
        self.calls = 0
        self.vectors = vectors or {}
        self.fail = fail

    def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("embedding endpoint down")
        return [self.vectors.get(text, [1.0, 0.0]) for text in texts]


def _long_memory(
    tmp_path: Path, embedder: FakeEmbedder, *, buyer_id: str | None = "b1"
) -> tuple[PreferenceMemory, SqliteMemoryStore]:
    store = SqliteMemoryStore(tmp_path / "memory.sqlite3")
    memory = PreferenceMemory(buyer_id=buyer_id, store=store, embedder=embedder, vector_model="m1")
    return memory, store


def _add_long(memory: PreferenceMemory, state: AgentState, text: str):
    return asyncio.run(memory.add(state, "long_term", text))


def test_long_term_add_writes_text_and_vector(tmp_path: Path) -> None:
    embedder = FakeEmbedder()
    memory, store = _long_memory(tmp_path, embedder)
    state = _state()
    asyncio.run(memory.begin_turn(state))

    result = _add_long(memory, state, "预算 500 以内")

    assert result.preference.scope == "long_term"
    assert embedder.calls == 1
    record = asyncio.run(store.get(buyer_id="b1", memory_id=result.preference.id))
    assert record is not None
    assert record.text == "预算 500 以内"
    assert record.vector == [1.0, 0.0]
    assert [item.id for item in memory.long_term_items()] == [result.preference.id]
    assert memory.long_term_total() == 1


def test_long_term_add_reuses_duplicate_without_embedding(tmp_path: Path) -> None:
    embedder = FakeEmbedder()
    memory, _ = _long_memory(tmp_path, embedder)
    state = _state()
    asyncio.run(memory.begin_turn(state))

    first = _add_long(memory, state, "预算 500 以内")
    second = _add_long(memory, state, "第3轮：预算 500 以内")

    assert second.duplicate is True
    assert second.preference.id == first.preference.id
    assert embedder.calls == 1


def test_two_sessions_of_one_buyer_do_not_duplicate_a_long_term_preference(
    tmp_path: Path,
) -> None:
    """Each session caches long-term memory at ``begin_turn``; one may be stale."""
    embedder = FakeEmbedder()
    store = SqliteMemoryStore(tmp_path / "memory.sqlite3")
    session_a = PreferenceMemory(buyer_id="b1", store=store, embedder=embedder, vector_model="m1")
    session_b = PreferenceMemory(buyer_id="b1", store=store, embedder=embedder, vector_model="m1")
    state_a, state_b = _state(), _state()
    asyncio.run(session_a.begin_turn(state_a))
    asyncio.run(session_b.begin_turn(state_b))  # both caches are empty

    async def body():
        first = await session_a.add(state_a, "long_term", "预算 500 以内")
        second = await session_b.add(state_b, "long_term", "第4轮：预算 500 以内")
        return first, second, await store.list(buyer_id="b1")

    first, second, records = asyncio.run(body())
    assert second.duplicate is True
    assert second.preference.id == first.preference.id
    assert [record.text for record in records] == ["预算 500 以内"]
    assert embedder.calls == 1, "the stale session should not even embed"


def test_long_term_add_returns_similar_records(tmp_path: Path) -> None:
    embedder = FakeEmbedder({"预算 500": [1.0, 0.0], "预算 600": [0.98, 0.2]})
    memory, _ = _long_memory(tmp_path, embedder)
    state = _state()
    asyncio.run(memory.begin_turn(state))

    first = _add_long(memory, state, "预算 500")
    second = _add_long(memory, state, "预算 600")

    assert second.similar, "a near-duplicate should be surfaced to the model"
    similar_id, score = second.similar[0]
    assert similar_id.id == first.preference.id
    assert score == pytest.approx(0.98, abs=1e-2)


def test_long_term_embedding_failure_writes_nothing(tmp_path: Path) -> None:
    embedder = FakeEmbedder(fail=True)
    memory, store = _long_memory(tmp_path, embedder)
    state = _state()
    asyncio.run(memory.begin_turn(state))

    with pytest.raises(PreferenceWriteError):
        _add_long(memory, state, "预算 500 以内")

    assert asyncio.run(store.list(buyer_id="b1")) == []
    assert memory.long_term_items() == []


def test_long_term_add_without_identity_is_rejected(tmp_path: Path) -> None:
    embedder = FakeEmbedder()
    memory, _ = _long_memory(tmp_path, embedder, buyer_id=None)
    state = _state()
    asyncio.run(memory.begin_turn(state))

    with pytest.raises(PreferenceWriteError) as excinfo:
        _add_long(memory, state, "预算 500 以内")
    assert excinfo.value.retryable is False


def test_long_term_survives_a_new_memory_instance(tmp_path: Path) -> None:
    embedder = FakeEmbedder()
    memory, _ = _long_memory(tmp_path, embedder)
    state = _state()
    asyncio.run(memory.begin_turn(state))
    _add_long(memory, state, "只考虑母婴宠物")

    same_store = SqliteMemoryStore(tmp_path / "memory.sqlite3")
    reopened = PreferenceMemory(
        buyer_id="b1", store=same_store, embedder=embedder, vector_model="m1"
    )
    asyncio.run(reopened.begin_turn(state))
    assert [item.text for item in reopened.long_term_items()] == ["只考虑母婴宠物"]
    assert reopened.long_term_total() == 1


def test_long_term_update_rewrites_text_and_vector(tmp_path: Path) -> None:
    embedder = FakeEmbedder({"旧预算": [1.0, 0.0], "新预算": [0.0, 1.0]})
    memory, store = _long_memory(tmp_path, embedder)
    state = _state()
    asyncio.run(memory.begin_turn(state))
    added = _add_long(memory, state, "旧预算")

    updated = asyncio.run(memory.update(state, added.preference.id, "新预算"))

    assert updated is not None and updated.text == "新预算"
    record = asyncio.run(store.get(buyer_id="b1", memory_id=added.preference.id))
    assert record is not None
    assert record.vector == [0.0, 1.0]
    assert [item.text for item in memory.long_term_items()] == ["新预算"]


def test_long_term_update_with_same_text_skips_embedding(tmp_path: Path) -> None:
    embedder = FakeEmbedder()
    memory, _ = _long_memory(tmp_path, embedder)
    state = _state()
    asyncio.run(memory.begin_turn(state))
    added = _add_long(memory, state, "预算 500 以内")

    asyncio.run(memory.update(state, added.preference.id, "预算 500 以内"))
    assert embedder.calls == 1


def test_long_term_delete_removes_the_row(tmp_path: Path) -> None:
    embedder = FakeEmbedder()
    memory, store = _long_memory(tmp_path, embedder)
    state = _state()
    asyncio.run(memory.begin_turn(state))
    added = _add_long(memory, state, "预算 500 以内")

    deleted = asyncio.run(memory.delete(state, added.preference.id))

    assert deleted is not None and deleted.id == added.preference.id
    assert asyncio.run(store.list(buyer_id="b1")) == []
    assert memory.long_term_items() == []
    assert memory.long_term_total() == 0


def test_long_term_update_works_for_an_id_outside_the_cache(tmp_path: Path) -> None:
    embedder = FakeEmbedder()
    memory, store = _long_memory(tmp_path, embedder)

    async def seed() -> str:
        oldest = None
        for index in range(26):
            record = await store.add(
                buyer_id="b1", text=f"偏好 {index}", vector=[1.0, 0.0], vector_model="m1"
            )
            oldest = oldest or record.id
        return oldest

    oldest_id = asyncio.run(seed())
    state = _state()
    asyncio.run(memory.begin_turn(state))
    assert len(memory.long_term_items()) == 24
    assert oldest_id not in {item.id for item in memory.long_term_items()}

    updated = asyncio.run(memory.update(state, oldest_id, "改过的旧偏好"))
    assert updated is not None and updated.id == oldest_id
    assert memory.long_term_total() == 26


# ---------------------------------------------------------------------------
# session merge: explicit records + the S7 transcript heuristic
# ---------------------------------------------------------------------------


def _record(preference_id: str, text: str) -> Preference:
    return Preference(id=preference_id, scope="session", text=text, created_at="", updated_at="")


def test_merged_session_items_keeps_explicit_records() -> None:
    merged = merged_session_items([_record("pref_1", "只考虑母婴宠物类目")], [])

    assert merged == [("pref_1", "只考虑母婴宠物类目")]


def test_merged_session_items_adds_heuristic_only_clauses() -> None:
    merged = merged_session_items([], ["预算 600", "要轻一点"])

    assert merged == [("", "预算 600"), ("", "要轻一点")]


def test_merged_session_items_dedupes_a_restated_constraint_by_semantic_key() -> None:
    merged = merged_session_items(
        [_record("pref_1", "预算 600")],
        ["第3轮：预算 600", "预算 800"],
    )

    # The explicit record wins: it is the one that can be updated by id.
    assert merged == [("pref_1", "预算 600"), ("", "预算 800")]


def test_a_restatement_that_drops_the_pronoun_is_the_same_long_term_preference(
    tmp_path: Path,
) -> None:
    """The real model restates 「我以后只看官方店」 as 「以后只看官方店」.

    Dropping the first-person pronoun does not change what the user wants, so
    the semantic key must ignore it — otherwise S8-3's dedupe depends on the
    model copying the sentence exactly.
    """
    memory, store = _long_memory(tmp_path, FakeEmbedder())
    state = _state()

    async def body():
        await memory.begin_turn(state)
        first = await memory.add(state, "long_term", "我以后只看官方店")
        second = await memory.add(state, "long_term", "以后只看官方店")
        return first, second

    first, second = asyncio.run(body())
    assert second.duplicate is True
    assert second.preference.id == first.preference.id
    assert asyncio.run(store.count(buyer_id="b1")) == 1
