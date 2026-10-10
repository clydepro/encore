"""The live channel, from the side that asks for it (SAPRS 9.10, 10.5).

`tests/unit/test_sse_publisher.py` proves the fan-out: frames queued, slow clients resynced,
streams closed. This file proves the part a browser can see — that a URL holds a response open
for minutes, that the first thing on it is the whole current state, and that when a phone's
radio sleeps and the socket dies, the appliance notices, closes the queue and stops counting
that listener as a screen in the room.

Three properties get coverage rather than a tour of the endpoint:

* **A connection begins with a snapshot.** The alternative is a client that renders whatever
  the next fact happens to be, and a guest who loads a page mid-song and sees an empty player
  is the report that makes a demo look broken.
* **A disconnect is prompt and certain.** Every registered stream is a queue holding up to
  `MAX_CLIENT_QUEUE` rendered panels on a Pi 4's heap; the only thing that frees it is the
  generator's `finally`, so these tests hold references to the streams themselves rather than
  counting them.
* **The frames are the ones the composition root publishes.** An adapter that invented names,
  or re-sent the whole state per fact, would be a private protocol — and `encore-live.js`,
  written against the published one, would go quietly stale.

`tests/support/http.py` explains why a held-open response needs its own driver rather than
`httpx.ASGITransport`. The publisher is real, the appliance thread is real, and everything
below `views.player` is replaced: that function's contract is what
`tests/integration/test_server_app.py` checks for real.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from encore.api import sse, views
from encore.api.html import build_renderer
from encore.api.sse import PING_SECONDS, SNAPSHOT
from encore.domain import QueueItemId, SongId
from encore.events import EventBus, SongQueued
from encore.services import SSEPublisher
from encore.services.sse_publisher import MAX_CLIENT_QUEUE, Stream
from encore.utilities.appliance import ApplianceThread
from tests.support.http import held_open

pytestmark = pytest.mark.unit


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def live(bus: EventBus) -> SSEPublisher:
    return SSEPublisher(events=bus)


@pytest.fixture
def registered(live: SSEPublisher) -> Iterator[list[Stream]]:
    """Every `Stream` the endpoint registered, by the end of the test.

    The way to see a leak is to hold the object it leaked. A test that watched the publisher's
    client count alone would pass while the queues stayed reachable, and the count reaching
    zero is not what frees memory.
    """

    seen: list[Stream] = []
    original = live.connect

    def connect(**kwargs: Any) -> Stream:
        stream = original(**kwargs)
        seen.append(stream)
        return stream

    live.connect = connect  # type: ignore[method-assign]
    yield seen
    live.connect = original  # type: ignore[method-assign]


@pytest.fixture
def appliance_thread() -> Iterator[ApplianceThread]:
    """A real worker thread, because `/events` reads through it (ADR-012)."""

    made = ApplianceThread(name="encore-test-sse")
    made.start()
    yield made
    made.stop()


@pytest.fixture
def app(
    live: SSEPublisher, appliance_thread: ApplianceThread, monkeypatch: pytest.MonkeyPatch
) -> Iterator[FastAPI]:
    """`/events` over a real publisher and a real thread, with no services at all.

    `views.player` is the seam: the view model's own correctness is the integration file's
    business, and stubbing it here keeps a broken template out of a test about sockets.
    """

    monkeypatch.setattr(
        views,
        "player",
        lambda _jukebox: SimpleNamespace(
            as_json=lambda: {
                "state": "playing",
                "song": {"id": 7, "title": "First Album Track 1"},
                "position_ms": 1234,
                "queue_length": 3,
            }
        ),
    )
    made = FastAPI()
    made.state.jukebox = SimpleNamespace(
        live=live,
        appliance=appliance_thread,
        health=SimpleNamespace(snapshot=SimpleNamespace(overall="healthy", components=[])),
        subscriptions=[],
    )
    made.state.renderer = build_renderer()
    made.include_router(sse.router)
    # The composition root does this on the appliance thread, before the first request; there
    # is no composition root here, and a publisher that has not subscribed to the bus is a
    # socket that never receives anything — which reads as a broken test rather than as the
    # two-second hole it is.
    live.start()
    try:
        yield made
    finally:
        live.stop()


# -- what a connection receives -------------------------------------------


async def test_a_new_connection_begins_with_the_whole_current_state(app: FastAPI) -> None:
    """SAPRS 9.10's "clients receive initial state snapshots", as a first frame.

    A browser that connected and then waited for a fact would render an empty player for as
    long as nobody pressed anything — which on a quiet afternoon is the whole time.
    """

    async with held_open(app) as stream:
        event, data = (await stream.next(1))[0]

    assert event == SNAPSHOT
    assert json.loads(data)["queue_length"] == 3


async def test_the_response_is_one_that_stays_open(app: FastAPI) -> None:
    """SAPRS 10.5's replacement for polling, in the shape of the answer.

    The check is that nothing arrives for a quarter second after the snapshot: a response that
    completes is a client that reconnects, which is polling with extra steps — and the way an
    ordinary `JSONResponse` refactor of this route would present itself.
    """

    async with held_open(app) as stream:
        await stream.next(1)
        assert stream.status == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        with pytest.raises(AssertionError):
            await stream.next(1, within=0.25)


async def test_the_headers_a_long_lived_response_needs_are_on_it(app: FastAPI) -> None:
    """`no-store`, and the one header that stops a proxy buffering the party.

    SAPRS 10.10 covers the cache half. `X-Accel-Buffering: no` is the other: someone puts
    nginx in front of the appliance for TLS and every phone in the room starts receiving facts
    in bundles of eight, several seconds late.
    """

    async with held_open(app) as stream:
        await stream.next(1)
        assert stream.headers["cache-control"] == "no-store"
        assert stream.headers["x-accel-buffering"] == "no"


async def test_the_snapshot_is_read_on_the_appliance_thread(
    app: FastAPI,
    appliance_thread: ApplianceThread,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-012, at the one HTTP handler that reads services without being asked to.

    A connect is less a request for state than a promise to be told about it, and if the read
    that seeds that promise ran on the event loop it would be a queue query racing the tick.
    The assertion is the thread identity the view saw, which is the guardrail's own question.
    """

    seen: dict[str, Any] = {}

    def fake_player(jukebox: Any) -> Any:
        seen["current"] = appliance_thread.is_current
        seen["ident"] = threading.get_ident()
        return SimpleNamespace(as_json=lambda: {"state": "idle"})

    monkeypatch.setattr(views, "player", fake_player)
    async with held_open(app) as stream:
        await stream.next(1)

    assert seen["current"] is True
    assert seen["ident"] == appliance_thread.worker_ident
    assert seen["ident"] != threading.get_ident()


