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
from datetime import datetime, timezone
from pathlib import Path

from agentscope.state import AgentState

from harbor.features.chat.schemas import CommerceIntentResponse

logger = logging.getLogger(__name__)

SessionKey = tuple[str, str]  # (buyer_id, shopping_session_id)

SCHEMA_VERSION = 1


class SnapshotWriteError(Exception):
    """The snapshot could not be serialized or written to disk (S6)."""

    def __init__(self) -> None:
        super().__init__("failed to write session snapshot")


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
                "updated_at": datetime.now(timezone.utc).isoformat(),
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
        """Restore the session state, or ``None`` when absent/corrupt/unknown.

        A corrupt or future-versioned snapshot must not take the service down:
        it is treated as "no history yet" (a fresh session) with a warning, never
        an exception.
        """
        path = self.snapshot_path(key)
        if not path.exists():
            return None
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc.get("schema_version") != SCHEMA_VERSION:
                logger.warning(
                    "snapshot %r has schema_version %r, expected %d; ignoring",
                    path,
                    doc.get("schema_version"),
                    SCHEMA_VERSION,
                )
                return None
            return AgentState.model_validate(doc["state"])
        except Exception as exc:  # noqa: BLE001 — corrupt data is a normal case
            logger.warning("ignoring corrupt snapshot %r: %s", path, exc)
            return None

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
        ts = datetime.now(timezone.utc).isoformat()
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
