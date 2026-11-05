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
import time
from collections.abc import Callable
from dataclasses import dataclass

from agentscope.agent import Agent
from agentscope.message import ToolResultState, UserMsg
from agentscope.state import AgentState

from harbor.agents.cards import ProductCardList
from harbor.agents.context_governance import ContextCapacityError, recorded_turn_count
from harbor.agents.preferences import IDENTITY_STATE_KEY
from harbor.features.chat.repository import SessionKey, SessionRepository, SnapshotWriteError
from harbor.features.chat.schemas import CommerceIntentResponse

logger = logging.getLogger(__name__)


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


class SessionStore:
    """In-memory session registry with TTL + LRU eviction."""

    def __init__(
        self,
        agent_factory: Callable[[AgentState | None], Agent],
        ttl_seconds: float = 1800.0,
        max_sessions: int = 1000,
        clock: Callable[[], float] = time.monotonic,
        repository: SessionRepository | None = None,
    ) -> None:
        self._agent_factory = agent_factory
        self._ttl = ttl_seconds
        self._max = max_sessions
        self._clock = clock
        self._repository = repository
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
            # S8: the buyer id travels in the state, not in the factory signature
            # (``agent_factory(state)``), so long-term memory can be scoped to the
            # buyer without touching every existing factory.
            state.middle_context[IDENTITY_STATE_KEY] = {"buyer_id": buyer_id}
            session = Session(
                agent=self._agent_factory(state), lock=asyncio.Lock(), last_active=now
            )
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


async def run_turn(
    store: SessionStore,
    buyer_id: str,
    shopping_session_id: str,
    raw_query: str,
    timeout_seconds: float = 120.0,
) -> CommerceIntentResponse:
    """Run one conversational turn and return the model's product cards."""
    while True:
        key, session = await store.get_or_create(buyer_id, shopping_session_id)
        try:
            async with session.lock:
                # A concurrent failed turn on this key may have evicted the session
                # while we waited for its lock. Our ``session`` reference is then
                # stale: reload a fresh one so we don't carry the failed turn's
                # half-updated ``state.context`` into the snapshot we persist.
                if not store.is_cached(key, session):
                    continue
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
                try:
                    reply = await asyncio.wait_for(
                        session.agent.reply(
                            UserMsg(name="user", content=raw_query),
                            structured_schema=ProductCardList,
                        ),
                        timeout=timeout_seconds,
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
                # AgentScope converts a tool exception into an ERROR ``tool_result``
                # stored in the conversation context — *not* on the returned ``reply``,
                # which only carries the final text answer. Detect any ERROR produced
                # during this turn and fail loudly (S5-2) rather than trust whatever the
                # model guessed after the failed search.
                for msg in session.agent.state.context:
                    for block in msg.get_content_blocks("tool_result"):
                        block_id = getattr(block, "id", None)
                        if block_id is not None and block_id in known_results:
                            continue
                        if block.state == ToolResultState.ERROR:
                            raise ToolFailureError()
                structured = reply.structured_output
                if structured is None:
                    raise NoStructuredOutputError(reply.get_text_content() or "")
                cards = ProductCardList.model_validate(structured)
                response = CommerceIntentResponse(final_text=cards.summary, cards=cards.cards)
                await store.persist(key, session.agent, raw_query, response)
                return response
        except Exception:
            # A failed turn must not leave a half-updated session in memory: evict so
            # the next access reloads the last good snapshot (S6 evict-on-failure).
            store.evict(key)
            raise
        finally:
            store.touch(key)
