"""`QueueService`: SAPRS Chapter 8, rule by rule.

Persistence is the real `runtime.db` (the `runtime_store` fixture) because positions,
`compact()` and status transitions are exactly the things a fake would be tempted to get
conveniently wrong. What is faked is the two things on either side: the library lookup and
the player, which is the seam the queue is allowed to push on (ADR-011) — and whose events
are the only way back in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from encore.config.models import QueueConfig
from encore.domain import (
    AlbumId,
    ArtistId,
    AudioFormat,
    PlaybackOutcome,
    PlaybackProgress,
    PlaybackState,
    QueueItemId,
    QueueItemStatus,
    Song,
    SongId,
)
from encore.events import (
    Event,
    EventBus,
    HealthChanged,
    PlaybackRecovered,
    QueueAdvanced,
    SongFinished,
    SongQueued,
    SongStarted,
)
from encore.events.playback import FinishedReason
from encore.repositories.runtime import RuntimeStore
from encore.services.errors import QueueFullError, SongNotAvailableError
from encore.services.queue_service import QueueService

START = datetime(2026, 6, 1, 20, 0, tzinfo=UTC)


class FixedClock:
    def __init__(self) -> None:
        self.at = START

    def now(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


class FakeLibrary:
    """`SongLookup`. `None` for a known id is the rebuilt-library case, not an error."""

    def __init__(self, songs: list[Song]) -> None:
        self.songs = {song.id: song for song in songs}
        self.lookups = 0
        self.batch_reads = 0

    def song(self, song_id: SongId) -> Song | None:
        self.lookups += 1
        return self.songs.get(song_id)

    def songs_by_ids(self, ids: object) -> list[Song]:
        self.batch_reads += 1
        wanted = list(ids)  # type: ignore[call-overload]
        return [self.songs[song_id] for song_id in wanted if song_id in self.songs]

    def forget(self, song_id: SongId) -> None:
        """Stand in for a library rebuild that dropped a track someone queued (SAPRS 14.10)."""

        self.songs.pop(song_id, None)


class FakePlayer:
    """The playback contract as the queue sees it.

    `unplayable` is the interesting part: a file mpv cannot open makes `play()` publish
    `SongFinished(FAILED)` *from inside the call*, which is the re-entrant path that would
    otherwise nest one `SongFinished` handler per broken file.
    """

    def __init__(
        self,
        *,
        events: EventBus,
        clock: FixedClock,
        idle: bool = True,
        unplayable: set[SongId] | None = None,
        refuses: set[SongId] | None = None,
    ) -> None:
        self._events = events
        self._clock = clock
        self._idle = idle
        self.unplayable = unplayable or set()
        self.refuses = refuses or set()
        self.state = PlaybackState.IDLE
        self.started: list[SongId] = []
        self.skips = 0
        self.stops = 0
        self.pauses = 0
        self.resumes = 0
        self.current: Current | None = None

    @property
    def is_idle(self) -> bool:
        return self._idle

    @property
    def progress(self) -> PlaybackProgress:
        return PlaybackProgress(state=self.state, position=timedelta(0), duration=timedelta(0))

    def set_idle(self, idle: bool) -> None:
        """Stand in for the engine becoming available or busy, from the outside."""

        self._idle = idle
        if idle:
            self.current = None
            self.state = PlaybackState.IDLE

    def play(self, song: Song, *, queue_item_id: QueueItemId) -> bool:
        if song.id in self.unplayable:
            self._events.publish(
                SongFinished(
                    song_id=song.id,
                    queue_item_id=queue_item_id,
                    reason=FinishedReason.FAILED,
                    occurred_at=self._clock.now(),
                )
            )
            return False
        if song.id in self.refuses:
            # Worse than a failure: an engine that says no and reports nothing. The queue must
            # still not mark the item playing forever, which is what `unreported_refusal`
            # exercises from the other side.
            return False
        self.started.append(song.id)
        self.current = Current(song_id=song.id, queue_item_id=queue_item_id)
        self._idle = False
        self.state = PlaybackState.PLAYING
        self._events.publish(
            SongStarted(
                song_id=song.id,
                queue_item_id=queue_item_id,
                occurred_at=self._clock.now(),
            )
        )
        return True

    def finish(self, reason: FinishedReason = FinishedReason.COMPLETED) -> None:
        """The engine reaching the end of the track, which the queue cannot cause."""

        if self.current is None:
            return
        song_id, item_id = self.current.song_id, self.current.queue_item_id
        self.current = None
        self._idle = True
        self.state = PlaybackState.IDLE
        self._events.publish(
            SongFinished(
                song_id=song_id,
                queue_item_id=item_id,
                reason=reason,
                completion=1.0 if reason is FinishedReason.COMPLETED else 0.5,
                occurred_at=self._clock.now(),
            )
        )

    def pause(self) -> bool:
        self.pauses += 1
        self.state = PlaybackState.PAUSED
        return True

    def resume(self) -> bool:
        self.resumes += 1
        self.state = PlaybackState.PLAYING
        return True

    def stop(self) -> None:
        self.stops += 1
        self.finish(FinishedReason.STOPPED)

    def skip(self) -> None:
        self.skips += 1
        self.finish(FinishedReason.SKIPPED)


@dataclass(frozen=True, slots=True, kw_only=True)
class Current:
    """`CurrentTrack`: the two ids the queue needs from the playing track."""

    song_id: SongId
    queue_item_id: QueueItemId


def player_item_id(service: QueueService, index: int) -> QueueItemId:
    """The queue id of the `index`th item, for tests that publish the engine's facts."""

    return service.active()[index].id


