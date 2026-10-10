"""`SSEPublisher`: one fact in, N streams out, and a slow phone that costs nothing.

SAPRS 11.9's sentence is the design: a client that cannot keep up must not affect playback.
Three properties carry it, and all three are testable without a browser:

* **`offer` never blocks and never raises.** It is called from the appliance thread, inside
  a bus handler, inside `QueueService.enqueue`. A queue that stalled because one phone in
  the room had a bad radio would be this milestone's most serious bug, and it is exactly
  what a naive `run_coroutine_threadsafe(...).result()` would ship.
* **A publication costs O(clients) queue pushes and nothing else.** The render-once decision
  lives in `encore/api/fragments.py`; this module's half is that a second client is a second
  `append`, never a second template.
* **A client that falls behind is told to resync rather than handed a gap.** The alternative
  — an unbounded queue — is memory bounded by how slow the network is, and silent drops are
  a screen that never updates.

The two sides of the boundary are real here: the loop is pytest-asyncio's, and every
publication is made from a short-lived thread, because `Stream.offer` reaches the loop with
`call_soon_threadsafe` and called from the loop's own thread that degrades to `call_soon` —
which is the difference between a test that proves the arrangement and one that imitates it.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from encore.domain import QueueItemId, SongId
from encore.domain.health import HealthStatus
from encore.domain.playback import PlaybackProgress, PlaybackState
from encore.events import (
    EVENT_VOCABULARY,
    Event,
    EventBus,
    FinishedReason,
    HealthChanged,
    LibraryReloaded,
    QueueAdvanced,
    SongFinished,
    SongQueued,
    SongStarted,
)
from encore.services.sse_publisher import (
    MAX_CLIENT_QUEUE,
    Frame,
    SSEPublisher,
    Stream,
    to_json,
)


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def publisher(bus: EventBus) -> Iterator[SSEPublisher]:
    made = SSEPublisher(events=bus)
    made.start()
    try:
        yield made
    finally:
        made.stop()


def publish(bus: EventBus, event: Event) -> None:
    """Send a fact from another thread, because that is where the appliance sends it.

    The joining is not the point — a bus dispatch is synchronous, so the publication has
    reached every stream by the time the thread ends, exactly as `QueueService.enqueue`
    returns with the room already told.
    """

    thread = threading.Thread(target=lambda: bus.publish(event), name="encore-test-appliance")
    thread.start()
    thread.join(2.0)


async def next_frame(stream: Stream, *, within: float = 2.0) -> tuple[str, str]:
    """Read one frame, on the loop the stream was registered with.

    `Stream.next` must be awaited there — its wake-up is an `asyncio.Event`, which is not
    thread-safe by design — and that is the same rule `/events` follows in the application.
    """

    frame = await asyncio.wait_for(stream.next(), within)
    assert frame is not None, "the stream ended before the frame arrived"
    return frame.event, frame.data


async def read_all(stream: Stream, count: int, *, within: float = 5.0) -> list[tuple[str, str]]:
    async def read() -> list[tuple[str, str]]:
        return [await next_frame(stream) for _ in range(count)]

    return await asyncio.wait_for(read(), within)


def connect(publisher: SSEPublisher) -> Stream:
    """Register a client on the running loop, which is where its response lives."""

    return publisher.connect(loop=asyncio.get_running_loop())


# -- naming (SAPRS 10.5, ADR-004) -----------------------------------------


async def test_a_fact_arrives_under_its_class_name(bus: EventBus, publisher: SSEPublisher) -> None:
    """One naming rule, enforced in the code that has to obey it.

    `encore/events/base.py` says a class name is the name a fact carries on the wire, and
    SAPRS 10.5 lists the eight by those names. A translation to snake_case here would be a
    second vocabulary to keep in step with the first, which is how `docs/api/README.md`
    starts lying.
    """

    stream = connect(publisher)

    publish(bus, SongStarted(song_id=SongId(7), queue_item_id=QueueItemId(3)))

    event, data = await next_frame(stream)
    assert event == "SongStarted"
    assert json.loads(data)["song_id"] == 7


def test_the_stream_covers_the_whole_event_vocabulary() -> None:
    """A ninth fact would reach a browser the day it landed, and show up unwired in the UI.

    `EVENT_NAMES` is derived from `EVENT_VOCABULARY` rather than typed beside it, so the diff
    for a new fact is this assertion passing unchanged and `test_fragments.py` showing the new
    name with an empty region list: seen, and deliberately not drawn.
    """

    from encore.services.sse_publisher import EVENT_NAMES

    assert {event.__name__ for event in EVENT_VOCABULARY} == set(EVENT_NAMES.values())


# -- fan-out (SAPRS 9.10, 11.9) -------------------------------------------


async def test_one_fact_reaches_every_client(bus: EventBus, publisher: SSEPublisher) -> None:
    streams = [connect(publisher) for _ in range(5)]

    publish(bus, QueueAdvanced(finished_queue_item_id=None, now_playing=None, queue_length=3))

    for stream in streams:
        event, data = await next_frame(stream)
        assert event == "QueueAdvanced"
        assert json.loads(data)["queue_length"] == 3


async def test_a_client_that_never_reads_does_not_hold_up_a_second_one(
    bus: EventBus, publisher: SSEPublisher
) -> None:
    """SAPRS 11.9 in one assertion.

    `busy` is connected and never drained, so its queue fills and starts dropping. If a full
    queue blocked `offer`, the publication would never return, the bus handler would never
    finish, and the request that queued the song would be waiting on a phone in a pocket.
    """

    busy = connect(publisher)
    quick = connect(publisher)

    for index in range(MAX_CLIENT_QUEUE * 3):
        publish(bus, LibraryReloaded(song_count=index, version=f"{index}"))

    # The drops are counted on the loop, in `Stream._accept`, and a synchronous stretch of
    # this test is a stretch the loop is not running: the wait is not impatience, it is the
    # same hand-off `/events` makes in production.
    await asyncio.sleep(0.05)

    assert busy.dropped >= MAX_CLIENT_QUEUE * 2, "a full queue sheds rather than stalls"
    assert publisher.stats["dropped"] == busy.dropped + quick.dropped
    event, _ = await next_frame(quick)
    assert event == "LibraryReloaded"


async def test_a_dropping_client_is_told_to_resync(bus: EventBus, publisher: SSEPublisher) -> None:
    """The frame that makes a drop recoverable instead of silent.

    Without it the browser's screen is a stale page that looks current, which is the failure
    SAPRS 9.10's "update without a full page reload" is specifically about.
    """

    stream = connect(publisher)

    for index in range(MAX_CLIENT_QUEUE + 4):
        publish(bus, LibraryReloaded(song_count=index, version=str(index)))

    # The bound is the queue's own size: a dropped frame is gone, and the point of the
    # resync is that what remains says so rather than looking continuous.
    events = [event for event, _ in await read_all(stream, MAX_CLIENT_QUEUE)]
    assert "resync" in events, "a gap must be announced, not left to be noticed"
    assert events.count("resync") == 1, "one warning per overflow episode, not per frame"
    assert stream.dropped >= 4


async def test_a_disconnected_client_stops_the_fan_out(
    bus: EventBus, publisher: SSEPublisher
) -> None:
    """The leak this milestone could create: a queue with no reader, forever.

    Forty phones a night, each with a stream nobody drains, is how a 4 GB board runs out of
    memory by Sunday. `disconnect` is called from the endpoint generator's `finally`, which
    is the only cleanup a cancelled stream gets.
    """

    first = connect(publisher)
    second = connect(publisher)
    publisher.disconnect(first)

    publish(
        bus,
        SongFinished(
            song_id=SongId(1), queue_item_id=QueueItemId(1), reason=FinishedReason.COMPLETED
        ),
    )

    assert publisher.clients == 1
    event, _ = await next_frame(second)
    assert event == "SongFinished"


async def test_a_frame_offered_after_disconnect_is_ignored(
    bus: EventBus, publisher: SSEPublisher
) -> None:
    """The race a real shutdown has: the bus is mid-publication as the browser leaves.

    The stream is closed, the frame has nowhere to go, and nothing may raise into the
    publisher's loop for a client that has already gone.
    """

    stream = connect(publisher)
    publisher.disconnect(stream)

    publish(bus, SongStarted(song_id=SongId(1), queue_item_id=QueueItemId(1)))

    assert await stream.next() is None
    assert stream.pending == 0


async def test_stop_releases_every_waiting_reader(publisher: SSEPublisher) -> None:
    """A held-open `/events` generator ends, so uvicorn's shutdown can finish.

    Without this the process waits for a client that will never send another byte, and
    `systemctl stop` becomes `systemctl kill` (SAPRS 11.8's step 2).
    """

    stream = connect(publisher)
    waiting = asyncio.create_task(stream.next())
    await asyncio.sleep(0)

    publisher.stop()

    assert await asyncio.wait_for(waiting, 2.0) is None
    assert publisher.clients == 0


async def test_a_second_start_resubscribes(bus: EventBus, publisher: SSEPublisher) -> None:
    """`start()` is idempotent and `stop()` is not fatal, because a lifespan can restart.

    A test client that connects after a previous app's shutdown should not find a publisher
    that has forgotten how to broadcast.
    """

    publisher.stop()
    publisher.start()
    stream = connect(publisher)

    publish(bus, SongStarted(song_id=SongId(2), queue_item_id=QueueItemId(1)))

    event, _ = await next_frame(stream)
    assert event == "SongStarted"


# -- the transport's own frames -------------------------------------------


async def test_push_html_sends_a_region_not_a_fact(publisher: SSEPublisher) -> None:
    """A browser that subscribes to `swap:player` need not know the domain's verbs.

    Which facts redraw the panel is `encore/api/fragments.py`'s table and the appliance's
    business; teaching a client that `SongFinished` and `QueueAdvanced` are different events
    would be teaching it something that changes whenever the presentation does.
    """

    stream = connect(publisher)

    publisher.push_html("swap:player", '<section id="player">ok</section>')

    event, data = await next_frame(stream)
    assert event == "swap:player"
    assert data == '<section id="player">ok</section>'


async def test_a_progress_beat_is_silent_when_nothing_is_audible(
    publisher: SSEPublisher,
) -> None:
    """SAPRS 9.10's "use SSE rather than polling", without the idle cost of a heartbeat.

    A silent room at 2 a.m. should not be forty frames a second. The transport's own pings
    still say the connection is alive, so nothing is lost by being quiet about a second that
    passed over nothing.
    """

    stream = connect(publisher)

    publisher.beat(PlaybackProgress(state=PlaybackState.IDLE))

    assert stream.pending == 0


async def test_a_progress_beat_carries_the_numbers_a_bar_needs(
    publisher: SSEPublisher,
) -> None:
    stream = connect(publisher)

    publisher.beat(
        PlaybackProgress(
            state=PlaybackState.PLAYING,
            song_id=SongId(4),
            position=timedelta(seconds=30),
            duration=timedelta(seconds=120),
        )
    )

    event, data = await next_frame(stream)
    payload = json.loads(data)
    assert event == "progress"
    assert (payload["song_id"], payload["position_ms"], payload["fraction"]) == (4, 30000, 0.25)


async def test_a_beat_for_a_track_of_unknown_length_reports_zero(
    publisher: SSEPublisher,
) -> None:
    """`PlaybackProgress.fraction`'s rule, at the wire: display data never raises.

    A corrupt file's duration is 0, and a frame that divided by it would end the stream for
    every client in the room over one bad row (SAPRS 6.7, 11.9).
    """

    stream = connect(publisher)

    publisher.beat(
        PlaybackProgress(
            state=PlaybackState.PLAYING, song_id=SongId(9), position=timedelta(seconds=12)
        )
    )

    _, data = await next_frame(stream)
    assert json.loads(data)["fraction"] == 0.0


# -- the payload ----------------------------------------------------------


def test_a_fact_serialises_as_data_not_as_a_python_repr() -> None:
    """`datetime` becomes ISO 8601 and an enum becomes its value.

    The default encoder would raise on both, and a publisher that swallowed the error would
    be a stream that quietly stopped for everybody.
    """

    text = to_json(
        SongStarted(
            song_id=SongId(3),
            queue_item_id=QueueItemId(1),
            occurred_at=datetime(2026, 6, 1, 21, 30, tzinfo=UTC),
        )
    )

    assert '"song_id":3' in text
    assert "2026-06-01T21:30:00" in text
    assert "datetime(" not in text


def test_a_path_and_a_timedelta_survive_the_encoder() -> None:
    """The domain holds `Path` and `timedelta`; a browser holds strings.

    `default=str` in `to_json` is the two lines that make that true, and it is why this
    module does not import a web framework's encoder (SAPRS 11.10).
    """

    assert '"/music/a.flac"' in to_json({"where": Path("/music/a.flac")})
    # A duration becomes milliseconds rather than `str(timedelta)`: a browser does
    # arithmetic on it, and `to_json` is the encoder for the whole stream.
    assert to_json({"length": timedelta(minutes=1)}) == '{"length":60000}'


def test_frame_ids_are_monotonic_within_one_publisher() -> None:
    """An `id:` a browser can compare, and a reconnect that deliberately ignores it.

    The ids exist so a stalled client's log can say "quiet after frame 4,212" rather than
    "at about 9pm". Replay from them is not implemented, because a snapshot cannot drift —
    see `encore/api/sse.py`.
    """

    publisher = SSEPublisher(events=EventBus())
    ids = [publisher._next_id() for _ in range(4)]

    assert ids == sorted(ids)
    assert len(set(ids)) == 4


async def test_a_stream_with_no_reader_holds_a_bounded_number_of_frames() -> None:
    """`MAX_CLIENT_QUEUE` is a memory claim, so it is measured rather than described.

    A `Stream` whose deque grew without limit would look fine on a laptop with one browser
    open and would end a party on a Pi with thirty. The `resync` frame is counted against the
    bound rather than added to it, which is what `<= MAX_CLIENT_QUEUE` means here.
    """

    stream = Stream(loop=asyncio.get_running_loop())

    for index in range(MAX_CLIENT_QUEUE * 5):
        stream.offer(Frame(event="LibraryReloaded", data=f'{{"song_count":{index}}}'))
    await asyncio.sleep(0.05)  # let the loop take each frame, as a client's reader would

    assert stream.pending <= MAX_CLIENT_QUEUE, "the bound is the memory claim, not a hint"
    assert stream.dropped >= MAX_CLIENT_QUEUE * 4 - 1


async def test_health_frames_reach_clients(bus: EventBus, publisher: SSEPublisher) -> None:
    """`HealthChanged` is a fact, and the alert strip is drawn from it (SAPRS 11.6)."""

    stream = connect(publisher)

    publish(
        bus,
        HealthChanged(
            status=HealthStatus.DEGRADED,
            previous=HealthStatus.HEALTHY,
            component="playback",
            detail="mpv restarted",
        ),
    )

    event, data = await next_frame(stream)
    assert event == "HealthChanged"
    assert json.loads(data)["component"] == "playback"


async def test_song_queued_is_a_fact_a_browser_can_hear(
    bus: EventBus, publisher: SSEPublisher
) -> None:
    """`SongQueued` is broadcast even though no region is redrawn by it.

    The machine-readable interface and the human one answer to different audiences: an
    observer watching the stream should see a request land whether or not a screen changes,
    and the guest who made it is answered by the POST itself (see `fragments.py`).
    """

    stream = publisher.connect(loop=asyncio.get_running_loop())

    publish(
        bus, SongQueued(song_id=SongId(1), queue_item_id=QueueItemId(1), position=1, queue_length=1)
    )

    event, data = await next_frame(stream)
    assert event == "SongQueued"
    assert json.loads(data)["position"] == 1
