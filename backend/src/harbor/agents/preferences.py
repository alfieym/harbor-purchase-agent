"""S8 preference memory: turn / session / long-term scopes.

The three scopes differ only in *where the record lives*:

======  ============================================  =========================
scope   storage                                        lifetime
======  ============================================  =========================
turn    ``PreferenceMemory._turn`` (process memory)   one ``agent.reply``
session ``state.middle_context`` (S6 snapshot)         the shopping session
long    SQLite ``memories`` table, keyed by buyer      forever
======  ============================================  =========================

Injection is still S7's job (a block merged into the system message and rebuilt
per model call, so the user's own message stays last); this
module only owns the records and the tool-facing operations.

Two record shapes share the table:

* ``kind='preference'`` — a constraint the user wants honoured ("预算 500 以内").
* ``kind='dislike'`` — a *material* the user does not want. The tags are kept
  structured so ``product_search`` can merge them into the hard filter in code
  instead of asking the model to remember them (S8-8).

Durable writes are two-phase: ``stage`` parks the operation in
``state.middle_context`` and ``confirm`` performs it. An operation the user never
answers is not a memory; the next turn injects 「还有待确认的记忆操作」 so the
model can ask again. ``add`` itself is the immediate write (turn scope uses it
directly, and ``confirm`` delegates to it).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, ToolChunk
from pydantic import BaseModel, Field

from harbor.agents.context_governance import _QUESTION_CUES, _preference_key
from harbor.features.memory.sqlite_store import (
    DEFAULT_LIST_LIMIT,
    MemoryLimitReached,
    MemoryRecord,
    MemoryStoreError,
    MemoryVersionConflict,
    SqliteMemoryStore,
)

PreferenceScope = Literal["turn", "session", "long_term"]
MemoryKind = Literal["preference", "dislike"]

SCOPES: tuple[PreferenceScope, ...] = ("turn", "session", "long_term")
KINDS: tuple[MemoryKind, ...] = ("preference", "dislike")
PREFERENCES_STATE_KEY = "harbor.preferences"
# Unconfirmed writes survive turns (and S6 snapshots) here, so the model can
# revisit them instead of silently losing the user's instruction.
PENDING_STATE_KEY = "harbor.pending_preferences"
# ``SessionStore`` writes the buyer id here before building the agent, so the
# factory signature (``agent_factory(state)``) never has to change.
IDENTITY_STATE_KEY = "harbor.identity"
MAX_TEXT_LENGTH = 200
# S8-5: preferences are capped so one buyer's long-term list cannot grow without
# bound. Dislikes are few and safety-relevant, so they are exempt.
MAX_LONG_TERM_PREFERENCES = 200

# The similarity call on a long-term add is advisory: it tells the model "you
# already remember something close to this", so it can update instead of adding
# a near-duplicate. Three is enough to see it without bloating the tool result.
SIMILAR_LIMIT = 3


@dataclass(frozen=True)
class Preference:
    """One preference record, addressable by ``id``."""

    id: str
    scope: PreferenceScope
    text: str
    created_at: str
    updated_at: str
    version: int = 1
    kind: MemoryKind = "preference"
    material_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class PendingPreference:
    """An add the user has not confirmed yet; not a memory until it is."""

    id: str
    scope: PreferenceScope
    text: str
    kind: MemoryKind
    material_tags: tuple[str, ...]
    created_at: str


@dataclass(frozen=True)
class AddResult:
    """What ``add`` reports back to the tool layer (and the model)."""

    preference: Preference
    duplicate: bool = False
    similar: tuple[tuple[Preference, float], ...] = ()


class Embedder(Protocol):
    """Anything that embeds a batch of texts (the catalog embedder fits)."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class PreferenceRejected(Exception):
    """The text is not a storable preference (empty, too long, or a question)."""


