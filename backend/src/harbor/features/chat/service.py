"""Stateful intents service: session store + one turn (S5).

One ``Agent`` holds the ReAct conversation state, so it must be isolated per
session. Sessions are keyed by ``(buyer_id, shopping_session_id)`` — the
checklist's multi-tenant contract — and create-or-resume on access. Each session
has its own ``asyncio.Lock`` (same-session requests are serialized because
``Agent.reply`` is not reentrant); sessions expire after a TTL and are evicted
LRU past a cap.
"""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from agentscope.agent import Agent
from agentscope.event import (
    EventBase,
    ModelCallEndEvent,
    ModelCallStartEvent,
    TextBlockDeltaEvent,
    ToolCallStartEvent,
)
from agentscope.message import Msg, ToolResultState, UserMsg
from agentscope.state import AgentState

from harbor.agents.cards import ProductCard, ProductCardList
from harbor.agents.context_governance import ContextCapacityError, recorded_turn_count
from harbor.agents.grounding import (
    dispatch_prices_from_context,
    ground_cards,
    search_evidence_from_context,
    tool_results_since,
)
from harbor.agents.preferences import IDENTITY_STATE_KEY
from harbor.features.chat.repository import SessionKey, SessionRepository, SnapshotWriteError
from harbor.features.chat.schemas import CommerceIntentResponse
from harbor.features.orders.schemas import OrderDraftView
from harbor.features.orders.service import OrderService
from harbor.guardrails.events import log_guard_event
from harbor.guardrails.output import GroundingFacts, OutputGuard
from harbor.guardrails.runtime import GuardRuntime
from harbor.guardrails.session import GuardSession
from harbor.observability import publish_event, span, trace_scope

logger = logging.getLogger(__name__)


ToolResultsObserver = Callable[[list[dict]], Awaitable[None]]
TextDeltaObserver = Callable[[str], Awaitable[None]]

# AgentScope asks for the structured reply through this tool; a model call
# that requests it is the one producing the answer, not narration.
STRUCTURED_OUTPUT_TOOL = "GenerateStructuredOutput"


class ModelTimeoutError(Exception):
    """The model+tool turn exceeded its budget (S5-2)."""

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(f"model turn exceeded {timeout_seconds}s")


class ToolFailureError(Exception):
    """A tool ended in ERROR state during the turn (S5-2)."""

    def __init__(self) -> None:
        super().__init__("product_search tool failed")


class NoStructuredOutputError(Exception):
    """The model answered but produced no ``ProductCardList``."""

    def __init__(self, text: str) -> None:
        self.text = text
        super().__init__("agent produced no structured output")


class ModelFailureError(Exception):
    """The model call itself failed (network / rate-limit / provider error).

    Distinct from a timeout (the turn exceeded its budget) and a tool failure
    (a tool result came back ERROR): here ``Agent.reply`` raised out of the
    model call, which must map to a structured 5xx instead of a bare 500.
    """

    def __init__(self) -> None:
        super().__init__("model call failed")


@dataclass
class Session:
    agent: Agent
    lock: asyncio.Lock
    last_active: float
    # S11: per-session loop/sequence state; ``None`` when the guard is off.
    guard: GuardSession | None = None