def _song(id_: int) -> Song:
    return Song(
        id=SongId(id_),
        title=f"Track {id_}",
        artist_id=ArtistId(1),
        album_id=AlbumId(1),
        file_path=Path(f"/music/artist/album/track-{id_}.mp3"),
        file_format=AudioFormat.MP3,
        duration=timedelta(seconds=180),
    )


SONGS = [_song(number) for number in range(1, 13)]


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def bus() -> EventBus:
    return EventBus(logger=logging.getLogger("encore.test.queue"))


@pytest.fixture
def library() -> FakeLibrary:
    return FakeLibrary(SONGS)


@pytest.fixture
def seen(bus: EventBus) -> list[object]:
    """Every fact the system published, in order.

    Subscribing to `Event` is the bus's documented way to receive everything, which is what
    a recorder wants and what no service should ever do.
    """

    events: list[object] = []
    bus.subscribe(Event, events.append, name="recorder")
    return events


def _service(
    store: RuntimeStore,
    library: FakeLibrary,
    player: FakePlayer,
    bus: EventBus,
    clock: FixedClock,
    *,
    max_items: int = 200,
) -> QueueService:
    service = QueueService(
        store=store,
        library=library,
        player=player,
        events=bus,
        config=QueueConfig(max_items=max_items),
        clock=clock,
        logger=logging.getLogger("encore.test.queue"),
    )
    service.start()
    return service


# -- accepting a request (SAPRS 8.2, 8.3, 8.4) ----------------------------


def test_a_request_on_a_silent_appliance_starts_immediately(
    runtime_store: RuntimeStore,
    library: FakeLibrary,
    bus: EventBus,
    clock: FixedClock,
    seen: list[object],
) -> None:
    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)

    item = service.enqueue(SONGS[0].id)

    assert player.started == [SONGS[0].id]
    assert item.status is QueueItemStatus.PLAYING
    assert [type(event).__name__ for event in seen] == [
        "SongQueued",
        "QueueAdvanced",
        "SongStarted",
    ]


def test_a_request_while_playing_waits_its_turn(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)
    service.enqueue(SONGS[0].id)

    second = service.enqueue(SONGS[1].id)

    assert second.position == 2
    assert player.started == [SONGS[0].id], "the first song keeps playing"


def test_the_same_song_queued_twice_is_two_items(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    """SAPRS 8.3 is a prohibition, so the test is the implementation of it."""

    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)

    first = service.enqueue(SONGS[2].id)
    second = service.enqueue(SONGS[2].id)

    assert first.id != second.id
    assert [item.song_id for item in service.active()] == [SONGS[2].id, SONGS[2].id]