class PreferenceWriteError(Exception):
    """A long-term write could not be completed (embedding or SQLite failure).

    Nothing is written when this is raised: the store's transaction rolls back,
    and the embedding happens before the transaction opens.
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        self.retryable = retryable
        super().__init__(message)


class PreferenceVersionConflict(Exception):
    """``expected_version`` was stale; the current record is returned instead."""

    def __init__(self, current: Preference) -> None:
        self.current = current
        super().__init__(f"版本冲突：当前 version={current.version}")


class PreferenceLimitReached(Exception):
    """The buyer already holds the maximum number of preferences."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"记忆数量已达上限 {limit}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _clean(text: str) -> str:
    clause = " ".join(str(text).split())
    if not clause or len(clause) > MAX_TEXT_LENGTH or _QUESTION_CUES.search(clause):
        raise PreferenceRejected("不是一条可保存的偏好（空、过长或疑问句）")
    return clause


def _tags(material_tags: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(tag.strip() for tag in material_tags if tag.strip()))


def _preference_from_dict(data: object) -> Preference | None:
    if not isinstance(data, dict):
        return None
    scope = data.get("scope")
    if scope not in SCOPES:
        return None
    identifier = data.get("id")
    text = data.get("text")
    if not isinstance(identifier, str) or not isinstance(text, str) or not text:
        return None
    created_at = str(data.get("created_at") or "")
    updated_at = str(data.get("updated_at") or created_at)
    kind = data.get("kind")
    return Preference(
        id=identifier,
        scope=scope,
        text=text,
        created_at=created_at,
        updated_at=updated_at,
        version=int(data.get("version") or 1),
        kind=kind if kind in KINDS else "preference",
        material_tags=_tags(data.get("material_tags") or ()),
    )


def _pending_from_dict(data: object) -> PendingPreference | None:
    if not isinstance(data, dict):
        return None
    scope = data.get("scope")
    identifier = data.get("id")
    text = data.get("text")
    if scope not in SCOPES or not isinstance(identifier, str) or not isinstance(text, str):
        return None
    kind = data.get("kind")
    return PendingPreference(
        id=identifier,
        scope=scope,
        text=text,
        kind=kind if kind in KINDS else "preference",
        material_tags=_tags(data.get("material_tags") or ()),
        created_at=str(data.get("created_at") or ""),
    )


def _pending_to_dict(pending: PendingPreference) -> dict:
    return {
        "id": pending.id,
        "scope": pending.scope,
        "text": pending.text,
        "kind": pending.kind,
        "material_tags": list(pending.material_tags),
        "created_at": pending.created_at,
    }


def _preference_from_record(record: MemoryRecord) -> Preference:
    return Preference(
        id=record.id,
        scope="long_term",
        text=record.text,
        created_at=record.created_at,
        updated_at=record.updated_at,
        version=record.version,
        kind="dislike" if record.kind == "dislike" else "preference",
        material_tags=tuple(record.material_tags),
    )


def _as_dict(preference: Preference) -> dict:
    return {
        "id": preference.id,
        "scope": preference.scope,
        "text": preference.text,
        "created_at": preference.created_at,
        "updated_at": preference.updated_at,
        "version": preference.version,
        "kind": preference.kind,
        "material_tags": list(preference.material_tags),
    }


def _session_items(state: AgentState) -> list:
    """The live session-scope list, created on first write (mutations land here)."""
    bucket = state.middle_context.setdefault(PREFERENCES_STATE_KEY, {})
    items = bucket.setdefault("items", [])
    return items if isinstance(items, list) else []


def _session_items_readonly(state: AgentState) -> list:
    """Session records for a read; never creates anything in ``middle_context``."""
    bucket = state.middle_context.get(PREFERENCES_STATE_KEY) or {}
    items = bucket.get("items") if isinstance(bucket, dict) else None
    return list(items) if isinstance(items, list) else []


def _pending_items(state: AgentState) -> list:
    """The live pending queue, created on first stage (mutations land here)."""
    items = state.middle_context.setdefault(PENDING_STATE_KEY, [])
    return items if isinstance(items, list) else []