class SessionStore:
    """In-memory session registry with TTL + LRU eviction."""

    def __init__(
        self,
        agent_factory: Callable[[AgentState | None], Agent],
        ttl_seconds: float = 1800.0,
        max_sessions: int = 1000,
        clock: Callable[[], float] = time.monotonic,
        repository: SessionRepository | None = None,
        guard: GuardRuntime | None = None,
    ) -> None:
        self._agent_factory = agent_factory
        self._ttl = ttl_seconds
        self._max = max_sessions
        self._clock = clock
        self._repository = repository
        # S11: the process-wide guard; ``None`` keeps the pre-S11 behaviour.
        self.guard = guard
        self._sessions: dict[SessionKey, Session] = {}
        self._mutex = asyncio.Lock()

    async def get_or_create(
        self, buyer_id: str, shopping_session_id: str
    ) -> tuple[SessionKey, Session]:
        """Return the existing session for the key, or create a fresh one.

        The checklist always supplies both ids, so this is create-or-resume
        (never "unknown session"). Expired sessions are dropped on every access.
        """
        key: SessionKey = (buyer_id, shopping_session_id)
        now = self._clock()
        async with self._mutex:
            self._evict_expired(now)
            session = self._sessions.get(key)
            if session is not None:
                return key, session
            self._evict_lru()
            state = None
            if self._repository is not None:
                state = await asyncio.to_thread(self._repository.load, key)
            if state is None:
                state = AgentState()
            # S8/S10: identity travels in the state, not in the factory signature
            # (``agent_factory(state)``), so long-term memory and the order tools
            # can scope themselves without touching every existing factory.
            # The draft's uniqueness key is ``(buyer_id, shopping_session_id)``,
            # so the tool needs both ids, not just the buyer.
            state.middle_context[IDENTITY_STATE_KEY] = {
                "buyer_id": buyer_id,
                "shopping_session_id": shopping_session_id,
            }
            session = Session(
                agent=self._agent_factory(state), lock=asyncio.Lock(), last_active=now
            )
            # S11: ``build_agent`` attaches the session it wrapped the tools
            # with; a fake agent without one simply runs unguarded.
            session.guard = getattr(session.agent, "guard_session", None)
            self._sessions[key] = session
            return key, session

    async def persist(
        self,
        key: SessionKey,
        agent: Agent,
        question: str,
        response: CommerceIntentResponse,
    ) -> None:
        """Durably write the session state + journal, if a repository is wired.

        Snapshot first: a write failure must raise (and be mapped to 500 by the
        router) *before* we derive ``turn`` from ``agent.state.context`` — the
        order keeps this compatible with test stubs whose state has no ``role``.
        The journal is best-effort: a soft failure only logs, it never fails the
        turn.
        """
        repo = self._repository
        if repo is None:
            return
        try:
            await asyncio.to_thread(repo.save, key, agent.state)
        except SnapshotWriteError:
            # The router maps this to a 500, but the client only sees a clean
            # message — log the real cause (disk full / permissions / serialization)
            # server-side so ops can diagnose it.
            logger.exception("snapshot write failed for %r", key)
            raise
        turn = recorded_turn_count(agent.state) or sum(
            1 for m in agent.state.context if m.role == "user"
        )
        try:
            await asyncio.to_thread(repo.append_journal, key, question, response, turn)
        except Exception:  # noqa: BLE001 — journal is best-effort (S6)
            logger.warning("failed to append journal for %r", key, exc_info=True)

    def evict(self, key: SessionKey) -> None:
        """Drop a session from memory (rolls the next access back to its snapshot)."""
        self._sessions.pop(key, None)

    def is_cached(self, key: SessionKey, session: Session) -> bool:
        """True if ``session`` is still the live in-memory session for ``key``.

        ``run_turn`` checks this after acquiring the per-session lock: a session
        evicted by a concurrent failed turn (while this turn waited on the lock)
        must be reloaded, not reused with the failed turn's residue in its state.
        """
        return self._sessions.get(key) is session

    def touch(self, key: SessionKey) -> None:
        """Mark a session active now (extends its TTL). Atomic: no ``await``."""
        session = self._sessions.get(key)
        if session is not None:
            session.last_active = self._clock()

    def _evict_expired(self, now: float) -> None:
        for key in [k for k, s in self._sessions.items() if now - s.last_active >= self._ttl]:
            del self._sessions[key]

    def _evict_lru(self) -> None:
        while len(self._sessions) >= self._max:
            candidates = [k for k in self._sessions if not self._sessions[k].lock.locked()]
            if not candidates:
                # All sessions are in-flight; evicting one would break the
                # same-session serialization guarantee, so stop safely.
                break
            oldest = min(candidates, key=lambda k: self._sessions[k].last_active)
            del self._sessions[oldest]

    def __len__(self) -> int:
        return len(self._sessions)


