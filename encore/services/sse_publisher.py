"""The SSE publisher: one bus subscription, many browsers (AIG 7, SAPRS 9.10, 10.5).

`EventBus` is in-process and synchronous (ADR-004); a party is forty phones each
holding one HTTP connection open. This module is the bridge, and it is deliberately
the only place in `encore/services/` that knows a browser exists.

Three rules decide its shape:

* **The bus handler does almost nothing.** It serialises one fact and puts one frame
  on each client's queue, then returns. A handler that waited on a socket write would
  make playback wait for a phone three rooms away, which is SAPRS 11.4's forbidden
  direction and would make SAPRS 11.9's "SSE client failure must not affect playback"
  unachievable.
* **Each client's queue is bounded, and overflow is visible.** When a slow client
  falls behind, its oldest frame is dropped and one `resync` frame is sent, so the
  page redraws from current state instead of replaying a stale fragment. Silent
  dropping is how a jukebox shows a song that ended three minutes ago.
* **Fan-out crosses threads explicitly.** The bus handler runs on the appliance thread
  (ADR-012) and the generator runs on the event loop, so the hand-off is
  `loop.call_soon_threadsafe` and never a bare `put_nowait`. That one line is why a
  publication cannot corrupt a browser's queue.

What is *not* here: per-client state (guests are anonymous, ADR-007), replay of
missed events (a reconnect resyncs from a snapshot, which cannot drift), and HTML.
The stream carries facts; fragments are rendered by `encore/controllers/` and fetched
by HTMX.

`beat()` is the one frame that is not a bus fact, and it is called out rather than
hidden: SAPRS 9.10 wants progress on the stream, no fact says "a second passed while
something was audible", and ADR-004 fixes the vocabulary at eight.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import json
import logging
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from encore.domain.playback import PlaybackProgress
from encore.events import EVENT_VOCABULARY, Event
from encore.events.bus import EventBus, Subscription

__all__ = ["EVENT_NAMES", "MAX_CLIENT_QUEUE", "Frame", "SSEPublisher", "Stream", "to_json"]

#: Frames a client may fall behind by before it is told to resync. At the SSE budget in
#: SAPRS 1.8 that is a few seconds of a party; past it a browser is not slow, it is
#: gone, and continuing to queue for it is a memory leak with a name.
MAX_CLIENT_QUEUE = 32

#: The stream's vocabulary, derived from `EVENT_VOCABULARY` rather than typed out
#: beside it. `encore/events/base.py` already says the class name *is* the name an event
#: carries on the SSE `event:` field (SAPRS 10.5), and SAPRS 10.5 lists the facts by
#: those names — so there is one naming rule, and a ninth fact would reach a browser
#: the day it is written, which is what makes ADR-004's "exactly eight" the thing a
#: reviewer has to agree to rather than a string someone forgot to add.
EVENT_NAMES: Mapping[type[Event], str] = MappingProxyType(
    {event: event.__name__ for event in EVENT_VOCABULARY}
)

#: The transport's own frames, in lowercase so they cannot be mistaken for a fact.
#: `snapshot` says "redraw, you have been away", `resync` says "you fell behind",
#: `progress` says "another second passed while something was audible".
SNAPSHOT = "snapshot"
PROGRESS = "progress"
RESYNC = "resync"


@dataclass(frozen=True, slots=True, kw_only=True)
class Frame:
    """One SSE message, as it will be written.

    `event_id` is monotonic across the process rather than per client, so the same
    number means the same publication in two browsers' logs — which is the whole use
    (nothing replays by id), and it is why it is a string on the wire.
    """

    event: str
    data: str = "{}"
    event_id: int | None = None

    def as_sse(self) -> dict[str, str]:
        """The shape `sse-starlette` writes. The only HTTP-shaped thing in this file."""

        payload = {"event": self.event, "data": self.data}
        if self.event_id is not None:
            payload["id"] = str(self.event_id)
        return payload


class Stream:
    """One browser's frame queue: written from the appliance thread, read on the loop.

    Attributes:
        dropped: Frames this client was too slow for. Reported on the resync frame,
            because "you missed four updates" is a more useful log line than a queue
            that quietly lied.
    """

    def __init__(self, *, loop: asyncio.AbstractEventLoop, limit: int = MAX_CLIENT_QUEUE) -> None:
        self._loop = loop
        self._limit = limit
        self._queue: deque[Frame] = deque()
        self._wake: asyncio.Event | None = None
        self.dropped = 0
        self.closed = False

    @property
    def pending(self) -> int:
        return len(self._queue)

    # -- the appliance thread -----------------------------------------------

    def offer(self, frame: Frame) -> None:
        """Hand one frame to this client. Never blocks, never raises, loses nothing loud."""

        if self.closed:
            return
        try:
            self._loop.call_soon_threadsafe(self._accept, frame)
        except RuntimeError:  # pragma: no cover - the loop closed mid-publication
            self.closed = True

    # -- the event loop ------------------------------------------------------

    async def next(self) -> Frame | None:
        """The next frame, or None once this client is finished with."""

        while True:
            if self._queue:
                return self._queue.popleft()
            if self.closed:
                return None
            wake = self._wake or asyncio.Event()
            self._wake = wake
            wake.clear()
            await wake.wait()
            self._wake = None

    def close(self) -> None:
        """Retire the client. Safe twice over, and from either thread."""

        self.closed = True
        wake = self._wake
        if wake is not None:
            self._loop.call_soon_threadsafe(wake.set)

    def _accept(self, frame: Frame) -> None:
        """Append one frame, telling the client to resync if that meant dropping one."""

        if self.closed:
            return
        if len(self._queue) >= self._limit:
            self._queue.popleft()
            self.dropped += 1
            if self.dropped % self._limit == 1:
                # One resync per overflow episode, then again every `limit` frames: a
                # browser that never catches up should keep being told to, but not once
                # per frame it misses. The warning costs a frame of its own — it is
                # appended and one is dropped again, so `pending` never exceeds `limit`
                # and the bound in this class's docstring stays true.
                self._queue.append(Frame(event=RESYNC, data=json.dumps({"dropped": self.dropped})))
                self._queue.popleft()
        self._queue.append(frame)
        if self._wake is not None:
            self._wake.set()


class SSEPublisher:
    """Turns bus facts into stream frames (AIG 7, SAPRS 10.5).

    Args:
        events: The bus to listen to. Subscribed on `start()`, withdrawn on `stop()`.
        logger: For the client that fell behind, which is the only failure this
            component is permitted (SAPRS 11.9).
    """

    def __init__(self, *, events: EventBus, logger: logging.Logger | None = None) -> None:
        self._events = events
        self._logger = logger or logging.getLogger("encore.sse")
        self._lock = threading.Lock()
        self._streams: list[Stream] = []
        self._subscriptions: list[Subscription] = []
        self._ids = itertools.count(1)
        self._published = 0

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Subscribe to every runtime fact. Idempotent, so a restart of the graph is safe."""

        if self._subscriptions:
            return
        self._subscriptions = [
            self._events.subscribe(event_type, self._handler(name), name=f"sse.{name}")
            for event_type, name in EVENT_NAMES.items()
        ]

    def stop(self) -> None:
        """Withdraw the subscriptions and release every client.

        Closing the streams is what lets a held-open `/events` generator return, so a
        shutdown that skipped it would leave every browser waiting on a cancelled task
        instead of ending cleanly (SAPRS 11.8 step 2).
        """

        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions = []
        with self._lock:
            streams, self._streams = self._streams, []
        for stream in streams:
            stream.close()

    # -- clients ----------------------------------------------------------

    def connect(self, *, loop: asyncio.AbstractEventLoop) -> Stream:
        """Register one browser. Called from the endpoint, on the loop."""

        stream = Stream(loop=loop)
        with self._lock:
            self._streams.append(stream)
        self._logger.debug("sse client connected", extra={"clients": self.clients})
        return stream

    def disconnect(self, stream: Stream) -> None:
        """Forget one browser. Idempotent, and always called by the generator's end."""

        was_there = False
        stream.close()
        with self._lock:
            for index, candidate in enumerate(self._streams):
                if candidate is stream:
                    del self._streams[index]
                    was_there = True
                    break
        if was_there:
            self._logger.debug("sse client left", extra={"clients": self.clients})

    @property
    def clients(self) -> int:
        with self._lock:
            return len(self._streams)

    @property
    def stats(self) -> dict[str, int]:
        """Publications and slow-client drops — the two numbers an operator wants."""

        with self._lock:
            dropped = sum(stream.dropped for stream in self._streams)
        return {"published": self._published, "dropped": dropped, "clients": self.clients}

    # -- the two entry points ----------------------------------------------

    def push_html(self, name: str, html: str) -> None:
        """Send one rendered panel to every client.

        Called from the appliance thread by `encore.api.fragments`, once per invalidated
        region per fact — not once per client. That single rendering decision is what
        makes forty phones cost one template render rather than forty, and it is the
        reason this module is a service with a fan-out queue rather than a loop over
        responses.
        """

        self._broadcast(Frame(event=name, data=html, event_id=self._next_id()))

    def beat(self, progress: PlaybackProgress) -> None:
        """Push the derived progress frame, from the server's tick task.

        Not a bus event, and not dressed up as one: no fact occurred, a second passed
        while something was audible. SAPRS 9.10 asks for progress on the stream and
        ADR-004 fixes the vocabulary at eight, so the composition root calls this
        directly rather than a ninth event being invented to carry it.
        """

        if progress.state.value == "idle":
            return
        self._broadcast(
            Frame(
                event=PROGRESS,
                data=json.dumps(
                    {
                        "state": progress.state.value,
                        "song_id": None if progress.song_id is None else int(progress.song_id),
                        "position_ms": round(progress.position.total_seconds() * 1000),
                        "duration_ms": round(progress.duration.total_seconds() * 1000),
                        "fraction": round(progress.fraction, 4),
                    }
                ),
                event_id=self._next_id(),
            )
        )

    # -- internals --------------------------------------------------------

    def _handler(self, name: str) -> Callable[[Any], None]:
        """The bus subscription: serialise, queue, return. Nothing else.

        Registered as a synchronous handler on purpose. The bus detects a coroutine
        handler and schedules it on a loop — and the publishing thread is the appliance
        thread, which has no loop, so an `async` handler here would be reported as
        skipped rather than delivered (SAPRS 11.3).
        """

        def handle(event: Any) -> None:
            self._broadcast(Frame(event=name, data=to_json(event), event_id=self._next_id()))

        return handle

    def _broadcast(self, frame: Frame) -> None:
        with self._lock:
            streams = list(self._streams)
            self._published += 1
        for stream in streams:
            stream.offer(frame)

    def _next_id(self) -> int:
        return next(self._ids)


def to_json(value: object) -> str:
    """Serialise a bus fact or a domain value as JSON.

    Hand-written rather than delegated to a web framework's encoder because the shapes
    that reach it are known and narrow — frozen dataclasses, `StrEnum`s, `NewType` ints,
    `Path`s, datetimes, timedeltas — and because the guardrail that matters (SAPRS
    11.10: domain services do not depend on a web framework) is the one this buys back
    with forty lines.
    """

    return json.dumps(_plain(value), separators=(",", ":"), default=str)


def _plain(value: object) -> Any:  # noqa: PLR0911 - a type dispatch, one branch per type
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return round(value.total_seconds() * 1000)
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _plain(getattr(value, field.name))
            for field in dataclasses.fields(value)
            if not field.name.startswith("_")
        }
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(str(item) for item in value)
    if isinstance(value, (list, tuple, deque)):
        return [_plain(item) for item in value]
    return str(value)