def _pending_items_readonly(state: AgentState) -> list:
    items = state.middle_context.get(PENDING_STATE_KEY)
    return list(items) if isinstance(items, list) else []


class PreferenceMemory:
    """Holds all three scopes and the operations the tools expose."""

    def __init__(
        self,
        *,
        buyer_id: str | None,
        store: SqliteMemoryStore | None,
        embedder: Embedder | None,
        vector_model: str = "",
    ) -> None:
        self._buyer_id = buyer_id
        self._store = store
        self._embedder = embedder
        self._vector_model = vector_model
        self._turn: dict[str, Preference] = {}
        self._long_term: list[Preference] = []
        self._long_term_total = 0

    @property
    def long_term_enabled(self) -> bool:
        return bool(self._buyer_id and self._store is not None and self._embedder is not None)

    # -- reads used by the injection middleware ---------------------------

    def turn_items(self, kind: MemoryKind | None = None) -> list[Preference]:
        return [item for item in self._turn.values() if kind is None or item.kind == kind]

    def session_items(self, state: AgentState, kind: MemoryKind | None = None) -> list[Preference]:
        parsed = (_preference_from_dict(item) for item in _session_items_readonly(state))
        return [item for item in parsed if item is not None and (kind is None or item.kind == kind)]

    def long_term_items(self, kind: MemoryKind | None = None) -> list[Preference]:
        return [item for item in self._long_term if kind is None or item.kind == kind]

    def long_term_total(self) -> int:
        """Preferences only: dislikes do not count against the cap (S8-5)."""
        return self._long_term_total

    def pending_items(self, state: AgentState) -> list[PendingPreference]:
        parsed = (_pending_from_dict(item) for item in _pending_items_readonly(state))
        return [item for item in parsed if item is not None]

    def blacklisted_material_tags(self, state: AgentState) -> tuple[str, ...]:
        """Every material the user currently dislikes, across all three scopes.

        ``product_search`` merges these into ``excluded_material_tags`` before
        the query leaves the process, so the filter holds even when the model
        forgets to mention the material (S8-8).
        """
        collections = (
            self.turn_items("dislike"),
            self.session_items(state, "dislike"),
            self.long_term_items("dislike"),
        )
        return _tags(tag for items in collections for item in items for tag in item.material_tags)

    # -- lifecycle --------------------------------------------------------

    async def begin_turn(self, state: AgentState) -> None:
        """Start a new reply: drop turn-scoped items, reload the long-term cache.

        Reloading once per turn (not per model call) keeps injection synchronous
        while still picking up writes made by another session of the same buyer.
        Pending operations are deliberately *not* cleared: an unanswered
        confirmation must survive into the next turn (S8-6).
        """
        self._turn.clear()
        if not self.long_term_enabled:
            self._long_term = []
            self._long_term_total = 0
            return
        assert self._buyer_id is not None and self._store is not None
        self._long_term = [
            _preference_from_record(record)
            for record in await self._store.list(
                buyer_id=self._buyer_id, kind=None, limit=DEFAULT_LIST_LIMIT
            )
        ]
        self._long_term_total = await self._store.count(buyer_id=self._buyer_id)

    # -- staging (two-phase write) ---------------------------------------

    async def stage(
        self,
        state: AgentState,
        scope: PreferenceScope,
        text: str,
        *,
        kind: MemoryKind = "preference",
        material_tags: Iterable[str] = (),
    ) -> PendingPreference:
        """Park an add until the user confirms. Writes nothing.

        Restating a pending operation returns the existing one, so a model that
        calls the tool twice does not produce two confirmation prompts.
        """
        if scope not in SCOPES:
            raise PreferenceRejected(f"未知作用域：{scope}")
        if kind not in KINDS:
            raise PreferenceRejected(f"未知记忆类型：{kind}")
        clause = _clean(text)
        tags = _tags(material_tags)
        for pending in self.pending_items(state):
            if (
                pending.scope == scope
                and pending.kind == kind
                and (_preference_key(pending.text) or pending.text)
                == (_preference_key(clause) or clause)
            ):
                return pending

        pending = PendingPreference(
            id=f"op_{uuid.uuid4().hex[:8]}",
            scope=scope,
            text=clause,
            kind=kind,
            material_tags=tags,
            created_at=_now(),
        )
        _pending_items(state).append(_pending_to_dict(pending))
        return pending

    async def confirm(self, state: AgentState, operation_id: str) -> AddResult | None:
        """Commit a staged add. Unknown ids return ``None``.

        The operation stays queued if the write fails — nothing was persisted,
        so the user can still confirm it later.
        """
        items = _pending_items(state)
        for index, raw in enumerate(items):
            pending = _pending_from_dict(raw)
            if pending is None or pending.id != operation_id:
                continue
            del items[index]
            try:
                return await self.add(
                    state,
                    pending.scope,
                    pending.text,
                    kind=pending.kind,
                    material_tags=pending.material_tags,
                )
            except Exception:
                items.insert(index, raw)
                raise
        return None

    async def cancel(self, state: AgentState, operation_id: str) -> PendingPreference | None:
        items = _pending_items(state)
        for index, raw in enumerate(items):
            pending = _pending_from_dict(raw)
            if pending is None or pending.id != operation_id:
                continue
            del items[index]
            return pending
        return None

    # -- operations -------------------------------------------------------

    async def add(
        self,
        state: AgentState,
        scope: PreferenceScope,
        text: str,
        *,
        kind: MemoryKind = "preference",
        material_tags: Iterable[str] = (),
    ) -> AddResult:
        if scope not in SCOPES:
            raise PreferenceRejected(f"未知作用域：{scope}")
        clause = _clean(text)
        tags = _tags(material_tags)

        existing = self._find_by_text(state, scope, clause, kind)
        if existing is not None:
            return AddResult(preference=existing, duplicate=True)

        if scope == "long_term":
            return await self._add_long_term(clause, kind, tags)

        now = _now()
        preference = Preference(
            id=f"pref_{uuid.uuid4().hex[:8]}",
            scope=scope,
            text=clause,
            created_at=now,
            updated_at=now,
            kind=kind,
            material_tags=tags,
        )
        if scope == "turn":
            self._turn[preference.id] = preference
        else:
            _session_items(state).append(_as_dict(preference))
        return AddResult(preference=preference)

    async def update(
        self,
        state: AgentState,
        preference_id: str,
        text: str,
        *,
        expected_version: int | None = None,
    ) -> Preference | None:
        clause = _clean(text)

        current = self._turn.get(preference_id)
        if current is not None:
            updated = self._replace_text(current, clause, "turn", expected_version)
            self._turn[preference_id] = updated
            return updated

        items = _session_items(state)
        for index, raw in enumerate(items):
            stored = _preference_from_dict(raw)
            if stored is None or stored.id != preference_id:
                continue
            updated = self._replace_text(stored, clause, "session", expected_version)
            items[index] = _as_dict(updated)
            return updated

        return await self._update_long_term(preference_id, clause, expected_version)

    async def delete(self, state: AgentState, preference_id: str) -> Preference | None:
        removed = self._turn.pop(preference_id, None)
        if removed is not None:
            return removed

        items = _session_items(state)
        for index, raw in enumerate(items):
            current = _preference_from_dict(raw)
            if current is None or current.id != preference_id:
                continue
            del items[index]
            return current

        return await self._delete_long_term(preference_id)

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _replace_text(
        current: Preference,
        clause: str,
        scope: PreferenceScope,
        expected_version: int | None,
    ) -> Preference:
        if expected_version is not None and current.version != expected_version:
            raise PreferenceVersionConflict(current)
        return Preference(
            id=current.id,
            scope=scope,
            text=clause,
            created_at=current.created_at,
            updated_at=_now(),
            version=current.version + 1,
            kind=current.kind,
            material_tags=current.material_tags,
        )

    def _find_by_text(
        self, state: AgentState, scope: PreferenceScope, clause: str, kind: MemoryKind
    ) -> Preference | None:
        key = _preference_key(clause)
        if scope == "turn":
            candidates = list(self._turn.values())
        elif scope == "session":
            candidates = self.session_items(state)
        else:
            candidates = self._long_term
        for candidate in candidates:
            if candidate.kind == kind and _preference_key(candidate.text) == key:
                return candidate
        return None

    # -- long-term --------------------------------------------------------

    async def _add_long_term(
        self, clause: str, kind: MemoryKind, material_tags: tuple[str, ...]
    ) -> AddResult:
        if not self.long_term_enabled:
            raise PreferenceWriteError("长期记忆未启用", retryable=False)
        key = _preference_key(clause) or clause
        stored = await self._find_long_term_in_store(key, kind)
        if stored is not None:
            return AddResult(preference=stored, duplicate=True)

        vector = await self._embed(clause)
        assert self._buyer_id is not None and self._store is not None
        try:
            record, created = await self._store.add_or_get(
                buyer_id=self._buyer_id,
                text=clause,
                vector=vector,
                vector_model=self._vector_model,
                kind=kind,
                text_key=key,
                material_tags=material_tags,
                # S8-5: the cap is enforced inside the write transaction, so a
                # burst of concurrent adds cannot overshoot it.
                max_count=MAX_LONG_TERM_PREFERENCES if kind == "preference" else None,
            )
        except MemoryVersionConflict as exc:
            raise PreferenceVersionConflict(_preference_from_record(exc.current)) from exc
        except MemoryLimitReached as exc:
            raise PreferenceLimitReached(exc.limit) from exc
        except MemoryStoreError as exc:
            raise PreferenceWriteError("偏好写入失败") from exc

        preference = _preference_from_record(record)
        if not created:
            # Another session won the race between our lookup and this write.
            return AddResult(preference=preference, duplicate=True)

        self._long_term.insert(0, preference)
        if kind == "preference":
            self._long_term_total += 1
        return AddResult(preference=preference, similar=await self._similar_to(record.id, vector))

    async def _find_long_term_in_store(self, key: str, kind: MemoryKind) -> Preference | None:
        """Dedupe against the database, not just this session's cache.

        Another session of the same buyer may have written since our last
        ``begin_turn``. Without this lookup both sessions would embed and add
        the same preference twice; ``add_or_get`` is the atomic backstop for the
        case where they write at the same moment.
        """
        assert self._buyer_id is not None and self._store is not None
        try:
            records = await self._store.list(buyer_id=self._buyer_id, kind=None, limit=None)
        except MemoryStoreError as exc:
            raise PreferenceWriteError("偏好读取失败") from exc
        for record in records:
            if record.kind == kind and (_preference_key(record.text) or record.text) == key:
                return _preference_from_record(record)
        return None

    async def _update_long_term(
        self, preference_id: str, clause: str, expected_version: int | None
    ) -> Preference | None:
        if not self.long_term_enabled:
            return None
        assert self._buyer_id is not None and self._store is not None
        try:
            current = await self._store.get(buyer_id=self._buyer_id, memory_id=preference_id)
        except MemoryStoreError as exc:
            raise PreferenceWriteError("偏好读取失败") from exc
        if current is None:
            return None
        if current.text == clause and (
            expected_version is None or current.version == expected_version
        ):
            return _preference_from_record(current)

        vector = await self._embed(clause)
        try:
            record = await self._store.update_text(
                buyer_id=self._buyer_id,
                memory_id=preference_id,
                text=clause,
                vector=vector,
                vector_model=self._vector_model,
                text_key=_preference_key(clause) or clause,
                expected_version=expected_version,
            )
        except MemoryVersionConflict as exc:
            raise PreferenceVersionConflict(_preference_from_record(exc.current)) from exc
        except MemoryStoreError as exc:
            raise PreferenceWriteError("偏好写入失败") from exc
        if record is None:
            return None

        updated = _preference_from_record(record)
        self._long_term = [
            updated if item.id == preference_id else item for item in self._long_term
        ]
        return updated

    async def _delete_long_term(self, preference_id: str) -> Preference | None:
        if not self.long_term_enabled:
            return None
        assert self._buyer_id is not None and self._store is not None
        try:
            record = await self._store.delete(buyer_id=self._buyer_id, memory_id=preference_id)
        except MemoryStoreError as exc:
            raise PreferenceWriteError("偏好删除失败") from exc
        if record is None:
            return None

        removed = _preference_from_record(record)
        self._long_term = [item for item in self._long_term if item.id != preference_id]
        if removed.kind == "preference":
            self._long_term_total = max(0, self._long_term_total - 1)
        return removed

    async def _embed(self, text: str) -> list[float]:
        """Embed before the write opens: a failure here means nothing is stored."""
        if not self.long_term_enabled:
            raise PreferenceWriteError("长期记忆未启用", retryable=False)
        assert self._embedder is not None
        try:
            vectors = await asyncio.to_thread(self._embedder.embed, [text])
        except Exception as exc:  # noqa: BLE001 — any embedder failure means "no write"
            raise PreferenceWriteError("记忆向量生成失败") from exc
        if not vectors or not vectors[0]:
            raise PreferenceWriteError("记忆向量生成失败")
        return list(vectors[0])

    async def _similar_to(
        self, exclude_id: str, vector: list[float]
    ) -> tuple[tuple[Preference, float], ...]:
        """Advisory only: a failure here must not undo a successful write."""
        assert self._buyer_id is not None and self._store is not None
        try:
            hits = await self._store.search_similar(
                buyer_id=self._buyer_id,
                vector=vector,
                limit=SIMILAR_LIMIT + 1,
                vector_model=self._vector_model,
            )
        except MemoryStoreError:
            return ()
        return tuple(
            (_preference_from_record(record), score)
            for record, score in hits
            if record.id != exclude_id
        )[:SIMILAR_LIMIT]