def test_a_request_for_a_song_that_is_not_in_the_library_is_refused(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)

    with pytest.raises(SongNotAvailableError):
        service.enqueue(SongId(9999))

    assert service.length == 0
    assert player.started == []


def test_the_queue_ceiling_is_enforced_and_says_what_it_is(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock, max_items=3)
    service.enqueue(SONGS[0].id)

    for number in (1, 2):
        service.enqueue(SONGS[number].id)

    with pytest.raises(QueueFullError, match="3"):
        service.enqueue(SONGS[3].id)

    assert service.length == 3, "the refusal wrote nothing"


def test_a_refusal_at_the_ceiling_publishes_no_event(
    runtime_store: RuntimeStore,
    library: FakeLibrary,
    bus: EventBus,
    clock: FixedClock,
    seen: list[object],
) -> None:
    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock, max_items=1)
    service.enqueue(SONGS[0].id)
    seen.clear()

    with pytest.raises(QueueFullError):
        service.enqueue(SONGS[1].id)

    assert seen == [], "a refusal is not a fact about the queue; nothing changed"


# -- the order (SAPRS 8.1, 8.5) -------------------------------------------


def test_order_is_arrival_order_and_survives_everything_else(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)
    for number in (0, 1, 2, 3):
        service.enqueue(SONGS[number].id)

    player.finish()  # track 1 ends
    player.finish()  # track 2 ends

    assert player.started == [SONGS[0].id, SONGS[1].id, SONGS[2].id]
    assert [entry.item.position for entry in service.up_next()] == [2]
    assert [entry.song for entry in service.up_next()] == [SONGS[3]]


def test_removal_keeps_the_order_and_closes_the_gap(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)
    for number in (0, 1, 2, 3):
        service.enqueue(SONGS[number].id)
    waiting = service.up_next()
    middle = waiting[1].item

    assert service.remove(middle.id) is True

    assert [entry.item.position for entry in service.up_next()] == [2, 3]
    assert [entry.song for entry in service.up_next()] == [SONGS[1], SONGS[3]]
    assert player.skips == 0, "a waiting request was removed, not the track on the speakers"


def test_removing_the_playing_item_skips_it(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    """The alternative is a silent appliance with a queue that looks stuck (SAPRS 8.8)."""

    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)
    service.enqueue(SONGS[0].id)
    service.enqueue(SONGS[1].id)
    playing = service.now_playing()
    assert playing is not None

    assert service.remove(playing.item.id) is True

    assert player.started == [SONGS[0].id, SONGS[1].id]
    assert service.length == 1


def test_clearing_takes_the_waiting_requests_and_leaves_the_music_alone(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)
    for number in (0, 1, 2):
        service.enqueue(SONGS[number].id)

    removed = service.clear()

    assert removed == 2
    assert player.stops == 0, "clear means stop taking requests, not stop the amplifier"
    assert service.length == 1
    assert service.now_playing() is not None


def test_removing_something_that_is_not_there_reports_false(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)

    assert service.remove(QueueItemId(4242)) is False


# -- advancement (SAPRS 8.7) ----------------------------------------------


def test_a_finished_track_moves_the_queue_on_by_itself(
    runtime_store: RuntimeStore,
    library: FakeLibrary,
    bus: EventBus,
    clock: FixedClock,
    seen: list[object],
) -> None:
    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)
    service.enqueue(SONGS[0].id)
    service.enqueue(SONGS[1].id)
    seen.clear()

    player.finish()

    assert player.started == [SONGS[0].id, SONGS[1].id]
    assert [type(event).__name__ for event in seen] == [
        "SongFinished",
        "QueueAdvanced",
        "SongStarted",
    ]
    statuses = [item.status for item in service.active()]
    assert statuses == [QueueItemStatus.PLAYING], "the finished row left the active set"


def test_every_ending_writes_one_history_row_with_its_outcome(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)
    service.enqueue(SONGS[0].id)
    player.finish(FinishedReason.COMPLETED)
    player.finish()  # nothing playing; must not write a second row
    service.enqueue(SONGS[1].id)

    player.skip()

    with runtime_store.unit_of_work() as work:
        rows = work.history.recent(limit=10)
        assert [(row.song_id, row.outcome) for row in rows] == [
            (SONGS[1].id, PlaybackOutcome.SKIPPED),
            (SONGS[0].id, PlaybackOutcome.COMPLETED),
        ]
        assert rows[-1].completion == pytest.approx(1.0)


