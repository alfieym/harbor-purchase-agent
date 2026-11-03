"""S6 real-data smoke: one real turn, then restore from disk in a fresh process.

Phase 1 (real calls): run one turn through the production path so
``data/sessions/`` receives a genuine snapshot + journal entry.
Phase 2 (zero API calls): in a new process, restore that snapshot through
``SessionRepository.load`` and ``SessionStore.get_or_create`` and assert the
previous turn's answer (including the recommended sku) is back.

Usage (from the repo root, Qdrant must not be held by ``make dev-api``)::

    HARBOR_SEARCH_RERANK_ENABLED=false uv run --directory backend \\
        python ../scripts/s6_smoke.py phase1
    HARBOR_SEARCH_RERANK_ENABLED=false uv run --directory backend \\
        python ../scripts/s6_smoke.py phase2
"""

import argparse
import asyncio
import json

from harbor.agents.factory import build_agent, build_runtime
from harbor.core.config import Settings
from harbor.features.chat.repository import SessionRepository
from harbor.features.chat.service import SessionStore, run_turn

BUYER = "smoke-buyer"
SESSION = "smoke-s6-1"
QUERY = "给猫买个饮水器"


def _settings() -> Settings:
    settings = Settings()
    if not settings.llm_configured:
        raise SystemExit("OPENAI_API_KEY missing: cannot run the real turn")
    return settings


def phase1() -> None:
    settings = _settings()
    runtime = build_runtime(settings)
    repo = SessionRepository(settings.sessions_dir)
    store = SessionStore(
        agent_factory=lambda state: build_agent(runtime, state=state),
        repository=repo,
    )
    response = asyncio.run(
        run_turn(store, BUYER, SESSION, QUERY, timeout_seconds=settings.turn_timeout_seconds)
    )
    path = repo.snapshot_path((BUYER, SESSION))
    print(
        json.dumps(
            {
                "final_text": response.final_text,
                "cards": [card.model_dump(mode="json") for card in response.cards],
                "snapshot": str(path),
                "snapshot_bytes": path.stat().st_size if path.exists() else 0,
                "journal": str(repo.journal_path()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def phase2() -> None:
    settings = _settings()
    repo = SessionRepository(settings.sessions_dir)
    key = (BUYER, SESSION)

    state = repo.load(key)
    assert state is not None, "snapshot missing — run phase1 first"
    users = [msg for msg in state.context if getattr(msg, "role", None) == "user"]
    assert users and users[0].get_text_content() == QUERY

    # AgentScope keeps a turn's parsed answer in ``reply_context.structured_output``
    # (not on the message: ``state.context`` holds the ReAct trace, where the same
    # cards also appear as the ``GenerateStructuredOutput`` tool-call input).
    cards = (state.reply_context.structured_output or {}).get("cards") or []
    assert cards, "previous turn restored without any recommended cards"

    trace_cards: list[dict] = []
    for msg in state.context:
        for block in msg.get_content_blocks("tool_call"):
            if block.name != "GenerateStructuredOutput":
                continue
            payload = block.input
            if isinstance(payload, str):
                payload = json.loads(payload)
            trace_cards = payload.get("cards") or []
    assert [card.get("sku_id") for card in trace_cards] == [card.get("sku_id") for card in cards], (
        "reply_context and the message trace disagree on the recommended cards"
    )

    # Restart path: a fresh store must load the snapshot and hand that state to
    # the agent factory. The stub factory keeps phase 2 free of model/embedding.
    seen: dict[str, object] = {}

    def capture_factory(state):  # noqa: ANN001
        seen["state"] = state
        return object()  # type: ignore[return-value]

    store = SessionStore(agent_factory=capture_factory, repository=repo)

    async def _resume() -> None:
        await store.get_or_create(BUYER, SESSION)

    asyncio.run(_resume())
    restored = seen["state"]
    assert restored is not None
    assert len(restored.context) == len(state.context)  # type: ignore[attr-defined]

    print(
        json.dumps(
            {
                "restored_messages": len(state.context),
                "first_user": users[0].get_text_content(),
                "cards_from": "reply_context.structured_output",
                "previous_cards": [
                    {
                        "title": card.get("title"),
                        "sku_id": card.get("sku_id"),
                        "price_major": card.get("price_major"),
                    }
                    for card in cards
                ],
                "message_trace_agrees": True,
                "factory_received_state": restored is not None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["phase1", "phase2"])
    args = parser.parse_args()
    if args.phase == "phase1":
        phase1()
    else:
        phase2()