# ---------------------------------------------------------------------------
# model-facing tools
# ---------------------------------------------------------------------------


class AddPreferenceArgs(BaseModel):
    text: str = Field(description="用户偏好原话，逐字保留，例如「预算 2000 以内」")
    scope: Literal["turn", "session", "long_term"] = Field(
        description="作用域：turn=只影响这一轮；session=本会话；long_term=跨会话长期记住"
    )
    kind: Literal["preference", "dislike"] = Field(
        default="preference",
        description="preference=想要的约束；dislike=不想要的材质，需同时给 material_tags",
    )
    material_tags: list[str] = Field(
        default_factory=list,
        description='kind=dislike 时必填：要排除的材质标签，如 ["塑料"]',
    )


class UpdatePreferenceArgs(BaseModel):
    preference_id: str = Field(description="要修改的偏好 id，见注入列表或 add 的返回")
    text: str = Field(description="新的偏好原话")
    expected_version: int | None = Field(
        default=None,
        description="你读到这条偏好时的 version；并发修改时会返回 409 而不是覆盖新值",
    )


class DeletePreferenceArgs(BaseModel):
    preference_id: str = Field(description="要删除的偏好 id")


class ConfirmPreferenceArgs(BaseModel):
    operation_id: str = Field(description="add_preference 暂存时返回的 operation.id")


