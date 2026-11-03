"""S6: SessionRepository snapshot save/load (offline, tmp_path filesystem)."""

import json

import pytest
from agentscope.message import Msg, TextBlock, UserMsg
from agentscope.state import AgentState

from harbor.agents.cards import ProductCard
from harbor.agents.context_governance import GOVERNANCE_STATE_KEY
from harbor.features.chat.repository import (
    SessionRepository,
    SnapshotLoadError,
    SnapshotWriteError,
)
from harbor.features.chat.schemas import CommerceIntentResponse


def _state() -> AgentState:
    s = AgentState()
    s.context.append(
        Msg(name="user", role="user", content=[TextBlock(type="text", text="预算300给猫买饮水器")])
    )
    s.context.append(
        Msg(
            name="purchase_agent",
            role="assistant",
            content=[TextBlock(type="text", text="命中2款")],
        )
    )
    return s


def test_save_then_load_roundtrips_state(tmp_path):
    repo = SessionRepository(tmp_path)
    repo.save(("u1", "s1"), _state())
    loaded = repo.load(("u1", "s1"))
    assert loaded is not None
    assert len(loaded.context) == 2
    assert loaded.context[0].role == "user"
    assert loaded.context[0].get_text_content() == "预算300给猫买饮水器"


def test_load_returns_none_when_missing(tmp_path):
    assert SessionRepository(tmp_path).load(("u1", "s1")) is None


