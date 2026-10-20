"""In-process async event bus.

Every subsystem publishes here — the daemon, brain, skills, scheduler, voice
pipeline — and every client (CLI, tray, overlay) subscribes. This is what lets a
spoken command and a typed command take identical paths through the system.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from friday.log import get

log = get(__name__)

Handler = Callable[["Event"], Awaitable[None]]


@dataclass(slots=True)
class Event:
    """Something that happened. `topic` is dotted: `brain.matched`, `skill.done`."""

    topic: str
    data: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:  # keeps log lines readable
        return f"Event({self.topic}, {self.data!r})"


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)
        self._queues: set[asyncio.Queue[Event]] = set()

    # -- publish/subscribe by topic ------------------------------------------

    def subscribe(self, topic: str, handler: Handler) -> None:
        """Subscribe to a topic. `*` matches everything."""
        self._subs[topic].append(handler)

    def unsubscribe(self, topic: str, handler: Handler) -> None:
        if handler in self._subs.get(topic, []):
            self._subs[topic].remove(handler)

    async def publish(self, topic: str, **data: Any) -> Event:
        event = Event(topic=topic, data=data)
        # Phase 64: the payload holds raw tool arguments, utterances and replies -- a debug log must say what happened, not repeat it
        log.debug("bus %s fields=%s", topic, ",".join(sorted(data)))

        handlers = [*self._subs.get(topic, []), *self._subs.get("*", [])]
        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                log.exception("handler failed for %s", topic)

        # Fan out to streaming consumers (websocket clients). They are outside the process, so they receive a PROJECTION of the event
        # (`friday.remote_events.project`: an explicit allowlist of topics and fields, deny by default, no content), made once and only when
        # somebody is listening; in-process subscribers above got the original. A topic the policy does not list is not delivered at all.
        if self._queues:
            from friday import remote_events

            projected = remote_events.project(topic, data)
            if projected is not None:
                outbound = Event(topic=topic, data=projected, at=event.at)
                for queue in list(self._queues):
                    queue.put_nowait(outbound)

        return event

    # -- streaming consumers --------------------------------------------------

    def stream(self) -> asyncio.Queue[Event]:
        """Register a queue that receives every event. Call `drop` when done."""
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)
        self._queues.add(queue)
        return queue

    def drop(self, queue: asyncio.Queue[Event]) -> None:
        self._queues.discard(queue)


BUS = EventBus()
