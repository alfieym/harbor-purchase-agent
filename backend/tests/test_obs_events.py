"""S13 event bus: per-session buffers, replay, live delivery and SSE framing."""

import asyncio
import json

from harbor.observability.events import EventBus, UserEvent
from harbor.observability.sse import encode_sse, sse_stream


def test_publish_assigns_monotonic_sequence_and_buffers_per_session() -> None:
    bus = EventBus()
    first = bus.publish("turn.started", session_id="s1", query_chars=3)
    second = bus.publish("turn.started", session_id="s2")

    assert (first.seq, second.seq) == (1, 2)
    assert bus.recent(session_id="s1") == [first]
    assert bus.recent() == [first, second]


def test_recent_supports_after_seq_limit_and_unknown_sessions() -> None:
    bus = EventBus()
    for _ in range(5):
        bus.publish("tick", session_id="s1")

    assert [event.seq for event in bus.recent(session_id="s1", after_seq=2)] == [3, 4, 5]
    assert len(bus.recent(session_id="s1", limit=2)) == 2
    assert bus.recent(session_id="missing") == []


def test_subscribe_replays_then_delivers_live_events() -> None:
    bus = EventBus()
    first = bus.publish("turn.started", session_id="s1")
    bus.publish("tool.search.completed", session_id="s1")

    async def scenario() -> None:
        subscription = bus.subscribe(session_id="s1", after_seq=first.seq)
        replayed = await subscription.get(timeout=0.5)
        assert replayed is not None and replayed.type == "tool.search.completed"

        bus.publish("turn.answer_ready", session_id="s1")
        live = await subscription.get(timeout=0.5)
        assert live is not None and live.type == "turn.answer_ready"

        subscription.close()
        bus.publish("turn.failed", session_id="s1")
        assert await subscription.get(timeout=0.1) is None

    asyncio.run(scenario())


def test_subscription_only_sees_its_session() -> None:
    bus = EventBus()

    async def scenario() -> None:
        subscription = bus.subscribe(session_id="s1")
        bus.publish("turn.started", session_id="s2")
        assert await subscription.get(timeout=0.1) is None

        bus.publish("turn.started", session_id="s1")
        event = await subscription.get(timeout=0.5)
        assert event is not None and event.session_id == "s1"
        subscription.close()

    asyncio.run(scenario())


def test_slow_subscriber_keeps_only_the_newest_events() -> None:
    bus = EventBus(subscriber_buffer=3)

    async def scenario() -> None:
        subscription = bus.subscribe(session_id="s1")
        for index in range(10):
            bus.publish("tick", session_id="s1", index=index)
        # Let the loop run the scheduled put_nowait callbacks.
        await asyncio.sleep(0)

        received = []
        while True:
            event = await subscription.get(timeout=0.01)
            if event is None:
                break
            received.append(event.data["index"])
        subscription.close()

        assert received == [7, 8, 9]

    asyncio.run(scenario())


def test_replay_cannot_outgrow_the_subscriber_buffer() -> None:
    bus = EventBus(subscriber_buffer=2)
    for index in range(6):
        bus.publish("tick", session_id="s1", index=index)

    async def scenario() -> None:
        subscription = bus.subscribe(session_id="s1")
        received = []
        while True:
            event = await subscription.get(timeout=0.01)
            if event is None:
                break
            received.append(event.data["index"])
        subscription.close()
        assert received == [4, 5]

    asyncio.run(scenario())


def test_session_buffers_are_evicted_lru() -> None:
    bus = EventBus(max_sessions=2)
    bus.publish("tick", session_id="s1")
    bus.publish("tick", session_id="s2")
    bus.publish("tick", session_id="s1")  # touch s1, so s2 is the oldest
    bus.publish("tick", session_id="s3")

    assert bus.recent(session_id="s1") != []
    assert bus.recent(session_id="s2") == []
    assert bus.recent(session_id="s3") != []


def test_encode_sse_frames_id_event_and_data() -> None:
    event = UserEvent(
        seq=7,
        type="turn.started",
        timestamp=1.0,
        session_id="s1",
        trace_id="t1",
        data={"query_chars": 3},
    )

    frame = encode_sse(event)

    assert frame.startswith("id: 7\nevent: turn.started\ndata: ")
    assert frame.endswith("\n\n")
    payload = json.loads(frame.split("data: ", 1)[1].strip())
    assert payload["session_id"] == "s1"
    assert payload["trace_id"] == "t1"


def test_sse_stream_emits_heartbeats_and_events() -> None:
    bus = EventBus()

    async def scenario() -> None:
        stream = sse_stream(bus, heartbeat_seconds=0.01)
        assert await anext(stream) == ": keepalive\n\n"

        bus.publish("turn.started", session_id="s1")
        frame = await anext(stream)
        assert "event: turn.started" in frame
        await stream.aclose()

    asyncio.run(scenario())