class CancelPreferenceArgs(BaseModel):
    operation_id: str = Field(description="要放弃的待确认 operation.id")


def _preference_payload(preference: Preference) -> dict:
    return {
        "id": preference.id,
        "scope": preference.scope,
        "text": preference.text,
        "kind": preference.kind,
        "material_tags": list(preference.material_tags),
        "version": preference.version,
    }


def _pending_payload(pending: PendingPreference) -> dict:
    return {
        "id": pending.id,
        "scope": pending.scope,
        "text": pending.text,
        "kind": pending.kind,
        "material_tags": list(pending.material_tags),
    }


def _counts(memory: PreferenceMemory, state: AgentState) -> dict[str, int]:
    return {
        "turn": len(memory.turn_items()),
        "session": len(memory.session_items(state)),
        "long_term": memory.long_term_total(),
    }


def _chunk(body: dict) -> ToolChunk:
    """A structured result the model can read.

    Failures use ``state=SUCCESS`` with ``ok: false`` on purpose: a preference
    that could not be saved must not fail the whole shopping turn (S5's tool
    failure contract is for ERROR states, which map to a 502).
    """
    return ToolChunk(
        content=[TextBlock(text=json.dumps(body, ensure_ascii=False))],
        state=ToolResultState.SUCCESS,
    )


