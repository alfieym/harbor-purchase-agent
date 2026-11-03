"""Durable session persistence: snapshot + journal on the filesystem (S6).

The snapshot is the source of truth for restoring a session's full ``AgentState``
(messages, tool calls, intermediate context); the journal is an append-only
one-question-one-answer log for review/evaluation. This module is pure disk I/O —
no Agent logic, no event loop — so it is fully testable offline against
``tmp_path``.
"""

import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentscope.state import AgentState

from harbor.agents.context_governance import recorded_turn_count
from harbor.features.chat.schemas import CommerceIntentResponse, SessionSummary

logger = logging.getLogger(__name__)

SessionKey = tuple[str, str]  # (buyer_id, shopping_session_id)

SCHEMA_VERSION = 1


class SnapshotWriteError(Exception):
    """The snapshot could not be serialized or written to disk (S6)."""

    def __init__(self) -> None:
        super().__init__("failed to write session snapshot")


class SnapshotLoadError(Exception):
    """An existing snapshot cannot be restored (S6-3).

    A missing file is not an error (first visit → fresh session). A file that
    exists but is corrupt, future-versioned or structurally invalid is: treating
    it as "no history" would silently clear the user's conversation.
    """

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"cannot load session snapshot {path}: {reason}")


class SessionRepository:
    """Filesystem store for per-session snapshots and a global journal."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def snapshot_path(self, key: SessionKey) -> Path:
        """Filesystem-safe per-session snapshot path.

        Session ids are arbitrary strings (may contain ``/`` or ``..``), so the
        directory name is a hash rather than the raw ids.
        """
        digest = hashlib.sha256(key[0].encode("utf-8") + b"\x00" + key[1].encode("utf-8"))
        return self._root / "snapshots" / f"{digest.hexdigest()}.json"

    def journal_path(self) -> Path:
        """The single global append-only journal file."""
        return self._root / "journal.jsonl"

    def save(self, key: SessionKey, state: AgentState) -> None:
        """Atomically write the session's full ``AgentState`` as a JSON snapshot.

        Raises ``SnapshotWriteError`` on any serialization or I/O failure: a
        snapshot the caller believes is durable but is not would silently lose
        conversation history on restart (S6's anti-goal).
        """
        try:
            doc = {
                "schema_version": SCHEMA_VERSION,
                "buyer_id": key[0],
                "shopping_session_id": key[1],
                "last_active_at": datetime.now(UTC).isoformat(),
                "turn_count": self._turn_count(state),
                "state": state.model_dump(mode="json"),
            }
            path = self.snapshot_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)  # atomic on POSIX: no half-written snapshot
        except Exception as exc:
            raise SnapshotWriteError() from exc

    def load(self, key: SessionKey) -> AgentState | None:
        """Restore the session state, or ``None`` when the snapshot is absent.

        An existing but corrupt / future-versioned / invalid snapshot raises
        ``SnapshotLoadError`` instead of returning ``None``: silently starting
        from scratch would look to the user like their history vanished (S6-3).
        The broken file is left in place for inspection.
        """
        path = self.snapshot_path(key)
        if not path.exists():
            return None
        doc = self._read_envelope(path)
        try:
            return AgentState.model_validate(doc["state"])
        except Exception as exc:
            raise SnapshotLoadError(path, f"invalid state: {exc}") from exc

    def list_sessions(self, buyer_id: str) -> list[SessionSummary]:
        """The buyer's sessions, newest ``last_active_at`` first (S6-5).

        Files that cannot be parsed at all have no recoverable ``buyer_id``, so
        they are skipped with a warning rather than failing every buyer's list.
        A parseable envelope for THIS buyer with missing/invalid fields does
        raise ``SnapshotLoadError``: that session is visibly broken.
        """
        directory = self._root / "snapshots"
        if not directory.exists():
            return []
        summaries: list[SessionSummary] = []
        for path in sorted(directory.glob("*.json")):
            try:
                doc = self._read_envelope(path)
            except SnapshotLoadError as exc:
                logger.warning("skipping unreadable snapshot in session list: %s", exc)
                continue
            if doc.get("buyer_id") != buyer_id:
                continue
            try:
                summaries.append(
                    SessionSummary(
                        shopping_session_id=doc["shopping_session_id"],
                        last_active_at=doc["last_active_at"],
                        turn_count=doc["turn_count"],
                    )
                )
            except Exception as exc:
                raise SnapshotLoadError(path, f"invalid envelope: {exc}") from exc
        summaries.sort(key=lambda s: s.last_active_at, reverse=True)
        return summaries

    @staticmethod
    def _turn_count(state: AgentState) -> int:
        # S7 compression removes old user messages from ``context``; the
        # middleware's durable counter wins when present (S6 snapshots and
        # test stubs without it fall back to the derived count).
        recorded = recorded_turn_count(state)
        if recorded:
            return recorded
        return sum(1 for msg in state.context if getattr(msg, "role", None) == "user")

    def _read_envelope(self, path: Path) -> dict[str, Any]:
        """Parse and version-check a snapshot envelope, raising on any defect."""
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SnapshotLoadError(path, f"unreadable JSON: {exc}") from exc
        if not isinstance(doc, dict):
            raise SnapshotLoadError(path, "envelope is not a JSON object")
        version = doc.get("schema_version")
        if version != SCHEMA_VERSION:
            raise SnapshotLoadError(path, f"schema_version {version!r} != {SCHEMA_VERSION}")
        return doc

    def append_journal(
        self,
        key: SessionKey,
        question: str,
        response: CommerceIntentResponse,
        turn: int,
    ) -> None:
        """Append one question + one answer (two JSONL lines) for review/eval.

        Both lines are written in a single ``write`` so a concurrent append from
        another session cannot split a question from its answer.
        """
        ts = datetime.now(UTC).isoformat()
        user_line = {
            "role": "user",
            "buyer_id": key[0],
            "shopping_session_id": key[1],
            "ts": ts,
            "turn": turn,
            "text": question,
        }
        assistant_line = {
            "role": "assistant",
            "buyer_id": key[0],
            "shopping_session_id": key[1],
            "ts": ts,
            "turn": turn,
            "text": response.final_text,
            "cards": [card.model_dump(mode="json") for card in response.cards],
        }
        payload = (
            json.dumps(user_line, ensure_ascii=False)
            + "\n"
            + json.dumps(assistant_line, ensure_ascii=False)
            + "\n"
        )
        path = self.journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(payload)