async def test_no_fact_can_arrive_before_the_snapshot_it_belongs_to(
    app: FastAPI,
    bus: EventBus,
    live: SSEPublisher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordering the endpoint's docstring names, made observable.

    The snapshot is built *before* this client's queue is registered. A connect that subscribed
    first could be handed a fact and then a snapshot taken after it, and the browser would draw
    whichever arrived last — the older of the two, usually. `clients == 0` at the moment of the
    read is the proof that the registration came second, which is the half a frame ordering
    alone cannot show.
    """

    observed: dict[str, int] = {}

    def fake_player(jukebox: Any) -> Any:
        observed["clients_at_read"] = live.clients
        return SimpleNamespace(as_json=lambda: {"state": "playing"})

    monkeypatch.setattr(views, "player", fake_player)
    async with held_open(app) as stream:
        got = await stream.next(1)
        bus.publish(
            SongQueued(song_id=SongId(9), queue_item_id=QueueItemId(9), position=2, queue_length=2)
        )
        got += await stream.next(1)

    assert observed["clients_at_read"] == 0
    assert [event for event, _ in got] == [SNAPSHOT, "SongQueued"]


async def test_a_published_fact_arrives_named_after_itself(app: FastAPI, bus: EventBus) -> None:
    """The vocabulary, at the wire.

    `encore-live.js` and any third-party client subscribe by these names, so a rename that
    stayed consistent inside Python would still break the room.
    """

    async with held_open(app) as stream:
        await stream.next(1)
        bus.publish(
            SongQueued(song_id=SongId(4), queue_item_id=QueueItemId(4), position=2, queue_length=2)
        )
        event, data = (await stream.next(1))[0]

    assert event == "SongQueued"
    assert json.loads(data)["song_id"] == 4


def test_the_keepalive_is_shorter_than_anything_in_the_path() -> None:
    """A comment line every 15 seconds, because a silent socket looks identical to a dead one.

    The number is the assertion, not the ping: an idle load balancer ends a connection at 60
    seconds, and a `progress` frame usually keeps things alive at one a second — except when
    nothing is playing, which is exactly when nobody would notice the stream had gone.
    """

    assert 0 < PING_SECONDS < 60


# -- what a lost connection leaves behind ---------------------------------


async def test_a_client_that_disconnects_frees_its_queue(
    app: FastAPI, live: SSEPublisher, bus: EventBus, registered: list[Stream]
) -> None:
    """The leak test, by the object rather than the counter.

    Every registered stream holds rendered panels. SAPRS 8.6's "forty devices" is a
    forty-queue ceiling on a 1 GB machine if this cleanup ever stops running, and the
    generator's `finally` is the only thing between that sentence and a party that freezes.
    The driver's `close()` sends the disconnect and waits, so what is asserted here is the
    appliance's reaction rather than a race.
    """

    async with held_open(app) as stream:
        await stream.next(1)
        assert live.clients == 1
        queued = registered[0]

    assert live.clients == 0
    bus.publish(
        SongQueued(song_id=SongId(1), queue_item_id=QueueItemId(1), position=1, queue_length=1)
    )
    assert queued.pending == 0  # nothing is queued for a browser that has gone


async def test_a_client_that_stops_reading_is_told_to_start_over(
    app: FastAPI, bus: EventBus, live: SSEPublisher, registered: list[Stream]
) -> None:
    """SAPRS 9.10's "detect and clean up stale connections", for the stalled rather than gone.

    A phone with a sleeping radio does not close its socket; it stops reading. The publisher's
    bound is what notices, and the frame it sends asks for a re-read, which is the only honest
    thing to say to a client that missed who knows how much.
    """

    async with held_open(app) as stream:
        await stream.next(1)
        stream.pause()  # the radio sleeps; the socket buffer fills and stops draining
        for index in range(MAX_CLIENT_QUEUE * 2):
            bus.publish(
                SongQueued(
                    song_id=SongId(1),
                    queue_item_id=QueueItemId(index + 1),
                    position=2,
                    queue_length=2,
                )
            )
            await asyncio.sleep(0)  # let the generator reach the bound, as a real pause would
        await stream.resume()
        event, _body = await stream.until("resync", limit=MAX_CLIENT_QUEUE * 3)

    assert event == "resync"
    assert registered[0].pending <= MAX_CLIENT_QUEUE


async def test_a_page_that_loses_the_stream_has_somewhere_to_reread(
    app: FastAPI,
) -> None:
    """The other half of the resync contract: the frame has to be answerable.

    `encore-live.js` answers it by re-fetching `/fragments/…`, so the routes it names must
    exist on the same app. An endpoint that renamed them would leave this protocol correct and
    the room silent, which no test of either half alone would catch.
    """

    from encore.controllers import panels

    app.include_router(panels.router)

    # The advertised paths, not the route objects: FastAPI keeps an included router lazy,
    # so `app.routes` is a list of wrappers without paths until the app has run.
    served = set(app.openapi()["paths"])

    assert {"/events", "/fragments/player", "/fragments/alerts"} <= served


async def test_each_connection_gets_its_own_snapshot(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two connects, two reads.

    The frame is built at connect from the live view rather than copied off somebody else's:
    an appliance with one cached snapshot for all clients would be a jukebox showing a song
    nobody queued, which is how a cache bug becomes a guest's complaint.
    """

    built: list[int] = []
    original = views.player

    def counting(jukebox: Any) -> Any:
        built.append(len(built))
        return original(jukebox)

    monkeypatch.setattr(views, "player", counting)
    async with held_open(app) as first:
        await first.next(1)
        async with held_open(app) as second:
            await second.next(1)

    assert len(built) == 2


async def test_a_reconnect_is_answered_without_a_replay_buffer(app: FastAPI) -> None:
    """`Last-Event-ID`, received and refused politely (SAPRS 9.10's event-id tracking).

    Replay would mean a ring buffer per listener — memory bounded by nobody's imagination. A
    snapshot plus one redraw gets the same result holding nothing, so the header a browser
    sends is accepted by the route and ignored by the code, and the frame that comes back is
    the truth as of now.
    """

    async with held_open(
        app, headers={"accept": "text/event-stream", "last-event-id": "17"}
    ) as stream:
        event, data = (await stream.next(1))[0]

    assert event == SNAPSHOT
    assert json.loads(data)["state"] == "playing"


async def test_nothing_is_sent_at_connect_about_health(app: FastAPI) -> None:
    """The frame that is deliberately absent, and why.

    The shell the browser already has was rendered with the aggregate health in it; a second
    source for that sentence is a second chance to disagree with the first. The test is a
    negative because the plausible mistake — "send health too, it's cheap" — would show up as
    a strip on screen contradicting the page it sits on.
    """

    async with held_open(app) as stream:
        event, data = (await stream.next(1))[0]

    assert event == SNAPSHOT
    assert "health" not in json.loads(data)