def _add_body(memory: PreferenceMemory, state: AgentState, result: AddResult) -> dict:
    body: dict = {
        "ok": True,
        "preference": _preference_payload(result.preference),
        "counts": _counts(memory, state),
    }
    if result.duplicate:
        body["duplicate"] = True
    if result.similar:
        body["similar"] = [
            {"id": item.id, "text": item.text, "score": round(score, 4)}
            for item, score in result.similar
        ]
    return body


def make_preference_tools(memory: PreferenceMemory) -> list[FunctionTool]:
    """Build the preference tools bound to one session's memory."""

    async def add_preference(
        _agent_state: AgentState,
        text: str,
        scope: Literal["turn", "session", "long_term"],
        kind: Literal["preference", "dislike"] = "preference",
        material_tags: list[str] | None = None,
    ) -> ToolChunk:
        """记住一条用户偏好；session / long_term 先暂存，等用户确认。"""
        tags = list(material_tags or [])
        if kind == "dislike" and not tags:
            return _chunk({"ok": False, "error": "dislike 需要给出 material_tags"})
        if scope == "long_term" and not memory.long_term_enabled:
            # Refuse before asking the user to confirm: an operation that could
            # never be written must not enter the pending queue.
            return _chunk({"ok": False, "error": "长期记忆未启用", "retryable": False})
        try:
            if scope == "turn":
                result = await memory.add(_agent_state, scope, text, kind=kind, material_tags=tags)
                return _chunk(_add_body(memory, _agent_state, result))
            pending = await memory.stage(_agent_state, scope, text, kind=kind, material_tags=tags)
        except PreferenceRejected as exc:
            return _chunk({"ok": False, "error": str(exc)})
        except PreferenceWriteError as exc:
            return _chunk({"ok": False, "error": str(exc), "retryable": exc.retryable})
        return _chunk(
            {
                "ok": True,
                "pending": True,
                "operation": _pending_payload(pending),
                "counts": _counts(memory, _agent_state),
                "instruction": "已暂存，尚未写入。向用户确认后调用 confirm_preference。",
            }
        )

    async def confirm_preference(_agent_state: AgentState, operation_id: str) -> ToolChunk:
        """用户确认后，把暂存的偏好真正写入。"""
        try:
            result = await memory.confirm(_agent_state, operation_id)
        except PreferenceRejected as exc:
            return _chunk({"ok": False, "error": str(exc)})
        except PreferenceLimitReached as exc:
            return _chunk(
                {
                    "ok": False,
                    "code": "limit_reached",
                    "limit": exc.limit,
                    "error": f"长期偏好最多 {exc.limit} 条，请先删除不用的再新增",
                }
            )
        except PreferenceWriteError as exc:
            return _chunk({"ok": False, "error": str(exc), "retryable": exc.retryable})
        if result is None:
            return _chunk({"ok": False, "error": "没有这条待确认操作"})
        return _chunk(_add_body(memory, _agent_state, result))

    async def cancel_preference(_agent_state: AgentState, operation_id: str) -> ToolChunk:
        """放弃一条待确认的偏好，不写入。"""
        cancelled = await memory.cancel(_agent_state, operation_id)
        if cancelled is None:
            return _chunk({"ok": False, "error": "没有这条待确认操作"})
        return _chunk({"ok": True, "cancelled": _pending_payload(cancelled)})

    async def update_preference(
        _agent_state: AgentState,
        preference_id: str,
        text: str,
        expected_version: int | None = None,
    ) -> ToolChunk:
        """修改一条已记住的偏好（id 不变）。"""
        try:
            updated = await memory.update(
                _agent_state, preference_id, text, expected_version=expected_version
            )
        except PreferenceRejected as exc:
            return _chunk({"ok": False, "error": str(exc)})
        except PreferenceVersionConflict as exc:
            return _chunk(
                {
                    "ok": False,
                    "code": "version_conflict",
                    "status": 409,
                    "error": "这条偏好已被更新，你的 version 过期了；请基于 current 重新决定",
                    "current": _preference_payload(exc.current),
                }
            )
        except PreferenceWriteError as exc:
            return _chunk({"ok": False, "error": str(exc), "retryable": exc.retryable})
        if updated is None:
            return _chunk({"ok": False, "error": "偏好不存在"})
        return _chunk(
            {
                "ok": True,
                "preference": _preference_payload(updated),
                "counts": _counts(memory, _agent_state),
            }
        )

    async def delete_preference(_agent_state: AgentState, preference_id: str) -> ToolChunk:
        """删除一条已记住的偏好。"""
        try:
            removed = await memory.delete(_agent_state, preference_id)
        except PreferenceWriteError as exc:
            return _chunk({"ok": False, "error": str(exc), "retryable": exc.retryable})
        if removed is None:
            return _chunk({"ok": False, "error": "偏好不存在"})
        return _chunk(
            {
                "ok": True,
                "preference": _preference_payload(removed),
                "counts": _counts(memory, _agent_state),
            }
        )

    return [
        FunctionTool(
            add_preference,
            name="add_preference",
            description=(
                "记住一条用户偏好。用户说「以后都…」用 long_term；「这次/先」用 turn；"
                "其余默认 session。turn 立即生效；session / long_term 只暂存，"
                "必须先向用户确认，再调用 confirm_preference 才写入。"
                "用户不想要某种材质时用 kind=dislike 并给出 material_tags。"
                "同一作用域内重复的偏好不会重复添加。"
            ),
            input_schema=AddPreferenceArgs,
            is_state_injected=True,
            is_read_only=False,
            permission=_memory_permission("新增偏好"),
        ),
        FunctionTool(
            confirm_preference,
            name="confirm_preference",
            description="用户同意后，把 add_preference 暂存的偏好真正写入。",
            input_schema=ConfirmPreferenceArgs,
            is_state_injected=True,
            is_read_only=False,
            permission=_memory_permission("确认偏好"),
        ),
        FunctionTool(
            cancel_preference,
            name="cancel_preference",
            description="用户拒绝后，放弃一条待确认的偏好。",
            input_schema=CancelPreferenceArgs,
            is_state_injected=True,
            is_read_only=False,
            permission=_memory_permission("取消偏好"),
        ),
        FunctionTool(
            update_preference,
            name="update_preference",
            description=(
                "修改一条已记住的偏好。preference_id 与 version 见偏好注入列表或 "
                "add 的返回；只改文本，作用域与类型不变。要换作用域请 delete + add。"
            ),
            input_schema=UpdatePreferenceArgs,
            is_state_injected=True,
            is_read_only=False,
            permission=_memory_permission("修改偏好"),
        ),
        FunctionTool(
            delete_preference,
            name="delete_preference",
            description="删除一条已记住的偏好（也可用来撤回一条不再适用的 dislike）。",
            input_schema=DeletePreferenceArgs,
            is_state_injected=True,
            is_read_only=False,
            permission=_memory_permission("删除偏好"),
        ),
    ]


def _memory_permission(action: str) -> PermissionDecision:
    return PermissionDecision(
        behavior=PermissionBehavior.ALLOW,
        message=f"用户偏好{action}，无需确认",
    )