def test_load_raises_on_corrupt_snapshot(tmp_path):
    """S6-3: a hand-broken snapshot must fail loudly, never look like a new session."""
    repo = SessionRepository(tmp_path)
    path = repo.snapshot_path(("u1", "s1"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(SnapshotLoadError):
        repo.load(("u1", "s1"))


def test_load_keeps_corrupt_file_for_inspection(tmp_path):
    repo = SessionRepository(tmp_path)
    path = repo.snapshot_path(("u1", "s1"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(SnapshotLoadError):
        repo.load(("u1", "s1"))
    assert path.exists()  # 不静默删除，留给人排查


def test_load_raises_on_unknown_schema_version(tmp_path):
    repo = SessionRepository(tmp_path)
    path = repo.snapshot_path(("u1", "s1"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 999, "state": {}}), encoding="utf-8")
    with pytest.raises(SnapshotLoadError):
        repo.load(("u1", "s1"))


def test_load_raises_when_state_payload_is_invalid(tmp_path):
    repo = SessionRepository(tmp_path)
    path = repo.snapshot_path(("u1", "s1"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "state": {"context": "not-a-list"}}), encoding="utf-8"
    )
    with pytest.raises(SnapshotLoadError):
        repo.load(("u1", "s1"))


def _envelope(buyer: str, session: str, at: str, turns: int) -> dict:
    return {
        "schema_version": 1,
        "buyer_id": buyer,
        "shopping_session_id": session,
        "last_active_at": at,
        "turn_count": turns,
        "state": AgentState().model_dump(mode="json"),
    }


def _write(repo: SessionRepository, key, doc: dict) -> None:
    path = repo.snapshot_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_list_sessions_filters_by_buyer_and_sorts_desc(tmp_path):
    """S6-5: only the buyer's sessions, newest last_active_at first."""
    repo = SessionRepository(tmp_path)
    _write(repo, ("u1", "old"), _envelope("u1", "old", "2026-11-01T10:00:00+00:00", 1))
    _write(repo, ("u1", "new"), _envelope("u1", "new", "2026-11-03T10:00:00+00:00", 3))
    _write(repo, ("u1", "mid"), _envelope("u1", "mid", "2026-11-02T10:00:00+00:00", 2))
    _write(repo, ("u2", "other"), _envelope("u2", "other", "2026-11-04T10:00:00+00:00", 9))

    assert [s.shopping_session_id for s in repo.list_sessions("u1")] == ["new", "mid", "old"]


def test_list_sessions_reports_turn_count(tmp_path):
    repo = SessionRepository(tmp_path)
    repo.save(("u1", "s1"), _state())
    [summary] = repo.list_sessions("u1")
    assert summary.turn_count == 1  # _state() 里恰好一条 role=="user"
    assert summary.last_active_at is not None


def test_list_sessions_empty_when_no_snapshots(tmp_path):
    assert SessionRepository(tmp_path).list_sessions("u1") == []


def test_snapshot_paths_are_isolated_per_key(tmp_path):
    repo = SessionRepository(tmp_path)
    assert repo.snapshot_path(("u1", "s1")) != repo.snapshot_path(("u2", "s1"))


def test_save_leaves_no_tmp_file(tmp_path):
    repo = SessionRepository(tmp_path)
    repo.save(("u1", "s1"), _state())
    assert list(tmp_path.rglob("*.tmp")) == []


def test_save_raises_snapshot_write_error_when_unwritable(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    repo = SessionRepository(blocker)
    with pytest.raises(SnapshotWriteError):
        repo.save(("u1", "s1"), _state())


def test_append_journal_writes_two_lines(tmp_path):
    repo = SessionRepository(tmp_path)
    response = CommerceIntentResponse(final_text="命中1条", cards=[])
    repo.append_journal(("u1", "s1"), "预算300给猫买饮水器", response, turn=1)
    lines = (tmp_path / "journal.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    first, second = json.loads(lines[0]), json.loads(lines[1])
    assert first["role"] == "user" and first["text"] == "预算300给猫买饮水器"
    assert first["turn"] == 1 and first["buyer_id"] == "u1" and first["shopping_session_id"] == "s1"
    assert second["role"] == "assistant" and second["text"] == "命中1条"
    assert "ts" in first and "ts" in second


def test_append_journal_appends_without_overwriting(tmp_path):
    repo = SessionRepository(tmp_path)
    repo.append_journal(
        ("u1", "s1"), "q1", CommerceIntentResponse(final_text="a1", cards=[]), turn=1
    )
    repo.append_journal(
        ("u1", "s1"), "q2", CommerceIntentResponse(final_text="a2", cards=[]), turn=2
    )
    lines = (tmp_path / "journal.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 4


def test_journal_roundtrips_chinese_text(tmp_path):
    repo = SessionRepository(tmp_path)
    repo.append_journal(
        ("u1", "s1"),
        "预算300元给猫买饮水器",
        CommerceIntentResponse(final_text="命中2款饮水器", cards=[]),
        turn=1,
    )
    lines = (tmp_path / "journal.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert json.loads(lines[0])["text"] == "预算300元给猫买饮水器"
    assert json.loads(lines[1])["text"] == "命中2款饮水器"


def test_append_journal_includes_cards(tmp_path):
    repo = SessionRepository(tmp_path)
    card = ProductCard(
        title="猫饮水器",
        brand="PetGo",
        category="母婴宠物",
        price_major=200.0,
        currency="CNY",
        weight_kg=1.0,
        sku_id="X1-S1",
    )
    repo.append_journal(
        ("u1", "s1"), "q", CommerceIntentResponse(final_text="命中1条", cards=[card]), turn=1
    )
    second = json.loads(
        (tmp_path / "journal.jsonl").read_text(encoding="utf-8").strip().split("\n")[1]
    )
    assert second["cards"][0]["sku_id"] == "X1-S1"


def test_save_uses_the_recorded_turn_count_when_context_was_compressed(tmp_path) -> None:
    """S7 compression shrinks ``context``; the turn counter must not reset."""
    repo = SessionRepository(tmp_path)
    state = AgentState()
    state.context = [UserMsg(name="user", content="only the retained turn")]
    state.middle_context[GOVERNANCE_STATE_KEY] = {"turn_count": 7}

    repo.save(("u1", "s1"), state)

    document = json.loads(repo.snapshot_path(("u1", "s1")).read_text(encoding="utf-8"))
    assert document["turn_count"] == 7
