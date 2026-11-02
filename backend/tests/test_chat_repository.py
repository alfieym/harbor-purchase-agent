"""S6: SessionRepository snapshot save/load (offline, tmp_path filesystem)."""

import json

import pytest
from agentscope.message import Msg, TextBlock
from agentscope.state import AgentState

from harbor.features.chat.repository import SessionRepository, SnapshotWriteError


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


def test_load_returns_none_when_corrupt(tmp_path):
    repo = SessionRepository(tmp_path)
    path = repo.snapshot_path(("u1", "s1"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert repo.load(("u1", "s1")) is None


def test_load_returns_none_on_unknown_schema_version(tmp_path):
    repo = SessionRepository(tmp_path)
    path = repo.snapshot_path(("u1", "s1"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 999, "state": {}}), encoding="utf-8")
    assert repo.load(("u1", "s1")) is None


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