def test_a_skip_records_a_skip_and_not_a_play(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    """SAPRS 10.3's "songs played" is a claim to a host, so a skipped request is not one."""

    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)
    service.enqueue(SONGS[0].id)

    service.skip()

    with runtime_store.unit_of_work() as work:
        (row,) = work.history.recent(limit=5)
        assert row.outcome is PlaybackOutcome.SKIPPED
        assert not row.outcome.counts_as_played


def test_stop_returns_the_song_to_the_head_of_the_queue(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    """SAPRS 8.5: stopping is not finishing, and the next request goes behind it."""

    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)
    service.enqueue(SONGS[0].id)
    service.enqueue(SONGS[1].id)

    service.stop()

    assert [item.song_id for item in service.active()] == [SONGS[0].id, SONGS[1].id]
    assert [item.status for item in service.active()] == [
        QueueItemStatus.PENDING,
        QueueItemStatus.PENDING,
    ]
    assert player.started == [SONGS[0].id], "stop holds; it does not start the next song"


def test_the_next_request_after_a_stop_plays_the_head_not_the_request(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    """Idle means "play what is waiting", or stop-then-queue would overtake the queue."""

    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)
    service.enqueue(SONGS[0].id)
    service.stop()
    player.started.clear()

    service.enqueue(SONGS[1].id)

    assert player.started == [SONGS[0].id], "the stopped song was never finished, so it is next"
    assert [entry.item.position for entry in service.up_next()] == [2]


def test_pausing_and_resuming_cost_the_queue_no_events(
    runtime_store: RuntimeStore,
    library: FakeLibrary,
    bus: EventBus,
    clock: FixedClock,
    seen: list[object],
) -> None:
    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)
    service.enqueue(SONGS[0].id)
    seen.clear()

    assert service.pause() is True
    assert service.resume() is True

    assert seen == []
    assert player.pauses == 1
    assert player.resumes == 1


# -- what the room sees (SAPRS 8.4, 9.9) ----------------------------------


def test_up_next_names_the_songs_in_one_read(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)
    for number in range(6):
        service.enqueue(SONGS[number].id)
    library.batch_reads = 0

    entries = service.up_next(limit=5)

    assert len(entries) == 5
    assert library.batch_reads == 1, "a list of six rows must not cost six queries"
    assert entries[0].available
    assert entries[0].song is not None


def test_a_song_that_left_the_library_is_still_counted_and_named_as_unavailable(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    """A queue that quietly shortens itself in the UI is how a party loses its place."""

    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)
    service.enqueue(SONGS[0].id)
    service.enqueue(SONGS[1].id)
    library.forget(SONGS[1].id)

    entries = service.up_next()

    assert len(entries) == 1
    assert entries[0].song is None
    assert entries[0].available is False


def test_wait_time_measures_the_front_of_the_queue(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)
    service.enqueue(SONGS[0].id)
    service.enqueue(SONGS[1].id)
    clock.advance(30)

    assert service.wait_time() == timedelta(seconds=30), "the request that is next in line"

    player.finish()

    assert service.wait_time() == timedelta(0), "nothing is waiting any more"


def test_an_empty_queue_waits_zero_seconds(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)

    assert service.wait_time() == timedelta(0)


# -- a library that outlived its queue (SAPRS 14.10) ----------------------


def test_a_queued_song_that_vanished_is_dropped_and_the_queue_keeps_going(
    runtime_store: RuntimeStore,
    library: FakeLibrary,
    bus: EventBus,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)
    service.enqueue(SONGS[0].id)
    service.enqueue(SONGS[1].id)
    service.enqueue(SONGS[2].id)
    library.forget(SONGS[1].id)

    player.finish()

    assert player.started == [SONGS[0].id, SONGS[2].id]
    assert service.length == 1
    assert "no longer in the library" in caplog.text