async def _with_pending_draft(
    response: CommerceIntentResponse,
    order_service: OrderService,
    buyer_id: str,
    shopping_session_id: str,
) -> CommerceIntentResponse:
    """Return ``response`` plus the session's pending draft, if any (S10)."""
    try:
        draft = await order_service.get_pending_draft(
            buyer_id=buyer_id, shopping_session_id=shopping_session_id
        )
    except Exception:  # noqa: BLE001 — the order DB must not break a chat turn
        logger.warning(
            "failed to read the pending draft for %r",
            (buyer_id, shopping_session_id),
            exc_info=True,
        )
        return response
    if draft is None:
        return response
    return response.model_copy(update={"pending_draft": OrderDraftView.from_draft(draft)})


_ORDER_ID = re.compile(r"\bord_[A-Za-z0-9_-]+")


async def _order_facts(
    order_service: OrderService | None, buyer_id: str, text: str
) -> tuple[bool, bool, frozenset[tuple[str, Decimal]]]:
    """Which order states the reply may legitimately claim (S11 L4 grounding).

    An id the model made up, or one belonging to another buyer, raises in
    ``get_order``; that claim stays ungrounded and the guard rewrites it.
    The returned prices are the stored order totals: the reply may quote the
    amount the database confirmed, not the one the model remembered.
    """
    if order_service is None:
        return False, False, frozenset()
    confirmed = cancelled = False
    prices: set[tuple[str, Decimal]] = set()
    for order_id in set(_ORDER_ID.findall(text)):
        try:
            order, _request = await order_service.get_order(order_id=order_id, buyer_id=buyer_id)
        except Exception:  # noqa: BLE001 — an unknown order is an ungrounded claim
            continue
        prices.add((order.currency, order.total))
        if order.status == "confirmed":
            confirmed = True
        elif order.status == "cancelled":
            cancelled = True
    return confirmed, cancelled, frozenset(prices)


def _guard_output(
    guard: GuardRuntime,
    summary: str,
    cards: list[ProductCard],
    facts: GroundingFacts,
) -> tuple[str, list[ProductCard]]:
    """Repair the reply in place of rejecting it (spec §8)."""
    output_guard = OutputGuard(guard.reply_max_chars)
    checked = output_guard.check(summary, facts)
    if checked.hits:
        guard.metrics.incr("output.guarded", count=len(checked.hits))
        for rule in checked.hits:
            log_guard_event("output.guarded", rule=rule)
    guarded: list[ProductCard] = []
    for card in cards:
        highlights: list[str] = []
        for line in card.highlights:
            result = output_guard.check(line, facts)
            if result.hits:
                guard.metrics.incr("output.guarded", count=len(result.hits))
            highlights.append(result.text)
        guarded.append(card.model_copy(update={"highlights": highlights}))
    return checked.text, guarded


def _output_text(output: Any) -> str:
    """Flatten a tool result's content blocks to the text the model saw."""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(
            part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "")
            for part in output
        ).strip()
    return "" if output is None else str(output)


async def _notify_tool_results(
    observer: ToolResultsObserver, context: Sequence[Any], results: Sequence[Any]
) -> None:
    """Describe this turn's tool traffic for a transport observer (S14).

    The call block supplies ``arguments``; AgentScope stores it JSON-encoded
    as a string, so it travels to AG-UI unchanged. The observer is a side
    channel: a subscriber that cannot keep up is logged, never allowed to
    fail a turn that produced a good answer.
    """
    calls = {
        getattr(block, "id", None): block
        for message in context
        for block in message.get_content_blocks("tool_call")
    }
    records = [
        {
            "id": getattr(block, "id", None),
            "name": getattr(block, "name", None),
            "arguments": getattr(calls.get(getattr(block, "id", None)), "input", None),
            "output": _output_text(getattr(block, "output", None)),
        }
        for block in results
    ]
    try:
        await observer(records)
    except Exception:  # noqa: BLE001 — the transport must not fail the turn
        logger.warning("tool-result observer failed", exc_info=True)


