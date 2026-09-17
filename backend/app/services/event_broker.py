"""Process-scoped SSE event broker (Step 5 Part 7).

Thread model:
  - FastAPI sync routes (POST /transcript-events, /start, /finish) run in the
    threadpool. `publish()` therefore runs on a WORKER thread.
  - The SSE generator (StreamingResponse in /events) runs on the event-loop
    thread. Its subscriber queue is an asyncio.Queue consumed on that loop.
  - All enqueues are marshalled onto the subscriber's event loop via
    `loop.call_soon_threadsafe`, so asyncio.Queue is only ever touched from the
    loop thread -> race-free (Part 7 "thread / async safety").
  - Fallback: if no loop is attached yet (subscriber just registered, generator
    not started), we enqueue directly — also single-threaded at that point.

Slow-consumer policy (Part 8):
  - Each subscriber queue is bounded (`settings.sse_queue_maxsize`).
  - If a subscriber's queue is full at publish time -> EVICT it:
      * mark inactive, remove from the meeting's subscriber set
      * push a disconnect sentinel so a blocked generator wakes and exits
  - The Producer (FINAL persistence flow) NEVER blocks on a subscriber. The only
    work publish() does is a bounded put_nowait (through the loop callback).

Lifecycle (Part 20): **Process Scoped**. Restart drops subscribers and queues —
recovery is DB Snapshot + persisted-FINAL catch-up. Broker != durable event log.
Multi-instance later -> Redis Pub/Sub / Streams / Kafka (not done in this step).
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.schemas.sse import SSEEventEnvelope

# Sentinel pushed to a queue on disconnect/evict so a waiting generator wakes up.
_DISCONNECT: SSEEventEnvelope | None = None


@dataclass(eq=False)
class Subscriber:
    """One SSE connection's mailbox (bounded) + lifecycle flags.

    eq=False -> identity-based equality/hash (each subscriber is unique even if
    fields are equal), so subscribers can live in a set.
    """

    meeting_id: str
    maxsize: int = field(default_factory=lambda: settings.sse_queue_maxsize)
    queue: asyncio.Queue[SSEEventEnvelope | None] = field(init=False)
    loop: asyncio.AbstractEventLoop | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.queue = asyncio.Queue(maxsize=self.maxsize)

    def is_active(self) -> bool:
        return True  # eviction is tracked by the broker set membership instead


class EventBroker:
    """meeting_id -> set[Subscriber]"""

    def __init__(self) -> None:
        self._subs: dict[str, set[Subscriber]] = {}
        self._lock = threading.RLock()
        self._publish_count = 0
        self._evict_count = 0

    # --- subscribe / unsubscribe -----------------------------------------
    def subscribe(self, meeting_id: str) -> Subscriber:
        sub = Subscriber(meeting_id=meeting_id)
        with self._lock:
            self._subs.setdefault(meeting_id, set()).add(sub)
        return sub

    def unsubscribe(self, meeting_id: str, sub: Subscriber) -> None:
        """Remove a subscriber; idempotent. Wakes it if blocked on the queue."""
        with self._lock:
            subs = self._subs.get(meeting_id)
            if subs is None:
                return
            subs.discard(sub)
            if not subs:
                self._subs.pop(meeting_id, None)
        _enqueue_sentinel(sub)

    def disconnect_all(self) -> None:
        """Close every subscriber (app shutdown)."""
        with self._lock:
            targets = list(self._subs.values())
            self._subs.clear()
        for subs in targets:
            for sub in subs:
                _enqueue_sentinel(sub)

    # --- publish (Producer side; NEVER blocks) -----------------------------
    def publish(self, meeting_id: str, event: SSEEventEnvelope) -> None:
        with self._lock:
            subs = self._subs.get(meeting_id)
            if not subs:
                return
            subs = list(subs)  # snapshot; eviction below may mutate the set

        for sub in subs:
            # Marshal enqueue onto the consumer's loop for thread-safety.
            loop = sub.loop
            if loop is not None:
                try:
                    loop.call_soon_threadsafe(self._put_or_evict, meeting_id, sub, event)
                except RuntimeError:
                    # Loop closed / not running: treat as dead subscriber.
                    _enqueue_sentinel(sub)
                    self.unsubscribe(meeting_id, sub)
            else:
                # No loop attached yet (generator not started): put directly.
                self._put_or_evict(meeting_id, sub, event)

    def _put_or_evict(self, meeting_id: str, sub: Subscriber, event: SSEEventEnvelope) -> None:
        """Run ON the subscriber's event-loop thread. Bounded put; evict on full."""
        try:
            sub.queue.put_nowait(event)
        except asyncio.QueueFull:
            # Slow consumer (Part 8): evict instead of blocking the Producer.
            with self._lock:
                self._evict_count += 1
            _enqueue_sentinel(sub)  # wake the generator so it ends this stream
            self.unsubscribe(meeting_id, sub)

    # --- inspection (used by tests / debug) --------------------------------
    def subscriber_count(self, meeting_id: str) -> int:
        with self._lock:
            subs = self._subs.get(meeting_id)
            return len(subs) if subs is not None else 0

    def total_subscribers(self) -> int:
        with self._lock:
            return sum(len(s) for s in self._subs.values())

    def publish_count(self) -> int:
        with self._lock:
            return self._publish_count

    def evict_count(self) -> int:
        with self._lock:
            return self._evict_count


def _enqueue_sentinel(sub: Subscriber) -> None:
    """Push the disconnect sentinel onto a subscriber's queue best-effort."""
    try:
        if sub.loop is not None:
            sub.loop.call_soon_threadsafe(sub.queue.put_nowait, _DISCONNECT)
        else:
            sub.queue.put_nowait(_DISCONNECT)
    except (asyncio.QueueFull, RuntimeError):
        pass


# Singleton: single-process MVP (Part 20).
event_broker = EventBroker()

__all__ = ["event_broker", "Subscriber", "_DISCONNECT"]
