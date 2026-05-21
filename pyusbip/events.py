"""Tiny synchronous-publish, async-subscribe event bus.

Used to fan out lifecycle events from the USB/IP server (someone
connected, someone IMPORTed a device, libusb saw a hotplug) to the
HTTP control plane's SSE stream. Subscribers receive events via an
`asyncio.Queue`; publishers call `publish()` from any thread/coroutine
context.

Design constraints:

  * Synchronous publish — libusb's hotplug callbacks fire from the
    libusb event thread, NOT inside the asyncio loop. We can't `await`
    in there, so publish must be safe to call from any context.
  * Bounded queues per subscriber — a stuck SSE client (browser tab
    backgrounded, network glitch) must not OOM the server. We use
    `maxsize=64` and drop the oldest event on overflow.
  * No backpressure to publishers — `publish()` always returns
    immediately, never blocks on slow subscribers.

This is deliberately not a full pub/sub library. ~40 lines of code,
no dependencies, behaviour you can hold in your head.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

logger = logging.getLogger("pyusbip.events")


# Event type constants. Strings (not Enums) so they serialize cleanly
# to JSON for SSE consumers.
DEVICE_ADDED = "device_added"
DEVICE_REMOVED = "device_removed"
DEVICE_ATTACHED = "device_attached"
DEVICE_DETACHED = "device_detached"
BIND_CHANGED = "bind_changed"


class EventBus:
    """Pub/sub for pyusbip lifecycle events.

    Subscribers call `subscribe()` to get an asyncio.Queue and consume
    events with `await queue.get()`. They MUST call `unsubscribe()` in
    their `finally` block so the bus doesn't accumulate dead queues.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, *, queue_size: int = 64):
        self._loop = loop
        self._queue_size = queue_size
        self._subscribers: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        """Returns a queue that will receive every event published from
        the moment of subscription forward. Bounded to `queue_size`
        events; on overflow the oldest event is dropped (preserves the
        most recent state, which is usually what consumers care about
        when they reconnect after a hiccup)."""
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def publish(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Publish an event. Safe to call from any thread (uses
        `call_soon_threadsafe` to hop onto the loop). Returns
        immediately; never blocks on slow subscribers."""
        event = {"type": event_type, **payload}
        # Hop to the loop thread if we're not on it. libusb's hotplug
        # callback runs on the libusb event thread, so this branch is
        # taken for hotplug events; in-loop publishers (server.py)
        # could call _dispatch directly but going through the loop is
        # safer and the cost is negligible.
        self._loop.call_soon_threadsafe(self._dispatch, event)

    def _dispatch(self, event: Dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the oldest event to keep the queue moving. The
                # SSE client will see a gap, which is preferable to
                # the queue stalling permanently.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
                logger.warning("event subscriber queue full; dropped oldest")
