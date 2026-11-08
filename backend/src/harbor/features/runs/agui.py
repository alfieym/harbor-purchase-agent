"""AG-UI event builders (S14).

The protocol is the wire contract, so these functions do two jobs: spell the
camelCase field names exactly once (a typo here is a client that silently
ignores a message), and reject values the protocol forbids at the point they
are produced rather than after they are persisted.

Every event is a plain dict. The run store persists ``json.dumps(event)``
verbatim, and an SSE frame carries that JSON unchanged, so what a client
replays is byte-for-byte what the server recorded.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "custom",
    "encode_sse",
    "run_error",
    "run_finished",
    "run_started",
    "state_delta",
    "state_snapshot",
    "step_finished",
    "step_started",
    "text_message_content",
    "text_message_end",
    "text_message_start",
    "tool_call_args",
    "tool_call_end",
    "tool_call_result",
    "tool_call_start",
]

Event = dict[str, Any]


def run_started(*, thread_id: str, run_id: str) -> Event:
    return {"type": "RUN_STARTED", "threadId": thread_id, "runId": run_id}


def run_finished(*, thread_id: str, run_id: str, result: Any = None) -> Event:
    event: Event = {"type": "RUN_FINISHED", "threadId": thread_id, "runId": run_id}
    if result is not None:
        event["result"] = result
    return event


def run_error(message: str, *, code: str | None = None) -> Event:
    event: Event = {"type": "RUN_ERROR", "message": message}
    if code is not None:
        event["code"] = code
    return event


def step_started(step_name: str) -> Event:
    return {"type": "STEP_STARTED", "stepName": step_name}


def step_finished(step_name: str) -> Event:
    return {"type": "STEP_FINISHED", "stepName": step_name}


def text_message_start(message_id: str, *, role: str = "assistant") -> Event:
    return {"type": "TEXT_MESSAGE_START", "messageId": message_id, "role": role}


def text_message_content(message_id: str, delta: str) -> Event:
    if not delta:
        raise ValueError("TEXT_MESSAGE_CONTENT.delta must be non-empty")
    return {"type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": delta}


def text_message_end(message_id: str) -> Event:
    return {"type": "TEXT_MESSAGE_END", "messageId": message_id}


def tool_call_start(
    tool_call_id: str, tool_call_name: str, *, parent_message_id: str | None = None
) -> Event:
    event: Event = {
        "type": "TOOL_CALL_START",
        "toolCallId": tool_call_id,
        "toolCallName": tool_call_name,
    }
    if parent_message_id is not None:
        event["parentMessageId"] = parent_message_id
    return event


def tool_call_args(tool_call_id: str, delta: str) -> Event:
    return {"type": "TOOL_CALL_ARGS", "toolCallId": tool_call_id, "delta": delta}


def tool_call_end(tool_call_id: str) -> Event:
    return {"type": "TOOL_CALL_END", "toolCallId": tool_call_id}


def tool_call_result(message_id: str, tool_call_id: str, content: str) -> Event:
    return {
        "type": "TOOL_CALL_RESULT",
        "messageId": message_id,
        "toolCallId": tool_call_id,
        "content": content,
        "role": "tool",
    }


def state_snapshot(snapshot: Any) -> Event:
    return {"type": "STATE_SNAPSHOT", "snapshot": snapshot}


def state_delta(delta: list[Any]) -> Event:
    return {"type": "STATE_DELTA", "delta": delta}


def custom(name: str, value: Any) -> Event:
    return {"type": "CUSTOM", "name": name, "value": value}


def encode_sse(seq: int, event: Event) -> str:
    """One AG-UI SSE frame: the seq is the event id, the type lives in the JSON.

    AG-UI clients read ``data:`` only; adding an SSE ``event:`` line would make
    the browser dispatch a named event they do not listen for.
    """
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"id: {seq}\ndata: {payload}\n\n"
