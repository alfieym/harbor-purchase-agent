"""The S15 wake-up channel: at-least-once, and the duplicate is harmless.

Nothing here touches a real Redis: ``MemoryRunQueue`` is the behavioural
reference and ``RedisRunQueue`` is checked against a fake client that records
the commands it would have sent.
"""

import asyncio

from harbor.features.runs.queue import Claimed, MemoryRunQueue, RedisRunQueue


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------- memory queue


def test_a_message_is_delivered_to_one_consumer() -> None:
    async def scenario():
        queue = MemoryRunQueue()
        await queue.enqueue("run_1")

        claimed = await queue.claim("worker-a", block_ms=10)
        # Nothing is left for a second consumer until the first one finishes.
        assert await queue.claim("worker-b", block_ms=10) is None
        return claimed

    claimed = _run(scenario())
    assert claimed is not None
    assert claimed.run_id == "run_1"


def test_an_acknowledged_message_is_gone() -> None:
    async def scenario():
        queue = MemoryRunQueue()
        await queue.enqueue("run_1")
        claimed = await queue.claim("worker-a", block_ms=10)
        await queue.ack(claimed.message_id)
        return await queue.claim("worker-b", block_ms=10)

    assert _run(scenario()) is None


def test_an_unacknowledged_message_is_redelivered_after_the_idle_timeout() -> None:
    """The worker that claimed it may have died; the message must come back."""

    async def scenario():
        queue = MemoryRunQueue()
        await queue.enqueue("run_1")
        await queue.claim("worker-a", block_ms=10)

        # Not old enough yet.
        assert await queue.reclaim("worker-b", min_idle_ms=1000, count=10) == []
        await asyncio.sleep(0.02)
        return await queue.reclaim("worker-b", min_idle_ms=10, count=10)

    claimed = _run(scenario())
    assert [c.run_id for c in claimed] == ["run_1"]


def test_redelivery_hands_the_message_to_the_reclaiming_consumer() -> None:
    async def scenario():
        queue = MemoryRunQueue()
        await queue.enqueue("run_1")
        await queue.claim("worker-a", block_ms=10)
        await asyncio.sleep(0.02)
        await queue.reclaim("worker-b", min_idle_ms=10, count=10)
        # worker-a must not still see it, and worker-b now owns it.
        return await queue.reclaim("worker-a", min_idle_ms=10, count=10)

    assert _run(scenario()) == []


def test_messages_keep_their_order() -> None:
    async def scenario():
        queue = MemoryRunQueue()
        for index in range(3):
            await queue.enqueue(f"run_{index}")
        return [(await queue.claim("w", block_ms=10)).run_id for _ in range(3)]

    assert _run(scenario()) == ["run_0", "run_1", "run_2"]


def test_claim_on_an_empty_queue_returns_none() -> None:
    async def scenario():
        queue = MemoryRunQueue()
        return await queue.claim("worker-a", block_ms=10)

    assert _run(scenario()) is None


# -------------------------------------------------------------- redis queue


class FakeRedis:
    """Records commands; replies are set per test."""

    def __init__(self, **replies) -> None:
        self.calls: list[tuple] = []
        self.replies = replies

    async def xgroup_create(self, stream, group, **kwargs):
        self.calls.append(("xgroup_create", stream, group, kwargs))
        return True

    async def xadd(self, name, fields, **kwargs):
        self.calls.append(("xadd", name, fields, kwargs))
        return self.replies.get("xadd", "1-0")

    async def xreadgroup(self, group, consumer, streams, **kwargs):
        self.calls.append(("xreadgroup", group, consumer, streams, kwargs))
        return self.replies.get("xreadgroup")

    async def xack(self, stream, group, message_id):
        self.calls.append(("xack", stream, group, message_id))
        return 1

    async def xautoclaim(self, stream, group, consumer, **kwargs):
        self.calls.append(("xautoclaim", stream, group, consumer, kwargs))
        return self.replies.get("xautoclaim", ("0-0", []))

    async def aclose(self):
        self.calls.append(("aclose",))


def _redis(replies=None) -> tuple[RedisRunQueue, FakeRedis]:
    client = FakeRedis(**(replies or {}))
    return RedisRunQueue(client=client, stream="s", group="g"), client


def test_the_stream_is_trimmed_because_the_journal_is_the_truth() -> None:
    async def scenario():
        queue, client = _redis()
        await queue.enqueue("run_1")
        return client.calls

    calls = _run(scenario())
    # The group is created once, before the first message.
    assert calls[0][0] == "xgroup_create"
    _, name, fields, kwargs = [c for c in calls if c[0] == "xadd"][0]
    assert (name, fields) == ("s", {"run_id": "run_1"})
    # A lost message costs one wake-up; the sweeper re-sends from SQLite.
    assert kwargs["maxlen"] == 10_000
    assert kwargs["approximate"] is True


def test_claim_reads_one_entry_from_the_group() -> None:
    async def scenario():
        queue, client = _redis({"xreadgroup": [["s", [("7-2", {"run_id": "run_9"})]]]})
        claimed = await queue.claim("worker-a", block_ms=250)
        return claimed, client.calls

    claimed, calls = _run(scenario())
    assert claimed == Claimed(message_id="7-2", run_id="run_9")
    _, group, consumer, streams, kwargs = [c for c in calls if c[0] == "xreadgroup"][0]
    assert (group, consumer, streams) == ("g", "worker-a", {"s": ">"})
    assert kwargs["block"] == 250


def test_claim_on_an_empty_reply_returns_none() -> None:
    async def scenario():
        queue, _ = _redis({"xreadgroup": []})
        return await queue.claim("worker-a", block_ms=10)

    assert _run(scenario()) is None


def test_ack_names_the_message_it_consumes() -> None:
    async def scenario():
        queue, client = _redis()
        await queue.ack("7-2")
        return client.calls

    assert _run(scenario())[0] == ("xack", "s", "g", "7-2")


def test_reclaim_parses_the_claimed_entries() -> None:
    async def scenario():
        queue, client = _redis(
            {
                "xautoclaim": (
                    "0-0",
                    [("8-1", {"run_id": "run_1"}), ("8-2", {"run_id": "run_2"})],
                    [],
                )
            }
        )
        claimed = await queue.reclaim("worker-b", min_idle_ms=60_000, count=5)
        return claimed, client.calls

    claimed, calls = _run(scenario())
    assert [c.run_id for c in claimed] == ["run_1", "run_2"]
    _, stream, group, consumer, kwargs = [c for c in calls if c[0] == "xautoclaim"][0]
    assert (stream, group, consumer) == ("s", "g", "worker-b")
    assert kwargs["min_idle_time"] == 60_000
    assert kwargs["count"] == 5


def test_reclaim_tolerates_an_empty_pending_list() -> None:
    async def scenario():
        queue, _ = _redis({"xautoclaim": ("0-0", [])})
        return await queue.reclaim("worker-b", min_idle_ms=1000)

    assert _run(scenario()) == []