async def run_turn(
    store: SessionStore,
    buyer_id: str,
    shopping_session_id: str,
    raw_query: str,
    timeout_seconds: float = 120.0,
    order_service: OrderService | None = None,
    on_tool_results: ToolResultsObserver | None = None,
    on_text_delta: TextDeltaObserver | None = None,
) -> CommerceIntentResponse:
    """Run one turn and record its trace, metrics and user-visible events (S13).

    The wrapper owns the trace so the whole turn — budget gate, model loop,
    guard repair and persistence — is attributable even when the turn fails.
    ``turn.answer_ready`` is what the buyer is shown; it is published only after
    the response is fully assembled, so the stream never shows an answer the
    client would not receive.
    """
    with trace_scope("turn", session_id=shopping_session_id, buyer_id=buyer_id) as turn_span:
        publish_event(
            "turn.started",
            session_id=shopping_session_id,
            buyer_id=buyer_id,
            query_chars=len(raw_query),
        )
        try:
            response = await _run_turn(
                store,
                buyer_id,
                shopping_session_id,
                raw_query,
                timeout_seconds=timeout_seconds,
                order_service=order_service,
                on_tool_results=on_tool_results,
                on_text_delta=on_text_delta,
            )
        except (Exception, asyncio.CancelledError) as exc:
            turn_span.fail(type(exc).__name__)
            publish_event(
                "turn.failed",
                session_id=shopping_session_id,
                # The buyer-visible stream gets a stable code. The full message
                # can name filesystem paths or provider internals; it stays in
                # the span's error text, which is internal.
                error=type(exc).__name__,
            )
            raise
        turn_span.attributes["outcome"] = "ok"
        turn_span.attributes["card_count"] = len(response.cards)
        turn_span.attributes["text_chars"] = len(response.final_text)
        publish_event(
            "turn.answer_ready",
            session_id=shopping_session_id,
            final_text=response.final_text,
            card_count=len(response.cards),
            cards=[
                {
                    "title": card.title,
                    "price_major": card.price_major,
                    "currency": card.currency,
                    "sku_id": card.sku_id,
                }
                for card in response.cards
            ],
            pending_draft=response.pending_draft is not None,
        )
        return response


async def _reply_with_deltas(
    agent: Any,
    user_msg: Msg,
    *,
    timeout_seconds: float,
    on_text_delta: TextDeltaObserver | None,
) -> Msg:
    """Await one agent reply, optionally forwarding its text as it arrives.

    AgentScope's ``reply_stream`` interleaves block events with the final
    ``Msg``. We consume it only when someone is listening: a caller with no
    observer (the plain HTTP intent endpoint) keeps the atomic ``reply``,
    which has no partially-rendered failure mode.

    The timeout wraps *consumption*, not just the call — a streaming reply is
    only complete when its last chunk arrives.
    """
    reply_stream = getattr(agent, "reply_stream", None)
    if on_text_delta is None or not callable(reply_stream):
        return await asyncio.wait_for(
            agent.reply(user_msg, structured_schema=ProductCardList),
            timeout=timeout_seconds,
        )

    final: Msg | None = None
    pending: list[str] = []
    answer_call = False

    async def flush() -> None:
        nonlocal pending
        for delta in pending:
            await on_text_delta(delta)
        pending = []

    async with asyncio.timeout(timeout_seconds):
        async for item in reply_stream(
            user_msg, structured_schema=ProductCardList, yield_final_msg=True
        ):
            if isinstance(item, ModelCallStartEvent):
                pending = []
                answer_call = False
            elif isinstance(item, TextBlockDeltaEvent):
                if item.delta:
                    pending.append(item.delta)
            elif isinstance(item, ToolCallStartEvent):
                if item.tool_call_name == STRUCTURED_OUTPUT_TOOL:
                    answer_call = True
            elif isinstance(item, ModelCallEndEvent):
                # The model call that asks for the structured answer wrote
                # prose around it, but that prose is unguarded — and the
                # guarded summary replaces it at the end of the turn. The
                # narration of every other call is safe to show, so it goes
                # out as soon as the call closes (the tool it asked for is
                # about to run, which is the waiting the buyer needs to see).
                if not answer_call:
                    await flush()
                pending = []
            elif not isinstance(item, EventBase):
                # With ``yield_final_msg`` the stream's last item is the
                # reply itself; every other item is a progress event.
                final = item
    if pending and not answer_call:
        # Agents that emit no model-call boundaries still get their narration.
        await flush()
    if final is None:
        # Without the final message there are no cards and no grounding
        # evidence, so a truncated stream fails like any other broken reply.
        logger.error("streaming reply ended without a final Msg")
        raise ModelFailureError()
    return final


