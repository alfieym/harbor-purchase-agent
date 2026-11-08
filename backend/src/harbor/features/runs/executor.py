"""The AG-UI event sequence for one turn, written exactly once (S14/S15).

Both execution paths use this: the inline manager (``queue_enabled=false``)
supplies an ``append`` that also fans out to in-memory subscribers, and the
worker supplies one that renews its heartbeat and honours a cancel flag. The
*sequence* — start, tool calls, narration, snapshot, terminal frame — lives
here only, because two copies of it would drift and the frontend reads it as a
contract.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from harbor.features.runs import agui

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"finished", "failed", "cancelled", "interrupted"})

# Frames that *end* a turn rather than narrate it. A caller that reacts to a
# cancel by refusing the next ``append`` must still let these through: the
# ending is what makes the journal replayable, and without it a cancelled run
# would leave its last message open forever.
CLOSING_EVENT_TYPES = frozenset(
    {"TEXT_MESSAGE_END", "STATE_SNAPSHOT", "STEP_FINISHED", "RUN_ERROR", "RUN_FINISHED"}
)

ToolResults = list[dict]
ToolObserver = Callable[[ToolResults], Awaitable[None]]
TextObserver = Callable[[str], Awaitable[None]]
TurnRunner = Callable[
    [str, str, str, ToolObserver | None, TextObserver],
    Awaitable[Any],
]
# ``append(event, status=..., error=...)`` returns the event's sequence number.
Append = Callable[..., Awaitable[int]]

_ERRORS: dict[str, tuple[str, str]] = {
    "ModelTimeoutError": ("MODEL_TIMEOUT", "模型响应超时"),
    "ModelFailureError": ("MODEL_FAILED", "模型调用失败"),
    "ToolFailureError": ("TOOL_FAILED", "商品检索工具调用失败"),
    "NoStructuredOutputError": ("NO_STRUCTURED_OUTPUT", "模型未产出结构化输出"),
    "ContextCapacityError": ("CONTEXT_CAPACITY", "上下文超出容量限制"),
}


class TurnCancelled(Exception):
    """The journal says this run was cancelled; stop writing narration."""


def error_for(exc: BaseException) -> tuple[str, str]:
    code, message = _ERRORS.get(type(exc).__name__, ("TURN_FAILED", "本轮处理失败"))
    # The exception text can name filesystem paths or provider internals; it
    # stays in the server log, never in the buyer-facing stream.
    logger.warning("run turn failed (%s)", type(exc).__name__, exc_info=exc)
    return code, message


def final_text(response: Any) -> str:
    if response is None:
        return ""
    if isinstance(response, dict):
        return str(response.get("final_text") or "")
    return str(getattr(response, "final_text", "") or "")


def snapshot(response: Any, status: str) -> dict[str, Any]:
    """The structured state a client rebuilds from; text is never parsed.

    Card fields keep the response's snake_case so the frontend can reuse the
    OpenAPI-generated ``ProductCard`` type unchanged.
    """
    if response is None:
        data: dict[str, Any] = {}
    elif hasattr(response, "model_dump"):
        data = response.model_dump(mode="json")
    elif isinstance(response, dict):
        data = response
    else:
        data = {}
    return {
        "status": status,
        "final_text": data.get("final_text", ""),
        "cards": data.get("cards", []),
        "pending_draft": data.get("pending_draft"),
    }


async def execute_turn(
    *,
    run_id: str,
    thread_id: str,
    buyer_id: str,
    session_id: str,
    text: str,
    turn_runner: TurnRunner,
    append: Append,
    id_factory: Callable[[], str] | None = None,
    shutting_down: Callable[[], bool] = lambda: False,
) -> str:
    """Run one turn and return its terminal status.

    ``append`` is the only way this function talks to the outside world, so a
    caller decides what "persist" means (and whether a cancel should be
    noticed) without this sequence knowing.

    Cancellation is the one case that does not return: after writing the
    ending, ``CancelledError`` is re-raised so the task still ends as
    cancelled, which is what the event loop expects of a cancelled task.
    """
    new_id = id_factory or (lambda: uuid.uuid4().hex)
    message_id = f"msg_{run_id}"
    text_open = False

    async def observer(records: ToolResults) -> None:
        for record in records:
            call_id = str(record.get("id") or f"call_{new_id()[:8]}")
            name = str(record.get("name") or "tool")
            await append(agui.tool_call_start(call_id, name, parent_message_id=message_id))
            arguments = record.get("arguments")
            if arguments:
                await append(agui.tool_call_args(call_id, str(arguments)))
            await append(agui.tool_call_end(call_id))
            await append(
                agui.tool_call_result(f"msg_{call_id}", call_id, str(record.get("output") or ""))
            )

    try:
        await append(agui.step_started("agent"))
        await append(agui.text_message_start(message_id))
        text_open = True

        async def on_text_delta(delta: str) -> None:
            # Persist-before-broadcast applies to narration too: a refresh
            # mid-turn must replay the same words the live stream showed.
            await append(agui.text_message_content(message_id, delta))

        response = await turn_runner(buyer_id, session_id, text, observer, on_text_delta)
        closing = final_text(response)
        if closing:
            await append(agui.text_message_content(message_id, closing))
        await append(agui.text_message_end(message_id))
        text_open = False
        await append(agui.state_snapshot(snapshot(response, "finished")))
        await append(agui.step_finished("agent"))
        await append(
            agui.run_finished(thread_id=thread_id, run_id=run_id, result={"status": "finished"}),
            status="finished",
        )
        return "finished"
    except TurnCancelled:
        if text_open:
            await append(agui.text_message_end(message_id))
        await append(agui.state_snapshot(snapshot(None, "cancelled")))
        await append(
            agui.run_finished(thread_id=thread_id, run_id=run_id, result={"status": "cancelled"}),
            status="cancelled",
        )
        return "cancelled"
    except asyncio.CancelledError:
        if text_open:
            await append(agui.text_message_end(message_id))
        if shutting_down():
            await append(
                agui.run_error("server_restarted", code="RUN_INTERRUPTED"),
                status="interrupted",
                error="server_restarted",
            )
        else:
            await append(agui.state_snapshot(snapshot(None, "cancelled")))
            await append(
                agui.run_finished(
                    thread_id=thread_id, run_id=run_id, result={"status": "cancelled"}
                ),
                status="cancelled",
            )
        raise
    except Exception as exc:  # noqa: BLE001 - every failure becomes RUN_ERROR
        if text_open:
            await append(agui.text_message_end(message_id))
        code, message = error_for(exc)
        await append(agui.run_error(message, code=code), status="failed", error=code)
        return "failed"
