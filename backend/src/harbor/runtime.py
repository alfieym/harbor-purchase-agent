"""The turn itself, assembled the same way by the API and by a worker.

S15 splits the process, not the behaviour: ``POST /commerce/intents`` and an
AG-UI run both call this one function, so a deployed worker cannot answer a
turn differently from the API that used to answer it inline.
"""

from __future__ import annotations

from harbor.core.config import Settings
from harbor.features.chat.service import SessionStore, run_turn
from harbor.features.orders.service import OrderService
from harbor.features.runs.executor import TurnRunner

__all__ = ["build_turn_runner"]


def build_turn_runner(
    settings: Settings,
    chat_store: SessionStore,
    order_service: OrderService | None,
) -> TurnRunner:
    async def turn_runner(
        buyer_id: str,
        session_id: str,
        text: str,
        on_tool_results,
        on_text_delta,
    ):
        return await run_turn(
            chat_store,
            buyer_id,
            session_id,
            text,
            timeout_seconds=settings.turn_timeout_seconds,
            order_service=order_service,
            on_tool_results=on_tool_results,
            on_text_delta=on_text_delta,
        )

    return turn_runner
