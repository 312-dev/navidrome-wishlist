"""The event bus behind the browser's live connection.

Only the runtime publishes. Providers are handed no way to reach this, which is
why `ProviderContext` has no event field: a provider that could emit interface
events would be deciding what the user sees, and that decision belongs to the
layer that knows whether anyone is watching.

Reconnecting clients are not replayed to. There is no buffer of missed events
and no sequence negotiation, because the page refetches its state on connect
anyway. A replay mechanism would be a second way for the client to learn the
same facts, and the two would drift.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator

from .log import get

log = get("events")

EVENT_VERSION = 1

# The canonical event names. Publishing anything not listed here raises, so a
# typo in an event name fails at the call site rather than silently never
# reaching a listener that spells it correctly.
EVENTS = frozenset({
    "track.added",
    "track.updated",
    "track.removed",
    "track.source_added",
    "job.started",
    "job.progress",
    "job.finished",
    "job.failed",
    "provider.state",
    "credential.updated",
    "scan.requested",
})

# Dropped rather than queued without limit. A browser tab that stops reading
# must not be able to grow the server's memory, and a client too far behind is
# better served by reconnecting and refetching than by receiving a backlog.
MAX_PENDING = 64

# Every open stream holds a server worker thread for as long as it lives, so the
# number of streams is bounded well below the pool. Without this the count only
# ever climbs: a reload leaves the previous subscriber in place until the server
# next tries to write to a socket that has gone, and a handful of reloads take
# every worker, at which point the whole application stops answering.
#
# Reaching the cap evicts the oldest rather than refusing the newest, because
# the common way to reach it is the same person reloading, and the connection
# they are actually looking at is the newest one.
MAX_CLIENTS = 12


@dataclass(frozen=True)
class Event:
    name: str
    data: dict[str, Any]
    at: float

    def sse(self) -> str:
        """Format as one Server-Sent Events frame."""
        payload = json.dumps({"v": EVENT_VERSION, **self.data}, default=str)
        return f"event: {self.name}\ndata: {payload}\n\n"


class Subscriber:
    def __init__(self, bus: "EventBus") -> None:
        self._bus = bus
        self._q: queue.Queue[Event | None] = queue.Queue(maxsize=MAX_PENDING)
        self.dropped = 0

    def put(self, event: Event) -> None:
        try:
            self._q.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def close(self) -> None:
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass

    def stream(self, keepalive: float = 20.0) -> Iterator[str]:
        """Yield SSE frames until the subscriber is closed.

        A comment frame goes out on every idle tick. Without it an idle
        connection is indistinguishable from a dead one to any proxy in the
        middle, and the browser learns about the disconnect only when it next
        tries to read.
        """
        try:
            yield f"retry: 3000\n\n"
            while True:
                try:
                    event = self._q.get(timeout=keepalive)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                if event is None:
                    return
                yield event.sse()
        finally:
            self._bus.unsubscribe(self)


class EventBus:
    """Fan-out to connected browsers, plus the presence signal that drives polling."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: set[Subscriber] = set()
        # Insertion order, so the cap can evict the oldest. A set alone cannot
        # answer "which of these arrived first".
        self._order: list[Subscriber] = []
        self._last_seen = 0.0

    def subscribe(self) -> Subscriber:
        sub = Subscriber(self)
        evicted = []
        with self._lock:
            while len(self._order) >= MAX_CLIENTS:
                stale = self._order.pop(0)
                self._subs.discard(stale)
                evicted.append(stale)
            self._subs.add(sub)
            self._order.append(sub)
            self._last_seen = time.time()
        for stale in evicted:
            # Closed outside the lock: close() puts a sentinel on the stale
            # subscriber's queue, and doing that while holding the lock would
            # let a full queue block every other publisher.
            stale.close()
        if evicted:
            log.warning("evicted stale streams to stay under the cap",
                        context={"evicted": len(evicted), "cap": MAX_CLIENTS})
        log.info("client connected", context={"clients": self.client_count})
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            self._subs.discard(sub)
            if sub in self._order:
                self._order.remove(sub)
            self._last_seen = time.time()
        if sub.dropped:
            log.warning("client fell behind", context={"dropped": sub.dropped})

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, name: str, **data: Any) -> None:
        if name not in EVENTS:
            raise ValueError(f"unknown event {name!r}; add it to EVENTS if it is real")
        event = Event(name, data, time.time())
        with self._lock:
            targets = list(self._subs)
        for sub in targets:
            sub.put(event)

    def someone_watching(self, grace_seconds: float) -> bool:
        """Whether to run sources on the hot interval.

        True while a client is connected, and for `grace_seconds` after the last
        one leaves, so that a page reload does not drop polling to the cold tier
        for the length of a reconnect.
        """
        with self._lock:
            if self._subs:
                return True
            return (time.time() - self._last_seen) < grace_seconds

    def close(self) -> None:
        with self._lock:
            targets = list(self._subs)
        for sub in targets:
            sub.close()