async def _run_turn(
    store: SessionStore,
    buyer_id: str,
    shopping_session_id: str,
    raw_query: str,
    timeout_seconds: float = 120.0,
    order_service: OrderService | None = None,
    on_tool_results: ToolResultsObserver | None = None,
    on_text_delta: TextDeltaObserver | None = None,
) -> CommerceIntentResponse:
    """Run one conversational turn and return the model's product cards.

    With ``order_service`` wired (S10), the response also carries the session's
    pending draft. That value is read back from the database, so the buyer
    confirms the stored rows rather than anything the model transcribed.

    With a guard (S11) an exhausted budget short-circuits before the agent is
    called: ``run_turn`` demands structured output, and a plain-text refusal
    from inside the loop would be retried until the turn 502s.
    """
    if (
        store.guard is not None
        and store.guard.switches.budget
        and not await store.guard.budget.turn_allowed_async()
    ):
        store.guard.metrics.incr("budget.exhausted")
        log_guard_event("budget.exhausted", reason="turn_fallback")
        publish_event(
            "turn.short_circuit",
            session_id=shopping_session_id,
            reason="turn_fallback",
        )
        return CommerceIntentResponse(final_text=store.guard.rule_fallback_text(), cards=[])

    while True:
        with span("session.get_or_create"):
            key, session = await store.get_or_create(buyer_id, shopping_session_id)
        try:
            async with session.lock:
                # A concurrent failed turn on this key may have evicted the session
                # while we waited for its lock. Our ``session`` reference is then
                # stale: reload a fresh one so we don't carry the failed turn's
                # half-updated ``state.context`` into the snapshot we persist.
                if not store.is_cached(key, session):
                    continue
                # S11: the loop window and sequence history are per turn — a
                # second identical search next turn is not a loop.
                if session.guard is not None:
                    session.guard.begin_turn()
                # Snapshot the tool results so a failure is attributed to *this*
                # turn: ``state.context`` persists across resumed sessions, and an
                # ERROR from an earlier turn must not fail a later one. Track block
                # ids, not an index — S7 compression shrinks the list mid-turn.
                known_results = {
                    block.id
                    for msg in session.agent.state.context
                    for block in msg.get_content_blocks("tool_result")
                    if getattr(block, "id", None) is not None
                }
                with span("agent.reply") as reply_span:
                    try:
                        reply = await _reply_with_deltas(
                            session.agent,
                            UserMsg(name="user", content=raw_query),
                            timeout_seconds=timeout_seconds,
                            on_text_delta=on_text_delta,
                        )
                    except TimeoutError as exc:
                        raise ModelTimeoutError(timeout_seconds) from exc
                    except ContextCapacityError:
                        # S7: a capacity failure is its own contract (507), not a
                        # generic model failure (502).
                        raise
                    except Exception as exc:
                        # AgentScope re-raises model-call failures (network / rate-limit /
                        # provider 5xx) out of ``reply``; surface them as a structured error
                        # rather than leaking an unhandled 500 (S5-2). Log the real cause
                        # server-side for ops while the client sees a clean 502.
                        logger.exception("model call failed for session %r", key)
                        raise ModelFailureError() from exc
                    reply_span.attributes["context_messages"] = len(session.agent.state.context)
                # AgentScope converts a tool exception into an ERROR ``tool_result``
                # stored in the conversation context — *not* on the returned ``reply``,
                # which only carries the final text answer. Detect any ERROR produced
                # during this turn and fail loudly (S5-2) rather than trust whatever the
                # model guessed after the failed search.
                this_turn = tool_results_since(session.agent.state.context, known_results)
                # Emit before the ERROR check: a failed search is still a tool
                # call the buyer's stream should show, immediately before the
                # run fails.
                if on_tool_results is not None and this_turn:
                    await _notify_tool_results(
                        on_tool_results, session.agent.state.context, this_turn
                    )
                if any(block.state == ToolResultState.ERROR for block in this_turn):
                    raise ToolFailureError()
                structured = reply.structured_output
                if structured is None:
                    # S11 structural assertion: the reply carries no
                    # ``ProductCardList``. Counted, then failed exactly as S5
                    # did — a missing structured reply must stay a 502.
                    if store.guard is not None and store.guard.switches.structure:
                        store.guard.metrics.incr("reply.assertion_failed")
                        log_guard_event("reply.assertion_failed", reason="no_structured_output")
                    raise NoStructuredOutputError(reply.get_text_content() or "")
                cards = ProductCardList.model_validate(structured)
                # The model can copy cards from an earlier turn's structured
                # output; only products this turn's tools returned are real.
                grounded = ground_cards(cards.cards, this_turn)
                # S11 L4: repair prices, completion claims and leaks before the
                # buyer — and before ``persist`` journals what was shown.
                summary = cards.summary
                guarded_cards: list[ProductCard] = grounded
                if store.guard is not None and store.guard.switches.output_guard:
                    with span("turn.output_guard"):
                        evidence = search_evidence_from_context(session.agent.state.context)
                        reply_text = " ".join(
                            [cards.summary, *(h for card in grounded for h in card.highlights)]
                        )
                        with span("turn.order_facts"):
                            confirmed, cancelled, order_prices = await _order_facts(
                                order_service, buyer_id, reply_text
                            )
                        # One fact set from three real sources: this session's
                        # searches, the child agents' verified refs, and the
                        # stored order rows the reply refers to.
                        prices = frozenset(
                            {(item.currency, item.unit_price) for item in evidence}
                            | dispatch_prices_from_context(session.agent.state.context)
                            | order_prices
                        )
                        summary, guarded_cards = _guard_output(
                            store.guard,
                            cards.summary,
                            grounded,
                            GroundingFacts(
                                prices=prices,
                                has_confirmed_order=confirmed,
                                has_cancelled_order=cancelled,
                            ),
                        )
                response = CommerceIntentResponse(final_text=summary, cards=guarded_cards)
                # S10: attach the draft before persisting so the journal records
                # what the buyer was shown. A read failure must not lose the
                # model's answer or the session, so it degrades to "no draft".
                if order_service is not None:
                    with span("turn.pending_draft"):
                        response = await _with_pending_draft(
                            response, order_service, buyer_id, shopping_session_id
                        )
                with span("turn.persist"):
                    await store.persist(key, session.agent, raw_query, response)
                return response
        except Exception:
            # A failed turn must not leave a half-updated session in memory: evict so
            # the next access reloads the last good snapshot (S6 evict-on-failure).
            store.evict(key)
            raise
        finally:
            store.touch(key)