def test_twenty_files_the_engine_cannot_open_do_not_nest_twenty_handlers(
    runtime_store: RuntimeStore, bus: EventBus, clock: FixedClock
) -> None:
    """The recursion this prevents is real: one `SongFinished` delivered inside another.

    `EventBus` raises `EventCycleError` at its nesting limit, so a handler that advanced by
    recursion would end a party with a traceback after the 32nd unreadable file.
    """

    broken = [_song(number) for number in range(100, 120)]
    library = FakeLibrary(broken)
    player = FakePlayer(events=bus, clock=clock, idle=False, unplayable={s.id for s in broken})
    service = QueueService(
        store=runtime_store,
        library=library,
        player=player,
        events=bus,
        config=QueueConfig(),
        clock=clock,
    )
    service.start()
    for song in broken:
        service.enqueue(song.id)
    player.set_idle(True)

    service.play_next()

    assert service.length == 0, "every unreadable file was taken out"
    assert player.started == []


# -- restart and recovery (SAPRS 8.6, 7.6) -------------------------------


def test_a_restart_leaves_the_queue_waiting_and_silent(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    player = FakePlayer(events=bus, clock=clock)
    before = _service(runtime_store, library, player, bus, clock)
    first = before.enqueue(SONGS[0].id)
    before.enqueue(SONGS[1].id)
    before.close()

    # A new process: nothing is playing, and the database still says the first item is.
    fresh_player = FakePlayer(events=bus, clock=clock)
    after = QueueService(
        store=runtime_store,
        library=library,
        player=fresh_player,
        events=bus,
        config=QueueConfig(),
        clock=clock,
    )

    restored = after.start()

    assert restored == 1
    assert [item.status for item in after.active()] == [
        QueueItemStatus.PENDING,
        QueueItemStatus.PENDING,
    ]
    assert after.active()[0].id == first.id
    assert fresh_player.started == [], "a server that began making noise before a guest arrived"


def test_recovery_continues_the_queue_from_the_head(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)
    service.enqueue(SONGS[0].id)
    service.enqueue(SONGS[1].id)
    player.started.clear()
    player.set_idle(False)
    bus.publish(
        SongFinished(
            song_id=SONGS[0].id,
            queue_item_id=player_item_id(service, 0),
            reason=FinishedReason.FAILED,
            occurred_at=clock.now(),
        )
    )
    assert player.started == [], "an engine in Error cannot be given the next track"

    player.set_idle(True)
    bus.publish(
        PlaybackRecovered(
            reason="mpv exited with code -11",
            restart_count=1,
            resumed_state=PlaybackState.IDLE,
            occurred_at=clock.now(),
        )
    )

    assert player.started == [SONGS[1].id], "the head is chosen by arrival, not by what died"


def test_recovery_while_something_is_still_playing_starts_nothing(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)
    service.enqueue(SONGS[0].id)
    player.started.clear()

    bus.publish(
        PlaybackRecovered(
            reason="restart",
            restart_count=1,
            resumed_state=PlaybackState.PLAYING,
            occurred_at=clock.now(),
        )
    )

    assert player.started == []


def test_an_unrelated_event_does_not_touch_the_queue(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    from encore.domain.health import HealthStatus

    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)
    service.enqueue(SONGS[0].id)
    player.started.clear()

    bus.publish(
        HealthChanged(
            component="library",
            status=HealthStatus.DEGRADED,
            detail="a rebuild finished",
            occurred_at=clock.now(),
        )
    )

    assert player.started == []


# -- events ---------------------------------------------------------------


def test_a_queued_event_carries_no_guest_identity(
    runtime_store: RuntimeStore,
    library: FakeLibrary,
    bus: EventBus,
    clock: FixedClock,
    seen: list[object],
) -> None:
    """Anonymous guests are an invariant, and events are where it could leak (SAPRS 8.9)."""

    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)

    service.enqueue(SONGS[0].id)

    for event in seen:
        assert is_dataclass(event), "every fact on the bus is a frozen dataclass"
        names = [field.name for field in fields(event)]
        assert not [name for name in names if "guest" in name or "session" in name]


