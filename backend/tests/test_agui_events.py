"""AG-UI event shapes (S14): exact wire names, camelCase, SSE framing."""

import json

import pytest

from harbor.features.runs import agui


def test_run_lifecycle_events_use_the_protocol_field_names() -> None:
    started = agui.run_started(thread_id="t1", run_id="r1")
    assert started == {"type": "RUN_STARTED", "threadId": "t1", "runId": "r1"}

    finished = agui.run_finished(
        thread_id="t1", run_id="r1", result={"status": "finished"}
    )
    assert finished == {
        "type": "RUN_FINISHED",
        "threadId": "t1",
        "runId": "r1",
        "result": {"status": "finished"},
    }

    error = agui.run_error("model_timeout", code="MODEL_TIMEOUT")
    assert error == {"type": "RUN_ERROR", "message": "model_timeout", "code": "MODEL_TIMEOUT"}


def test_text_message_events() -> None:
    assert agui.text_message_start("m1") == {
        "type": "TEXT_MESSAGE_START",
        "messageId": "m1",
        "role": "assistant",
    }
    assert agui.text_message_content("m1", "你好") == {
        "type": "TEXT_MESSAGE_CONTENT",
        "messageId": "m1",
        "delta": "你好",
    }
    assert agui.text_message_end("m1") == {"type": "TEXT_MESSAGE_END", "messageId": "m1"}


def test_an_empty_text_delta_is_rejected() -> None:
    # AG-UI requires TEXT_MESSAGE_CONTENT.delta to be non-empty; emitting an
    # empty frame makes some clients append nothing and still open a message.
    with pytest.raises(ValueError):
        agui.text_message_content("m1", "")


def test_tool_call_events() -> None:
    assert agui.tool_call_start("c1", "product_search", parent_message_id="m1") == {
        "type": "TOOL_CALL_START",
        "toolCallId": "c1",
        "toolCallName": "product_search",
        "parentMessageId": "m1",
    }
    assert agui.tool_call_args("c1", '{"query":"台灯"}') == {
        "type": "TOOL_CALL_ARGS",
        "toolCallId": "c1",
        "delta": '{"query":"台灯"}',
    }
    assert agui.tool_call_end("c1") == {"type": "TOOL_CALL_END", "toolCallId": "c1"}
    assert agui.tool_call_result("m2", "c1", "3 hits") == {
        "type": "TOOL_CALL_RESULT",
        "messageId": "m2",
        "toolCallId": "c1",
        "content": "3 hits",
        "role": "tool",
    }


def test_state_events_carry_structured_snapshots() -> None:
    snapshot = {"cards": [{"sku_id": "S1", "price_major": 99.0}], "status": "running"}
    assert agui.state_snapshot(snapshot) == {"type": "STATE_SNAPSHOT", "snapshot": snapshot}

    delta = [{"op": "replace", "path": "/status", "value": "finished"}]
    assert agui.state_delta(delta) == {"type": "STATE_DELTA", "delta": delta}


def test_custom_event() -> None:
    assert agui.custom("harbor.notice", {"level": "info"}) == {
        "type": "CUSTOM",
        "name": "harbor.notice",
        "value": {"level": "info"},
    }


def test_sse_frame_carries_the_seq_as_the_event_id_and_no_event_line() -> None:
    event = agui.run_started(thread_id="t1", run_id="r1")
    frame = agui.encode_sse(7, event)

    assert frame.startswith("id: 7\n")
    assert frame.endswith("\n\n")
    assert "event:" not in frame
    data_line = next(line for line in frame.splitlines() if line.startswith("data: "))
    assert json.loads(data_line.removeprefix("data: ")) == event
