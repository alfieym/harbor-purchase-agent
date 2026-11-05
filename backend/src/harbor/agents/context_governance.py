"""S7 context governance middleware: archive pointers, validated summaries, hard gate.

AgentScope already compresses the context (summary + retained tail), but its
built-in behaviour is best-effort: the summary may invent a product, a failed
summary silently truncates, and nothing stops an oversized request from reaching
the model. This module wraps every model call and makes those four promises:

* **Archive, don't destroy.** Before the context is compressed, the full old
  messages are written to ``data/sessions/archives/<session>/<seq>.json`` and
  the summary carries an ``archive://<session>/<seq>`` pointer. The model can
  call ``read_archive``; a human can open the JSON.
* **Validate the summary against its source.** A SKU mentioned in the summary
  must exist in the compressed messages, the retained tail, or the previous
  summary; a mentioned SKU's price must match the source. Otherwise roll back.
* **Capacity is a gate.** Above ``gate_ratio * context_size`` the middleware
  forces compression; if it still does not fit, the turn fails with
  ``ContextCapacityError`` instead of sending an oversized request.
* **Protect the boundary.** The last ``protect_turns`` user turns and
  everything after them never enter the compressed region; this is verified
  after compression and rolled back if violated.

Rollback is intentionally silent: a failed summary leaves the context exactly
as it was and the gate decides what happens next (continue under the ceiling,
or fail with a capacity error).
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentscope.agent import ContextConfig
from agentscope.message import DataBlock, Msg, TextBlock, ToolCallBlock, ToolResultBlock, UserMsg
from agentscope.middleware import MiddlewareBase
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, ToolChunk

if TYPE_CHECKING:  # ``preferences`` imports this module, so keep the edge one-way
    from harbor.agents.preferences import PreferenceMemory

logger = logging.getLogger(__name__)

ARCHIVE_PREFIX = "archive://"
SKU_PATTERN = re.compile(r"P\d+-S\d+")
# The catalog's SKU ids look like ``P3472-S1``, but the *model* writes the id
# into the card, and it can choose another shape (a real run produced
# ``sku_2175_b01``). Validation therefore keys on "id-shaped tokens" rather than
# on the catalog pattern: a letter start, at least one digit, and at least one
# separator. ``P3472`` alone (no separator) is a product id, not a SKU, so it is
# deliberately not matched.
_SKU_LIKE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:[_-][A-Za-z0-9]+)+\b")
PROTECTED_FACTS_HEADER = "## 受保护事实（机器生成，未经过模型）"
PREFERENCE_PREFIX = "- 会话约束："
GOVERNANCE_STATE_KEY = "harbor.context_governance"
# Session constraints are injected into every model call, so the list is
# bounded. Dedupe runs first (by semantic key), then the cap keeps the most
# recent entries; a normal session never reaches it.
MAX_PREFERENCES = 24
# S8: the three scopes are injected *side by side*, not pre-arbitrated. The
# header tells the model that a narrower scope is a better fit for right now,
# and that a genuine conflict is its call to weigh (or to confirm with the user).
S8_PREFERENCE_HEADER = (
    "用户偏好（[本轮] 只影响这一轮，[本会话] 随会话保存，[长期] 跨会话保存。"
    "作用域越窄越贴合当下；互相冲突时你自己权衡，必要时向用户确认）："
)
_CARD_TOOL = "GenerateStructuredOutput"
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_APPENDIX_PRODUCT = re.compile(
    r"^- (?P<sku>[^\s|]+)(?: \| (?P<title>[^|]*))? \| "
    r"(?:¥(?P<price>\d+(?:\.\d+)?)(?: (?P<currency>[A-Z]{3}))?|价格未知)$"
)
# User-turn cues that carry a session constraint. The sentence is kept verbatim,
# so "预算 2000 以内" survives compression without the model paraphrasing it.
_PREFERENCE_CUES = re.compile(r"预算|以内|以下|不超过|最多|不要|避开|偏好|喜欢|轻一点|重量|材质")
# A question is not a constraint ("我预算多少？" contains 预算 but states nothing).
_QUESTION_CUES = re.compile(r"多少|吗|呢|是否|什么|哪|怎么|如何|有没有|是不是|几[个件款种]")
# Repeated constraints differ only by a conversational prefix ("第14轮：…").
# Conversational prefixes peeled off before two clauses are compared: a
# transcript clause may start with "第4轮：", and a model restating a preference
# often drops the leading "我". Neither changes what the user wants.
_PREFERENCE_PREFIX_NOISE = re.compile(
    r"^(?:(?:第\s*\d+\s*(?:轮|次)\s*[：:,，、]?\s*)|(?:(?:我|我们|咱们)\s*))+"
)
_CLAUSE_SPLIT = re.compile(r"[。！？!?\n]+")


class ContextCapacityError(Exception):
    """The context cannot be brought under the hard gate without losing facts."""

    def __init__(self, tokens: int, limit: int) -> None:
        self.tokens = tokens
        self.limit = limit
        super().__init__(f"context is {tokens} tokens, above the hard gate of {limit}")


@dataclass(frozen=True)
class ProductFact:
    """The facts about one recommended product that must survive compression."""

    sku_id: str
    title: str = ""
    price_major: float | None = None
    currency: str | None = None


@dataclass
class ArchiveRecord:
    """One archive batch written by the offloader during a compression."""

    archive_id: str
    session_id: str
    created_at: str
    messages: list[Msg]


# ---------------------------------------------------------------------------
# archive storage
# ---------------------------------------------------------------------------


def _safe_component(session_id: str) -> str:
    """Turn an arbitrary session id into one safe path component."""
    cleaned = _SAFE_COMPONENT.sub("_", session_id).strip("._")[:64]
    if cleaned != session_id:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:8]
        cleaned = f"{cleaned or 'session'}-{digest}"
    return cleaned or "session"


def _next_sequence(directory: Path) -> int:
    sequences = []
    for path in directory.glob("*.json"):
        head = path.name.split("-", 1)[0]
        if head.isdigit():
            sequences.append(int(head))
    return max(sequences, default=0) + 1


class SessionArchiveOffloader:
    """Write compressed messages to inspectable JSON files (S7).

    Implements AgentScope's ``Offloader`` protocol. ``last_batch`` is set by
    ``offload_context`` so the middleware can validate the summary against the
    exact messages that were compressed.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.last_batch: ArchiveRecord | None = None

    async def offload_context(self, session_id: str, msgs: list[Msg]) -> str:
        record = await asyncio.to_thread(self._write, session_id, "context", msgs)
        self.last_batch = record
        return record.archive_id

    async def offload_tool_result(self, session_id: str, tool_result: ToolResultBlock) -> str:
        payload = {"tool_result": tool_result.model_dump(mode="json")}
        archive_id, _ = await asyncio.to_thread(self._write_raw, session_id, "tool-result", payload)
        return archive_id

    async def offload_data_block(self, block: DataBlock) -> DataBlock:
        # This chain has no images; nothing to persist.
        return block

    def read(self, archive_id: str) -> str:
        """Return a model- and human-readable rendering of an archive file."""
        path = self._resolve(archive_id)
        if path is None or not path.exists():
            return json.dumps(
                {"error": "归档不存在或无权限", "archive_id": archive_id},
                ensure_ascii=False,
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_messages = payload.get("messages") or []
        rendered = [_render_raw_message(raw) for raw in raw_messages]
        return json.dumps(
            {
                "archive_id": archive_id,
                "created_at": payload.get("created_at"),
                "message_count": len(rendered),
                "messages": rendered,
            },
            ensure_ascii=False,
        )

    def _write(self, session_id: str, kind: str, msgs: list[Msg]) -> ArchiveRecord:
        payload = {
            "message_count": len(msgs),
            "messages": [msg.model_dump(mode="json") for msg in msgs],
        }
        archive_id, created_at = self._write_raw(session_id, kind, payload)
        return ArchiveRecord(
            archive_id=archive_id,
            session_id=session_id,
            created_at=created_at,
            messages=list(msgs),
        )

    def _write_raw(self, session_id: str, kind: str, payload: dict) -> tuple[str, str]:
        directory = self.root / _safe_component(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        sequence = _next_sequence(directory)
        archive_id = f"{ARCHIVE_PREFIX}{directory.name}/{sequence:04d}"
        created_at = datetime.now(UTC).isoformat()
        body = {
            "archive_id": archive_id,
            "session_id": session_id,
            "created_at": created_at,
            **payload,
        }
        path = directory / f"{sequence:04d}-{kind}.json"
        path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        return archive_id, created_at

    def _resolve(self, archive_id: str) -> Path | None:
        if not archive_id.startswith(ARCHIVE_PREFIX):
            return None
        session, _, sequence = archive_id[len(ARCHIVE_PREFIX) :].rpartition("/")
        if not session or not sequence.isdigit():
            return None
        directory = self.root / session
        if directory.parent != self.root:
            return None
        for kind in ("context", "tool-result"):
            candidate = directory / f"{int(sequence):04d}-{kind}.json"
            if candidate.exists():
                return candidate
        return None


def _render_raw_message(raw: dict) -> dict[str, Any]:
    content = raw.get("content")
    blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
    texts: list[str] = []
    tool_calls: list[dict] = []
    tool_results: list[dict] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            texts.append(block.get("text") or "")
        elif block.get("type") == "tool_call":
            tool_calls.append(
                {"id": block.get("id"), "name": block.get("name"), "input": block.get("input")}
            )
        elif block.get("type") == "tool_result":
            tool_results.append(
                {
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "state": block.get("state"),
                    "output": block.get("output"),
                }
            )
    return {
        "role": raw.get("role"),
        "name": raw.get("name"),
        "text": "\n".join(text for text in texts if text),
        "tool_calls": tool_calls,
        "tool_results": tool_results,
    }


# ---------------------------------------------------------------------------
# deterministic fact extraction / validation
# ---------------------------------------------------------------------------


def _summary_text(summary: str | list[TextBlock | DataBlock]) -> str:
    if isinstance(summary, str):
        return summary
    return "\n".join(getattr(block, "text", "") for block in summary)


def _call_payload(block: ToolCallBlock) -> dict:
    payload = block.input
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return {}
    return payload if isinstance(payload, dict) else {}


def _fact_from_card(card: Any) -> ProductFact | None:
    if not isinstance(card, dict):
        return None
    sku_id = card.get("sku_id")
    if not isinstance(sku_id, str) or not sku_id:
        return None
    price = card.get("price_major")
    try:
        price_major = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_major = None
    currency = card.get("currency")
    return ProductFact(
        sku_id=sku_id,
        title=str(card.get("title") or ""),
        price_major=price_major,
        currency=str(currency) if currency else None,
    )


def product_facts(messages: list[Msg]) -> list[ProductFact]:
    """Extract every recommended product from ``GenerateStructuredOutput`` cards."""
    facts: dict[str, ProductFact] = {}
    for message in messages:
        for block in message.get_content_blocks("tool_call"):
            if block.name != _CARD_TOOL:
                continue
            for card in _call_payload(block).get("cards") or []:
                fact = _fact_from_card(card)
                if fact is not None:
                    facts[fact.sku_id] = fact
        structured = getattr(message, "structured_output", None)
        if isinstance(structured, dict):
            for card in structured.get("cards") or []:
                fact = _fact_from_card(card)
                if fact is not None:
                    facts.setdefault(fact.sku_id, fact)
    return list(facts.values())


def _message_text(message: Msg) -> str:
    parts = [message.get_text_content() or ""]
    content = message.content if isinstance(message.content, list) else []
    for block in content:
        if isinstance(block, ToolCallBlock):
            parts.append(f"{block.name} {block.input}")
        elif isinstance(block, ToolResultBlock):
            parts.append(str(block.output))
    return "\n".join(part for part in parts if part)


def _messages_text(messages: list[Msg]) -> str:
    return "\n".join(_message_text(message) for message in messages)


def sku_ids_in(text: str) -> set[str]:
    """Every id-shaped token in ``text`` (letter start, a digit, a separator).

    Shape-agnostic on purpose: the ids that matter are the ones the model wrote
    into its cards, not the ones a fixed catalog regex predicts.
    """
    return {token for token in _SKU_LIKE.findall(text) if any(char.isdigit() for char in token)}


def validate_summary(
    summary: str | list[TextBlock | DataBlock],
    *,
    source: list[Msg],
    retained: list[Msg],
    previous_summary: str | list[TextBlock | DataBlock],
) -> list[str]:
    """Return the ways ``summary`` contradicts its sources (empty = faithful).

    Only inventions and price mismatches are rejected. Omissions are covered
    deterministically by :func:`append_protected_facts`, so a terse summary is
    not itself a failure (that would roll back on every compression).
    """
    text = _summary_text(summary)
    mentioned = sku_ids_in(text)
    if not mentioned:
        return []

    allowed = sku_ids_in(_messages_text(source))
    allowed |= sku_ids_in(_messages_text(retained))
    allowed |= sku_ids_in(_summary_text(previous_summary))

    violations = [f"摘要提到来源中不存在的商品 {sku}" for sku in sorted(mentioned - allowed)]

    facts: dict[str, ProductFact] = {}
    for message in retained:
        for retained_fact in product_facts([message]):
            facts.setdefault(retained_fact.sku_id, retained_fact)
    for message in source:
        for source_fact in product_facts([message]):
            facts[source_fact.sku_id] = source_fact

    numbers = {round(float(value), 2) for value in _NUMBER.findall(_SKU_LIKE.sub(" ", text))}
    for sku in sorted(mentioned):
        fact = facts.get(sku)
        if fact is None or fact.price_major is None:
            continue
        if round(fact.price_major, 2) not in numbers:
            violations.append(f"商品 {sku} 的价格与来源不一致（来源 {fact.price_major:.2f}）")
    return violations


def _preference_key(clause: str) -> str:
    """The semantic identity of a constraint, ignoring conversational noise."""
    return _PREFERENCE_PREFIX_NOISE.sub("", clause).strip(" ：:，,。.;；")


def _normalize_preferences(preferences: Iterable[str]) -> list[str]:
    """Drop questions, merge restatements, and cap the list.

    A session constraint is injected into every model call, so an unbounded list
    grows quadratically with the conversation. Dedupe by semantic key (not exact
    text: "第14轮：…预算 2000 以内" restates "预算 2000 以内"), then keep the
    most recent entries.
    """
    unique: dict[str, str] = {}
    for item in preferences:
        clause = " ".join(str(item).split())
        if not clause or len(clause) > 200 or _QUESTION_CUES.search(clause):
            continue
        unique.setdefault(_preference_key(clause), clause)
    return list(unique.values())[-MAX_PREFERENCES:]


def _with_ids(items) -> list[tuple[str, str]]:
    """``Preference`` records as ``(id, text)`` pairs for rendering."""
    return [(item.id, item.text) for item in items]


def _appendix_text(summary: str | list[TextBlock | DataBlock]) -> str:
    """The machine-generated appendix from an earlier compression, if any."""
    text = _summary_text(summary)
    if PROTECTED_FACTS_HEADER not in text:
        return ""
    return text[text.index(PROTECTED_FACTS_HEADER) :]


def facts_from_appendix(summary: str | list[TextBlock | DataBlock]) -> list[ProductFact]:
    """Recover product facts protected by an earlier compression.

    The appendix is append-only across compressions: a second compression must
    not drop a SKU that only the previous summary still carries.
    """
    facts: list[ProductFact] = []
    for line in _appendix_text(summary).splitlines():
        match = _APPENDIX_PRODUCT.match(line.strip())
        if match is None:
            continue
        price = match.group("price")
        facts.append(
            ProductFact(
                sku_id=match.group("sku"),
                title=match.group("title") or "",
                price_major=float(price) if price else None,
                currency=match.group("currency") or None,
            )
        )
    return facts


def preferences_from_appendix(summary: str | list[TextBlock | DataBlock]) -> list[str]:
    """Recover the verbatim session constraints from an earlier compression."""
    return [
        line.strip()[len(PREFERENCE_PREFIX) :]
        for line in _appendix_text(summary).splitlines()
        if line.strip().startswith(PREFERENCE_PREFIX)
    ]


def preference_facts(messages: list[Msg]) -> list[str]:
    """Extract the user's own constraint sentences, verbatim.

    Only user text blocks are scanned — tool results are not preferences. The
    clause is copied as-is (no paraphrasing), which is what S7-3 checks.
    """
    preferences: list[str] = []
    for message in messages:
        if message.role != "user":
            continue
        for clause in _CLAUSE_SPLIT.split(message.get_text_content() or ""):
            clause = " ".join(clause.split())
            if not clause or len(clause) > 200:
                continue
            if _PREFERENCE_CUES.search(clause):
                preferences.append(clause)
    return _normalize_preferences(preferences)


def append_protected_facts(
    summary: str | list[TextBlock | DataBlock],
    facts: list[ProductFact],
    preferences: Iterable[str] = (),
) -> str | list[TextBlock | DataBlock]:
    """Append a deterministic fact appendix; idempotent across compressions."""
    unique: dict[str, ProductFact] = {}
    for fact in facts:
        unique.setdefault(fact.sku_id, fact)

    lines = [PROTECTED_FACTS_HEADER]
    for fact in unique.values():
        title = f" | {fact.title}" if fact.title else ""
        if fact.price_major is None:
            price = "价格未知"
        else:
            price = f"¥{fact.price_major:.2f}"
            if fact.currency:
                price += f" {fact.currency}"
        lines.append(f"- {fact.sku_id}{title} | {price}")
    for preference in _normalize_preferences(preferences):
        lines.append(f"{PREFERENCE_PREFIX}{preference}")
    block = "\n".join(lines)

    if isinstance(summary, list):
        kept = [
            item
            for item in summary
            if not (
                isinstance(item, TextBlock)
                and item.text.lstrip().startswith(PROTECTED_FACTS_HEADER)
            )
        ]
        return [*kept, TextBlock(text=block)]

    text = summary
    if PROTECTED_FACTS_HEADER in text:
        text = text[: text.index(PROTECTED_FACTS_HEADER)].rstrip()
    return f"{text}\n\n{block}" if text else block


# ---------------------------------------------------------------------------
# middleware
# ---------------------------------------------------------------------------


def recorded_turn_count(state) -> int:
    """The durable turn counter (S7: ``context`` shrinks, this does not)."""
    bucket = state.middle_context.get(GOVERNANCE_STATE_KEY) or {}
    try:
        return int(bucket.get("turn_count", 0))
    except (TypeError, ValueError):
        return 0


def _protected_messages(context: list[Msg], turns: int) -> list[Msg]:
    """The last ``turns`` user messages and everything after them."""
    user_indexes = [index for index, msg in enumerate(context) if msg.role == "user"]
    if len(user_indexes) <= turns:
        return list(context)
    return list(context[user_indexes[-turns] :])


class ContextGovernanceMiddleware(MiddlewareBase):
    """Wrap compression and every model call with the S7 guarantees."""

    def __init__(
        self,
        *,
        offloader: SessionArchiveOffloader,
        protect_turns: int = 2,
        gate_ratio: float = 0.85,
        preferences: "PreferenceMemory | None" = None,
        log: logging.Logger | None = None,
    ) -> None:
        self._offloader = offloader
        self._protect_turns = protect_turns
        self._gate_ratio = gate_ratio
        self._preferences = preferences
        self._log = log or logger
        self._gate_error: ContextCapacityError | None = None

    async def on_reply(self, agent, input_kwargs, next_handler):
        # A capacity failure belongs to one turn; the next turn starts clean.
        self._gate_error = None
        if self._preferences is not None:
            # Turn scope dies here; session and long-term survive into the reply.
            await self._preferences.begin_turn(agent.state)
        # S6 derived ``turn_count`` from the context; compression shrinks it, so
        # the counter now lives in the middleware state (part of the snapshot).
        # Seed from the derived count for snapshots written before S7.
        bucket = agent.state.middle_context.setdefault(GOVERNANCE_STATE_KEY, {})
        if "turn_count" not in bucket:
            bucket["turn_count"] = sum(
                1 for msg in agent.state.context if getattr(msg, "role", None) == "user"
            )
        bucket["turn_count"] = int(bucket["turn_count"]) + 1
        async for event in next_handler(**input_kwargs):
            yield event

    async def on_compress_context(self, agent, input_kwargs, next_handler) -> None:
        config: ContextConfig = input_kwargs.get("context_config") or agent.context_config
        # Truncation would destroy facts. If the summary fails we roll back
        # instead, and the model-call gate decides whether the turn can go on.
        config = config.model_copy(update={"compression_fallback_to_truncation": False})
        config = await self._protect_boundary(agent, config)

        before_context = list(agent.state.context)
        before_summary = agent.state.summary
        before_tokens = await self._count_input_tokens(agent)
        started = time.perf_counter()
        self._offloader.last_batch = None

        try:
            await next_handler(**{**input_kwargs, "context_config": config})
        except Exception:  # noqa: BLE001 — every compression failure rolls back
            self._log.warning("context compression failed; rolling back", exc_info=True)
            self._restore(agent, before_context, before_summary)
            self._offloader.last_batch = None
            self._log_compression("failed", before_tokens, before_tokens, started)
            return

        record = self._offloader.last_batch
        if record is None:
            return  # below the trigger threshold: nothing was compressed

        violations: list[str] = []
        if not self._boundary_kept(before_context, agent.state.context):
            violations.append("保护边界（最近两轮/工具调用）被压进摘要")
        violations.extend(
            validate_summary(
                agent.state.summary,
                source=record.messages,
                retained=agent.state.context,
                previous_summary=before_summary,
            )
        )
        if violations:
            self._log.warning("summary rejected, rolling back: %s", "; ".join(violations))
            self._restore(agent, before_context, before_summary)
            self._offloader.last_batch = None
            self._log_compression("rolled_back", before_tokens, before_tokens, started, record)
            return

        facts = (
            product_facts(record.messages)
            + product_facts(agent.state.context)
            + facts_from_appendix(before_summary)
        )
        preferences = (
            preference_facts(record.messages)
            + preference_facts(agent.state.context)
            + preferences_from_appendix(before_summary)
        )
        agent.state.summary = append_protected_facts(agent.state.summary, facts, preferences)
        after_tokens = await self._count_input_tokens(agent)
        self._log_compression("ok", before_tokens, after_tokens, started, record)

    async def on_model_call(self, agent, input_kwargs, next_handler):
        if self._gate_error is not None:
            raise self._gate_error

        model = input_kwargs.get("current_model") or agent.model
        messages = self._with_preferences(list(input_kwargs["messages"]), agent)
        tools = input_kwargs.get("tools")
        limit = int(model.context_size * self._gate_ratio)
        tokens = await model.count_tokens(messages, tools)
        if tokens <= limit:
            return await next_handler(**{**input_kwargs, "messages": messages})

        # Over the ceiling: force a compression (trigger_ratio ~0), validate it
        # through the on_compress_context chain, then re-measure.
        force = agent.context_config.model_copy(
            update={"trigger_ratio": 0.01, "compression_fallback_to_truncation": False}
        )
        try:
            await agent.compress_context(context_config=force)
        except ContextCapacityError:
            raise
        except Exception:  # noqa: BLE001 — the re-count below is the real verdict
            self._log.warning("forced compression failed", exc_info=True)

        prepared = await agent._prepare_model_input()
        prepared["messages"] = self._with_preferences(list(prepared["messages"]), agent)
        tokens = await model.count_tokens(prepared["messages"], prepared.get("tools"))
        if tokens > limit:
            self._gate_error = ContextCapacityError(tokens=tokens, limit=limit)
            raise self._gate_error

        return await next_handler(**{**input_kwargs, **prepared})

    async def _count_input_tokens(self, agent) -> int:
        prepared = await agent._prepare_model_input()
        return await agent.model.count_tokens(prepared["messages"], prepared.get("tools"))

    def _log_compression(
        self,
        result: str,
        before_tokens: int,
        after_tokens: int,
        started: float,
        record: ArchiveRecord | None = None,
    ) -> None:
        self._log.info(
            "context compression %s: tokens %d -> %d, %.1f ms, archived %d messages%s",
            result,
            before_tokens,
            after_tokens,
            (time.perf_counter() - started) * 1000,
            len(record.messages) if record is not None else 0,
            f" ({record.archive_id})" if record is not None else "",
        )

    def _with_preferences(self, messages: list[Msg], agent) -> list[Msg]:
        """Inject verbatim session constraints as a trailing UserMsg.

        Preferences never enter the system prompt or the persisted context:
        the system prompt keeps a stable prefix for provider-side caching, and
        the injected message is rebuilt for each model call.

        With S8 memory wired, the three scopes are rendered side by side (with
        their ids, so the model can update or delete them). The S7 heuristic
        remains the fallback for sessions whose preferences only exist in the
        transcript — a snapshot written before S8, or a store that is down.
        """
        if self._preferences is not None:
            scoped = self._scoped_preference_block(agent.state)
            pending = self._pending_block(agent.state)
            if scoped or pending:
                content = "\n\n".join(part for part in (scoped, pending) if part)
                return [*messages, UserMsg(name="user", content=content)]
        preferences = _normalize_preferences(
            preference_facts(agent.state.context) + preferences_from_appendix(agent.state.summary)
        )
        if not preferences:
            return messages
        body = "\n".join(
            [
                "<system-info>会话偏好（用户原话，逐字保留）：",
                *(f"- {preference}" for preference in preferences),
                "</system-info>",
            ]
        )
        return [*messages, UserMsg(name="user", content=body)]

    def _scoped_preference_block(self, state) -> str:
        """Render turn → session → long-term, skipping empty scopes.

        Each section is capped independently: a crowded long-term list must not
        crowd out the constraint the user stated this turn. Returns ``""`` when
        all three scopes are empty, so the caller can fall back to S7.
        """
        assert self._preferences is not None
        # Local import: ``preferences`` imports this module for the shared text
        # helpers, so a module-level import here would be circular.
        from harbor.agents.preferences import merged_session_items

        heuristic = _normalize_preferences(
            preference_facts(state.context) + preferences_from_appendix(state.summary)
        )
        sections = (
            ("本轮", _with_ids(self._preferences.turn_items("preference"))),
            (
                "本会话",
                merged_session_items(
                    self._preferences.session_items(state, "preference"), heuristic
                ),
            ),
            # Long-term lines carry the record's version: an update without it
            # cannot be checked for staleness (S8-4).
            (
                "长期",
                [
                    (item.id, f"{item.text}（v{item.version}）")
                    for item in self._preferences.long_term_items("preference")
                ],
            ),
        )
        lines = [
            f"[{label}] {identifier} {text}" if identifier else f"[{label}] {text}"
            for label, items in sections
            for identifier, text in items[:MAX_PREFERENCES]
        ]
        line = self._dislike_lines(state)
        lines.extend(line)
        if not lines:
            return ""
        return "\n".join([f"<system-info>{S8_PREFERENCE_HEADER}", *lines, "</system-info>"])

    def _dislike_lines(self, state) -> list[str]:
        """Materials the user rejected, tagged so the reason is not lost."""
        assert self._preferences is not None
        collections = (
            ("本轮·不喜欢", self._preferences.turn_items("dislike")),
            ("本会话·不喜欢", self._preferences.session_items(state, "dislike")),
            ("长期·不喜欢", self._preferences.long_term_items("dislike")),
        )
        lines: list[str] = []
        for label, items in collections:
            for item in items[:MAX_PREFERENCES]:
                tags = f"（材质：{'、'.join(item.material_tags)}）" if item.material_tags else ""
                lines.append(f"[{label}] {item.id} {item.text}{tags}")
        return lines

    def _pending_block(self, state) -> str:
        """Unconfirmed writes: named every turn until the user answers (S8-6).

        The exact phrase 「还有待确认的记忆操作」 is the acceptance signal, and
        the instruction tells the model the two tools that resolve it.
        """
        assert self._preferences is not None
        pending = self._preferences.pending_items(state)
        if not pending:
            return ""
        lines = [
            "<system-info>还有待确认的记忆操作（尚未写入）：",
            *(
                f"[待确认] {item.id} scope={item.scope} {item.text}"
                + (f"（材质：{'、'.join(item.material_tags)}）" if item.material_tags else "")
                for item in pending[:MAX_PREFERENCES]
            ),
            "先向用户确认；同意后调用 confirm_preference，拒绝则 cancel_preference。",
            "</system-info>",
        ]
        return "\n".join(lines)

    @property
    def _gate_tripped(self) -> bool:
        return self._gate_error is not None

    async def _protect_boundary(self, agent, config: ContextConfig) -> ContextConfig:
        """Raise ``reserve_ratio`` so the protected tail cannot be compressed."""
        protected = _protected_messages(list(agent.state.context), self._protect_turns)
        if not protected:
            return config
        prepared = await agent._prepare_model_input()
        tools = prepared.get("tools")
        full_tokens = await agent.model.count_tokens(prepared["messages"], tools)
        context_tokens = await agent.model.count_tokens(agent.state.context, tools)
        system_tokens = max(0, full_tokens - context_tokens)
        tail_tokens = await agent.model.count_tokens(protected, tools)
        needed = (system_tokens + tail_tokens) / agent.model.context_size
        # Keep reserve below the *real* trigger so the built-in split still has
        # something to compress. The forced-compression path passes a synthetic
        # trigger of 0.01, so compare against the agent's configured trigger.
        trigger = max(config.trigger_ratio, agent.context_config.trigger_ratio)
        reserve = min(max(config.reserve_ratio, needed), trigger - 0.01)
        return config.model_copy(update={"reserve_ratio": max(config.reserve_ratio, reserve)})

    def _boundary_kept(self, before_context: list[Msg], after_context: list[Msg]) -> bool:
        protected = _protected_messages(before_context, self._protect_turns)
        if not protected:
            return True
        after_by_id = {message.id: message for message in after_context}
        for message in protected:
            kept = after_by_id.get(message.id)
            if kept is None:
                return False
            # A boundary message can be split block-by-block; make sure the
            # protected ones kept every block (tool call, result, text).
            kept_block_ids = {block.id for block in kept.get_content_blocks()}
            if any(block.id not in kept_block_ids for block in message.get_content_blocks()):
                return False
        return True

    @staticmethod
    def _restore(agent, context: list[Msg], summary) -> None:
        agent.state.context = context
        agent.state.summary = summary


# ---------------------------------------------------------------------------
# model-facing archive lookup
# ---------------------------------------------------------------------------


def make_read_archive_tool(offloader: SessionArchiveOffloader) -> FunctionTool:
    """Read-only tool that lets the model look up a compressed batch again."""

    async def read_archive(archive_id: str) -> ToolChunk:
        """读取被压缩归档的历史消息。

        Args:
            archive_id: 摘要中出现的 ``archive://...`` 指针。
        """
        rendered = await asyncio.to_thread(offloader.read, archive_id)
        return ToolChunk(content=[TextBlock(text=rendered)])

    return FunctionTool(
        read_archive,
        name="read_archive",
        description=(
            "读取被上下文压缩归档的历史消息。当摘要里提到 archive:// 指针、"
            "而你需要核对之前推荐过哪些商品、价格或工具调用时调用它。"
        ),
        is_read_only=True,
        permission=PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="归档只读",
        ),
    )
