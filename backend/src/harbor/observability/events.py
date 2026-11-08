"""In-process user-visible event stream for S13.

The event bus answers "what is the user seeing right now": a turn started, a
search came back with hits, the answer is ready, the turn failed. Events are
buffered per shopping session so a reconnecting client can replay what it
missed, and pushed live to every subscriber of that session.

Publishing is synchronous and thread-safe: tools run on worker threads and
guard callbacks fire from the model wrapper, so a subscriber's loop is woken
with ``call_soon_threadsafe`` rather than assuming the publisher's thread.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

from harbor.observability.trace import current_trace_id

__all__ = ["EventBus", "Subscription", "UserEvent"]


@dataclass(frozen=True)
class UserEvent:
    """One event the buyer's client can render."""

    seq: int
    type: str
    timestamp: float
    session_id: str | None = None
    trace_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "type": self.type,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "data": dict(self.data),
        }


def _offer(queue: asyncio.Queue[UserEvent], event: UserEvent) -> None:
    """Append to a bounded queue, dropping the oldest event when full.

    A subscriber that stops reading must not grow server memory without
    bound: the newest events are what the buyer needs, and the sequence id
    lets the client notice the gap.
    """
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        pass


class _Subscriber:
    __slots__ = ("loop", "queue", "session_id")

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        session_id: str | None,
        buffer: int = 100,
    ) -> None:
        self.loop = loop
        self.queue: asyncio.Queue[UserEvent] = asyncio.Queue(maxsize=buffer)
        self.session_id = session_id

    def push(self, event: UserEvent) -> None:
        try:
            # ``_offer`` is a queue operation, so it must run on the loop
            # thread; the publisher may be a worker thread.
            self.loop.call_soon_threadsafe(_offer, self.queue, event)
        except RuntimeError:
            # The subscriber's loop closed between publishing and delivery.
            pass


class Subscription:
    """A live view of one session's event stream."""

    __slots__ = ("_bus", "_subscriber", "_closed")

    def __init__(self, bus: EventBus, subscriber: _Subscriber) -> None:
        self._bus = bus
        self._subscriber = subscriber
        self._closed = False

    @property
    def session_id(self) -> str | None:
        return self._subscriber.session_id

    async def get(self, timeout: float | None = None) -> UserEvent | None:
        """Next event, or ``None`` when ``timeout`` expires."""
        if self._closed:
            return None
        queue = self._subscriber.queue
        if timeout is None:
            return await queue.get()
        try:
            return await asyncio.wait_for(queue.get(), timeout)
        except TimeoutError:
            return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus._remove(self._subscriber)


class EventBus:
    """Per-session ring buffers plus live fan-out."""

    def __init__(
        self,
        *,
        session_buffer: int = 200,
        global_buffer: int = 1000,
        subscriber_buffer: int = 100,
        max_sessions: int = 256,
        clock=time.time,
    ) -> None:
        if session_buffer < 1 or global_buffer < 1 or subscriber_buffer < 1:
            raise ValueError("buffers must be at least 1")
        if max_sessions < 1:
            raise ValueError("max_sessions must be at least 1")
        self._session_buffer = session_buffer
        self._subscriber_buffer = subscriber_buffer
        self._max_sessions = max_sessions
        self._global: deque[UserEvent] = deque(maxlen=global_buffer)
        self._session: OrderedDict[str, deque[UserEvent]] = OrderedDict()
        self._subscribers: list[_Subscriber] = []
        self._seq = 0
        self._clock = clock
        self._lock = threading.Lock()

    def publish(
        self,
        event_type: str,
        *,
        session_id: str | None = None,
        trace_id: str | None = None,
        **data: Any,
    ) -> UserEvent:
        with self._lock:
            self._seq += 1
            event = UserEvent(
                seq=self._seq,
                type=event_type,
                timestamp=self._clock(),
                session_id=session_id,
                trace_id=trace_id or current_trace_id(),
                data=dict(data),
            )
            self._global.append(event)
            if session_id is not None:
                buffer = self._session.get(session_id)
                if buffer is None:
                    buffer = deque(maxlen=self._session_buffer)
                    self._session[session_id] = buffer
                    # Bound the number of sessions: a long-running process
                    # must not accumulate one buffer per session forever.
                    while len(self._session) > self._max_sessions:
                        self._session.popitem(last=False)
                else:
                    self._session.move_to_end(session_id)
                buffer.append(event)
            subscribers = [
                subscriber
                for subscriber in self._subscribers
                if subscriber.session_id is None or subscriber.session_id == session_id
            ]
        for subscriber in subscribers:
            subscriber.push(event)
        return event

    def recent(
        self,
        *,
        session_id: str | None = None,
        after_seq: int = 0,
        limit: int = 50,
    ) -> list[UserEvent]:
        with self._lock:
            source = self._global if session_id is None else self._session.get(session_id)
            events = [] if source is None else [e for e in source if e.seq > after_seq]
        if limit <= 0:
            return events
        return events[-limit:]

    def subscribe(self, *, session_id: str | None = None, after_seq: int = 0) -> Subscription:
        """Replay buffered events after ``after_seq``, then follow live ones."""
        loop = asyncio.get_running_loop()
        subscriber = _Subscriber(loop, session_id, self._subscriber_buffer)
        with self._lock:
            source = self._global if session_id is None else self._session.get(session_id)
            if source is not None:
                # Replay inside the lock so a live publish cannot overtake it.
                # The queue is bounded, so a long replay keeps only the newest.
                for event in source:
                    if event.seq > after_seq:
                        _offer(subscriber.queue, event)
            self._subscribers.append(subscriber)
        return Subscription(self, subscriber)

    def _remove(self, subscriber: _Subscriber) -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def size(self) -> int:
        with self._lock:
            return len(self._global)

    def clear(self) -> None:
        with self._lock:
            self._global.clear()
            self._session.clear()