def test_the_queue_length_in_an_event_never_understates_the_queue(
    runtime_store: RuntimeStore,
    library: FakeLibrary,
    bus: EventBus,
    clock: FixedClock,
    seen: list[object],
) -> None:
    """The UI's "3rd in line of 3" comes from these two numbers, so they must agree."""

    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)

    for number in range(4):
        service.enqueue(SONGS[number].id)

    queued = [event for event in seen if isinstance(event, SongQueued)]
    advanced = [event for event in seen if isinstance(event, QueueAdvanced)]
    assert [event.position for event in queued] == [1, 2, 3, 4]
    assert all(event.queue_length >= event.position for event in queued)
    assert advanced, "the queue moved without saying so"


def test_the_service_stops_listening_when_closed(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)
    service.enqueue(SONGS[0].id)
    service.enqueue(SONGS[1].id)
    service.close()
    player.started.clear()

    player.finish()

    assert player.started == [], "the queue heard nothing, so it moved for nothing"
    assert service.length == 2, "nothing advanced, so nothing left the active set"


# -- the paths that only exist because a collaborator can be wrong ---------


def test_an_engine_that_refuses_without_saying_so_does_not_hold_the_queue_open(
    runtime_store: RuntimeStore,
    library: FakeLibrary,
    bus: EventBus,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`play()` returning False with no event is a contradiction, and it is handled as one.

    Every honest engine settles the item itself — a real mpv that cannot open a file produces
    `SongFinished(FAILED)` from inside the call, which is `unplayable`'s path. An engine that
    refuses silently is not modelled anywhere in the playback package, so the queue's only
    correct answer is its own: the item was never started, so it cannot be left `PLAYING`, and
    the log says that a contract was broken rather than hiding it behind a working queue.
    """

    player = FakePlayer(events=bus, clock=clock, refuses={SONGS[0].id})
    service = _service(runtime_store, library, player, bus, clock)

    with caplog.at_level(logging.ERROR, logger="encore.test.queue"):
        first = service.enqueue(SONGS[0].id)
        second = service.enqueue(SONGS[1].id)

    assert "the engine refused a track without reporting it" in caplog.text
    active = service.active()
    assert [item.status for item in active] == [QueueItemStatus.PLAYING]
    assert active[0].id == second.id, "the second request is what is playing, not the refused one"
    assert service.length == 1
    refused = service.entry(first.id)
    assert refused is not None
    assert refused.item.status is QueueItemStatus.REMOVED, (
        "the row survives as a record, off the list"
    )


def test_now_playing_says_nothing_when_the_row_it_pointed_at_is_gone(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    """The gap between "the engine is playing" and "the queue has a row for it".

    Reachable the way an administrative removal is reachable — a row deleted from under a
    playing track — and it matters because `now_playing()` reporting the dead item's title
    would put a phantom in the UI's Now Playing panel, which is the one screen every guest
    looks at to check whether the appliance heard them.
    """

    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)
    service.enqueue(SONGS[0].id)
    assert service.now_playing() is not None

    with runtime_store.unit_of_work() as work:
        work.queue.remove(player_item_id(service, 0))

    assert service.now_playing() is None, (
        "the engine still plays, but nothing may be claimed about it"
    )
    player.finish()
    assert service.length == 0


def test_an_entry_reports_its_position_and_what_it_resolved_to(
    runtime_store: RuntimeStore, library: FakeLibrary, bus: EventBus, clock: FixedClock
) -> None:
    """`QueueEntry` is what the UI renders, so its two conveniences are behaviour.

    `position` is the row's queue position rather than an index into a list, and `available`
    is how a fragment says "this track is no longer in the library" without the template
    asking a repository — which would be the guardrail failing in the friendliest way, and
    the reason the row stays listed at all (SAPRS 9.9).
    """

    player = FakePlayer(events=bus, clock=clock)
    service = _service(runtime_store, library, player, bus, clock)
    service.enqueue(SONGS[0].id)
    second = service.enqueue(SONGS[1].id)

    entry = service.entry(second.id)
    assert entry is not None
    assert entry.position == 2
    assert entry.available is True
    assert entry.song is not None
    assert entry.song.id == SONGS[1].id

    library.forget(SONGS[1].id)
    gone = service.entry(second.id)
    assert gone is not None, "the row exists"
    assert gone.available is False, "the song does not"
    assert gone.song is None
