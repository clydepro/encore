"""A party, without the room: search, queue, playback and both databases working together.

This is the arrangement the server will run in milestone 11 — a real read-only `library.db`
produced by the Builder, a real read-write `runtime.db`, the event bus, and mpv replaced by
`tests/support/mpv.py`'s double — with one thing missing: no HTTP, no templates, no SSE.
Those are later steps, and leaving them out keeps the failures in this file about the
jukebox rather than about a request.

The value here is the seams. Each half is unit-tested against its own contract; only in this
file does a queue item resolved against a library row reach an mpv command line, which is
where a `SongId` that stopped existing, a position that stopped meaning FIFO, or a status
nobody settles would show up.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from encore.config.models import AudioConfig, PlaybackConfig, QueueConfig, SearchConfig
from encore.domain import PlaybackOutcome, PlaybackState, QueueItemStatus, Song, SongId
from encore.events import Event, EventBus, SongFinished, SongStarted
from encore.playback import MpvPlayer, PlaybackService, PlaybackSupervisor, TransitionPolicy
from encore.playback.errors import MpvCommandError
from encore.repositories.library import LibraryStore
from encore.repositories.runtime import RuntimeStore
from encore.search import SearchService
from encore.services import QueueService
from encore.services.queue_service import QueueStore
from tests.support.mpv import MockMpv


class Clock:
    """A clock the test moves, so a whole party fits into one second of virtual time."""

    def __init__(self) -> None:
        self.at = datetime(2026, 6, 1, 20, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


class RevivingLauncher:
    """An `EngineLauncher` whose mpv can be killed, and comes back as a fresh double.

    The important part is that a restart produces a *new* channel: `MpvPlayer` holds a
    provider rather than a socket, and a launcher that handed back the same dead object
    would prove the reconnection in `PlaybackSupervisor` is a lie.
    """

    def __init__(self) -> None:
        self.mpv = MockMpv()
        self.launches = 0
        self.alive = True
        self.terminations = 0

    def launch(self) -> MockMpv:
        self.launches += 1
        if self.launches > 1:
            self.mpv = MockMpv()
        self.alive = True
        return self.mpv

    def terminate(self) -> None:
        self.terminations += 1
        self.alive = False

    def diagnostics(self) -> str:
        return "mpv running" if self.alive else "mpv exited with code -11"

    def kill(self) -> None:
        """The process dies. Nobody is told; detection has to come from polling."""

        self.mpv.crash()
        self.alive = False


class Party:
    """The running appliance, minus the web.

    Attributes are the pieces a test looks at directly: both stores, the mock engine, and
    the three services whose decisions are under test.
    """

    def __init__(
        self,
        library: LibraryStore,
        runtime: RuntimeStore,
        *,
        crossfade: float = 0.0,
        max_items: int = 200,
    ) -> None:
        self.library = library
        self.runtime = runtime
        self.clock = Clock()
        self.events = EventBus(logger=logging.getLogger("encore.test.party"))
        self.seen: list[object] = []
        self.events.subscribe(Event, self.seen.append, name="recorder")
        self.launcher = RevivingLauncher()
        self.supervisor = PlaybackSupervisor(
            launcher=self.launcher,
            events=self.events,
            config=PlaybackConfig(restart_backoff_seconds=[1.0]),
            clock=self.clock,
            logger=logging.getLogger("encore.test.party.supervisor"),
        )
        self.search = SearchService(
            index=library.search,
            catalogue=library,
            page_size=SearchConfig().page_size,
        )
        audio = AudioConfig(crossfade_seconds=crossfade)
        self.playback = PlaybackService(
            player=MpvPlayer(self.supervisor.channel, volume=float(audio.volume)),
            events=self.events,
            clock=self.clock,
            recovery=self.supervisor,
            policy=TransitionPolicy.from_config(audio),
        )
        # The mutual reference the composition root makes (ADR-011): the service restarts
        # through the supervisor, and the supervisor reports the process to the service.
        self.supervisor.bind(self.playback)
        self.queue = QueueService(
            store=runtime,
            library=library,
            player=self.playback,
            events=self.events,
            config=QueueConfig(max_items=max_items),
            clock=self.clock,
            logger=logging.getLogger("encore.test.party.queue"),
        )
        self.queue.start()

    @property
    def mpv(self) -> MockMpv:
        """The engine as it exists right now, which a restart may have replaced."""

        return self.launcher.mpv

    def tick(self, seconds: float = 1.0) -> None:
        """One second of a party: the monitor looks at the process, the service at progress."""

        self.clock.advance(seconds)
        self.supervisor.tick()
        self.playback.tick()

    def play_until_silent(self, limit: int = 40) -> int:
        """Run the clock until the queue is empty and nothing is playing.

        Each tick reports end-of-file, because "the track finished" is the fact the queue
        advances on and a test that ended only because the queue ran out would pass with a
        stuck player.
        """

        for tick in range(limit):
            if self.playback.state is PlaybackState.IDLE and self.queue.length == 0:
                return tick
            if self.playback.state is PlaybackState.IDLE:
                assert self.queue.play_next(), "an idle engine and a queue with stock in it"
            self.mpv.properties["eof-reached"] = True
            self.tick()
        raise AssertionError(f"the party did not settle within {limit} ticks")

    def songs(self, text: str = "track") -> list[Song]:
        """Search the way a guest would, returning the things they can queue."""

        return [hit.song for hit in self.search.songs(text)]

    def close(self) -> None:
        self.queue.close()
        self.supervisor.stop()

    def loaded(self) -> list[str]:
        """Every file handed to the engine, in order, across restarts."""

        return [str(command.args[0]) for command in self.mpv.commands if command.name == "loadfile"]


@pytest.fixture
def party(library_store: LibraryStore, runtime_store: RuntimeStore) -> Iterator[Party]:
    """A running appliance over both real databases, closed after the test."""

    made = Party(library_store, runtime_store)
    try:
        yield made
    finally:
        made.close()


# -- the shape of the system ---------------------------------------------


def test_the_real_store_satisfies_what_the_queue_declares(runtime_store: RuntimeStore) -> None:
    """The annotation is the test, and mypy reads it on every commit.

    Structural typing is only honest when it is checked against the real object: a protocol
    nobody assigns can drift, and `QueueService` would keep working in tests until a guest
    found the difference.
    """

    store: QueueStore = runtime_store

    with store.unit_of_work() as work:
        assert work.queue.active() == []


def test_a_search_result_can_be_queued_and_reaches_the_engine(party: Party) -> None:
    """SAPRS 9.6's result row and SAPRS 7.4's `loadfile`, in one line of causation."""

    song = party.songs("first track")[0]

    party.queue.enqueue(song.id)

    assert song.file_path is not None
    assert party.loaded() == [str(song.file_path)]
    assert party.playback.state is PlaybackState.PLAYING


def test_a_queued_song_says_so_on_the_bus(party: Party) -> None:
    song = party.songs()[0]

    party.queue.enqueue(song.id)

    started = [event for event in party.seen if isinstance(event, SongStarted)]
    assert len(started) == 1
    assert started[0].song_id == song.id
    assert party.queue.length == 1


# -- a whole set, in order (SAPRS 8.1, 8.7) ------------------------------


def test_the_first_seven_requests_play_in_the_order_they_arrived(party: Party) -> None:
    songs = party.songs()[:7]
    for song in songs:
        party.queue.enqueue(song.id)
        party.queue.skip()

    assert party.loaded() == [str(song.file_path) for song in songs]


def test_the_whole_queue_runs_to_empty_and_writes_one_history_row_each(party: Party) -> None:
    songs = party.songs()[:4]
    for song in songs:
        party.queue.enqueue(song.id)

    party.play_until_silent()

    assert party.queue.length == 0
    assert party.playback.state is PlaybackState.IDLE
    with party.runtime.unit_of_work() as work:
        rows = work.history.recent(limit=20)
        assert len(rows) == 4
        assert {row.outcome for row in rows} == {PlaybackOutcome.COMPLETED}
        assert all(row.completion > 0.9 for row in rows)
        assert {row.song_id for row in rows} == {song.id for song in songs}


def test_the_same_song_queued_twice_by_two_guests_plays_twice(party: Party) -> None:
    """SAPRS 8.3, with a search box in the way: nothing deduplicates on the way in."""

    song = party.songs()[0]

    party.queue.enqueue(song.id)
    party.queue.enqueue(song.id)

    assert party.queue.length == 2
    assert [item.status for item in party.queue.active()] == [
        QueueItemStatus.PLAYING,
        QueueItemStatus.PENDING,
    ]


# -- administrative control (SAPRS 8.8) ----------------------------------


def test_a_skip_gives_the_next_request_the_speakers(party: Party) -> None:
    songs = party.songs()[:3]
    for song in songs:
        party.queue.enqueue(song.id)

    party.queue.skip()
    party.play_until_silent()

    assert party.loaded() == [str(song.file_path) for song in songs]
    with party.runtime.unit_of_work() as work:
        outcomes = {row.outcome for row in work.history.recent(limit=10)}
    assert outcomes == {PlaybackOutcome.COMPLETED, PlaybackOutcome.SKIPPED}


def test_stopping_holds_the_room_and_play_starts_the_head_again(party: Party) -> None:
    songs = party.songs()[:2]
    party.queue.enqueue(songs[0].id)
    party.queue.enqueue(songs[1].id)

    party.queue.stop()

    assert party.playback.state is PlaybackState.IDLE
    assert [item.song_id for item in party.queue.active()] == [songs[0].id, songs[1].id]

    assert party.queue.play_next() is True

    assert party.playback.current is not None
    assert party.playback.current.song_id == songs[0].id


def test_clearing_takes_the_waiting_requests_and_leaves_the_music_playing(party: Party) -> None:
    songs = party.songs()
    for song in songs[:3]:
        party.queue.enqueue(song.id)

    removed = party.queue.clear()

    assert removed == 2
    assert party.queue.length == 1
    assert party.playback.state is PlaybackState.PLAYING


# -- when things go wrong (SAPRS 7.6, 14.10) -----------------------------


def test_a_crash_mid_set_is_reported_once_and_the_queue_continues_after_the_restart(
    party: Party,
) -> None:
    """The path the Administrator Guide's "it stopped playing" section is about.

    Nobody tells Encore that mpv died. The monitor notices a dead process, the service
    publishes the track's failure, the supervisor brings mpv back, and the queue starts the
    next request — four components, one event each, and no polling loop.
    """

    songs = party.songs()[:3]
    for song in songs:
        party.queue.enqueue(song.id)
    first_engine = party.mpv
    party.seen.clear()

    party.launcher.kill()
    party.tick()

    finished = [event for event in party.seen if isinstance(event, SongFinished)]
    assert len(finished) == 1, "one death, one fact"
    assert party.launcher.launches == 2, "the supervisor brought mpv back"
    assert party.mpv is not first_engine, "the new commands go to the new process"

    party.play_until_silent()

    assert party.queue.length == 0
    with party.runtime.unit_of_work() as work:
        outcomes = [row.outcome for row in work.history.recent(limit=10)]
    assert outcomes.count(PlaybackOutcome.FAILED) == 1
    assert outcomes.count(PlaybackOutcome.COMPLETED) == 2


def test_a_file_the_engine_cannot_open_ends_the_track_as_failed_and_moves_on(
    party: Party,
) -> None:
    """The difference between this and a crash: mpv answered, and the answer was no.

    SAPRS 11.9 wants the failure visible rather than silent, and SAPRS 8.7 wants the queue
    to carry on. A file that vanished between the build and the party is the most common way
    a jukebox meets this code path.
    """

    songs = party.songs()[:2]
    original = party.mpv.send_command

    def refusing(name: str, *args: object) -> object:
        if name == "loadfile":
            raise MpvCommandError("loadfile", "No such file or directory")
        return original(name, *args)

    party.mpv.send_command = refusing  # type: ignore[method-assign]

    for song in songs:
        party.queue.enqueue(song.id)

    party.play_until_silent()

    assert party.queue.length == 0
    with party.runtime.unit_of_work() as work:
        rows = work.history.recent(limit=10)
    assert [row.outcome for row in rows] == [PlaybackOutcome.FAILED, PlaybackOutcome.FAILED]
    assert all(row.completion == 0.0 for row in rows)


def test_a_song_that_left_the_library_is_dropped_and_the_rest_of_the_queue_plays(
    party: Party,
) -> None:
    """`library.db` is rebuilt; `runtime.db` outlives it (ADR-009), so this is a real Tuesday.

    The queue's answer is to take the item out and keep playing. The alternative is an
    appliance that refuses to play anything because one row in twenty thousand has gone.
    """

    songs = party.songs()[:3]
    for song in songs:
        party.queue.enqueue(song.id)
    gone = party.queue.up_next()[0]
    unresolvable = SongId(int(gone.song.id if gone.song else songs[1].id) + 10_000)
    with party.runtime.unit_of_work() as work:
        work.queue.remove(gone.item.id)
        work.queue.append(unresolvable)
        work.queue.compact()

    party.play_until_silent()

    assert party.queue.length == 0, "the unresolvable request was taken out, not retried"
    with party.runtime.unit_of_work() as work:
        assert work.history.total() == 2, "a song that was never played wrote no history row"


# -- a restart (SAPRS 8.6) -----------------------------------------------


def test_the_queue_survives_a_process_restart_and_the_new_service_finishes_it(
    party: Party, library_store: LibraryStore, runtime_store: RuntimeStore
) -> None:
    songs = party.songs()[:3]
    for song in songs:
        party.queue.enqueue(song.id)
    party.queue.close()

    second = Party(library_store, runtime_store)
    try:
        assert second.queue.length == 3, "the requests were on disk, not in a process"
        assert second.playback.state is PlaybackState.IDLE
        assert second.queue.now_playing() is None
        assert [entry.song for entry in second.queue.up_next()] == songs, (
            "the song that was interrupted is first again, and nothing was reordered"
        )

        assert second.queue.play_next() is True

        second.play_until_silent()

        assert second.queue.length == 0
        assert second.loaded() == [str(song.file_path) for song in songs]
    finally:
        second.close()


# -- crossfade (SAPRS 7.8) -----------------------------------------------


def test_a_configured_fade_moves_the_volume_and_gives_it_back(
    library_store: LibraryStore, runtime_store: RuntimeStore
) -> None:
    """Not a two-deck mix: `encore/playback/transition.py` says what v1 actually does.

    The assertion is that the level comes back, which is the part a room notices — a
    jukebox that fades out and never fades in is quiet for the rest of the night.
    """

    party = Party(library_store, runtime_store, crossfade=4.0)
    try:
        song = party.songs()[0]
        party.queue.enqueue(song.id)
        party.mpv.properties.update({"time-pos": 1.0, "duration": 200.0})

        party.tick()

        assert party.mpv.send_command("get_property", "volume") < 85.0

        party.mpv.properties["time-pos"] = 100.0
        party.tick()

        assert party.mpv.send_command("get_property", "volume") == 85.0
    finally:
        party.close()


def test_the_write_path_of_this_milestone_landed_in_the_runtime_database_only(
    party: Party,
) -> None:
    """SAPRS 5.2's separation, asserted where this milestone could have broken it.

    Queueing and playing is the write path added here, and the only thing that proves the
    services obeyed "never modify `library.db`" is running them and looking at both files.
    `tests/integration/test_database_separation.py` covers the file mode; this covers the
    code that has no business reaching for it.
    """

    before = party.library.counts()
    songs = party.songs()[:2]
    for song in songs:
        party.queue.enqueue(song.id)
        party.queue.skip()

    assert party.library.counts() == before
    with party.runtime.unit_of_work() as work:
        assert work.history.total() == 2
        assert work.queue.all_items(), "the writes went here instead"
