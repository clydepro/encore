"""Regression: #23 — search, playback and the queue.

Eight defects found while building AIG steps 7-9. Six of them were silent: the feature
worked, the tests written from the specification passed, and the failure only appeared in a
situation no test had described yet — a stopped song, a file that could not be opened, an
mpv that died between two requests.

| Defect | Symptom |
| ------ | ------- |
| `PlaybackOutcome.counts_as_played` was `is not FAILED` | every skipped track inflated "songs played" |
| A queue item was marked `Playing` only after a successful load | an unreadable file stayed first forever |
| The advance ran after a stop | the stop button played the same song again |
| `_play_head` did not check the engine | a track was handed to an engine in `Error`, raising mid-event |
| The supervisor restarted mpv without telling the service | a crash left a `Playing` row and silence |
| `Supervisor.health()` trusted the channel object | `/health` said "healthy" over a dead mpv |
| `MpvProcess.stop()` waited twice, unguarded | shutdown raised after a SIGKILL that was slow to reap |
| An unparseable IPC line reached the caller as `JSONDecodeError` | a progress poll died with a traceback, not a named failure |

The first was found because `tests/unit/test_events.py` already asserted the opposite of
`encore/domain/playback.py`'s own docstring, and both were passing: two definitions of "did
this count as a play", agreeing with nothing. ADR-004's rule that one fact has one publisher
is what the rest of the list keeps.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from encore.config.models import PlaybackConfig, QueueConfig
from encore.domain import (
    AlbumId,
    ArtistId,
    AudioFormat,
    PlaybackOutcome,
    PlaybackState,
    QueueItemId,
    QueueItemStatus,
    Song,
    SongId,
)
from encore.events import EventBus, PlaybackRecovered, SongFinished
from encore.events.playback import FinishedReason
from encore.playback import MpvPlayer, PlaybackService, PlaybackSupervisor
from encore.playback.errors import MpvCommandError, MpvGoneError
from encore.playback.ipc import JsonIpc
from encore.repositories.runtime import RuntimeStore
from encore.services.queue_service import QueueService
from tests.support.mpv import MockMpv

START = datetime(2026, 6, 1, 20, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.at = START

    def now(self) -> datetime:
        return self.at


def _loaded(mpv: MockMpv) -> list[str]:
    """Every file the engine was asked to open, in order."""

    return [str(command.args[0]) for command in mpv.commands if command.name == "loadfile"]


def _state_of(service: PlaybackService) -> PlaybackState:
    """A call, so the type checker cannot narrow a property across the line that changed it."""

    return service.state


def _song(id_: int) -> Song:
    return Song(
        id=SongId(id_),
        title=f"Track {id_}",
        artist_id=ArtistId(1),
        album_id=AlbumId(1),
        file_path=Path(f"/music/track-{id_}.mp3"),
        file_format=AudioFormat.MP3,
        duration=timedelta(seconds=180),
    )


# -- "songs played" meant "songs that stopped" ----------------------------


def test_a_skipped_track_is_not_a_play_in_either_place_it_is_decided() -> None:
    """The defect and the guard together, because two definitions drifted once.

    `PlaybackOutcome.counts_as_played` said `is not FAILED`, which counts a skip, a stop and
    a failed load as a play; its own docstring said a skip is not a play, and
    `tests/unit/test_events.py` asserted the docstring. Both were green. The reason is that
    nothing read the enum's property, so the disagreement was only ever visible in the
    dashboard number nobody checks until the host asks.
    """

    assert not PlaybackOutcome.SKIPPED.counts_as_played
    assert not PlaybackOutcome.STOPPED.counts_as_played
    assert not PlaybackOutcome.FAILED.counts_as_played
    assert PlaybackOutcome.COMPLETED.counts_as_played

    reasons = [FinishedReason.SKIPPED, FinishedReason.STOPPED, FinishedReason.FAILED]
    assert [reason.outcome.counts_as_played for reason in reasons] == [False, False, False], (
        "the event's answer and the domain's answer must be the same answer"
    )


def test_a_set_of_skips_and_completions_counts_only_the_completions(
    runtime_store: object,
) -> None:
    """The number a host reads off the dashboard, from the rows that make it up.

    The store is the real one, because the bug survived every fake: a statistics service
    counting `outcome is not FAILED` is written the same way against a row or a dict.
    """

    store: RuntimeStore = runtime_store  # type: ignore[assignment]
    songs = [_song(number) for number in range(1, 6)]
    outcomes = [
        PlaybackOutcome.COMPLETED,
        PlaybackOutcome.SKIPPED,
        PlaybackOutcome.COMPLETED,
        PlaybackOutcome.STOPPED,
        PlaybackOutcome.FAILED,
    ]
    with store.unit_of_work() as work:
        for song, outcome in zip(songs, outcomes, strict=True):
            work.history.record(
                song_id=song.id,
                started_at=START,
                finished_at=START + timedelta(seconds=30),
                completion=1.0 if outcome is PlaybackOutcome.COMPLETED else 0.4,
                outcome=outcome,
            )

    with store.unit_of_work() as work:
        rows = work.history.recent(limit=10)
        counted = [row for row in rows if row.outcome is not None and row.outcome.counts_as_played]

    assert len(counted) == 2, "three of the five endings were not plays"


# -- a file nobody can open stops the whole queue -------------------------


class _Catalogue:
    def __init__(self, songs: list[Song]) -> None:
        self.songs = {song.id: song for song in songs}

    def song(self, song_id: SongId) -> Song | None:
        return self.songs.get(song_id)

    def songs_by_ids(self, ids: Iterable[SongId]) -> list[Song]:
        return [self.songs[song_id] for song_id in ids if song_id in self.songs]


class _Engine:
    """A player whose files all fail to open, in the way mpv fails: by answering the load.

    The first version of the advance marked an item `Playing` only after `play()` returned
    successfully, so the item that could never play stayed `Pending` at the head, and the
    loop that walks past unusable files walked onto the same one 1000 times.
    """

    def __init__(self, events: EventBus) -> None:
        self._events = events
        self.attempts = 0
        self.current_track: tuple[SongId, QueueItemId] | None = None

    @property
    def is_idle(self) -> bool:
        return True

    @property
    def state(self) -> PlaybackState:
        return PlaybackState.IDLE

    @property
    def current(self) -> object:
        return None

    @property
    def progress(self) -> object:
        return None

    def play(self, song: Song, *, queue_item_id: QueueItemId) -> bool:
        self.attempts += 1
        self._events.publish(
            SongFinished(
                song_id=song.id,
                queue_item_id=queue_item_id,
                reason=FinishedReason.FAILED,
                occurred_at=START,
            )
        )
        return False

    def pause(self) -> bool:
        return False

    def resume(self) -> bool:
        return False

    def stop(self) -> None:
        return None

    def skip(self) -> None:
        return None


def test_twenty_files_that_cannot_be_opened_empty_the_queue_instead_of_stalling_it(
    runtime_store: RuntimeStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The advance walks past an unusable item, once each.

    The first version marked a queue item `Playing` only after the engine had accepted it, so
    an item whose file could not be opened stayed `Pending` at the head — and the loop that
    walks past unusable files walked onto the same one a thousand times. The symptom at a
    party was a jukebox that went quiet with a full queue on the screen.
    """

    songs = [_song(number) for number in range(200, 220)]
    events = EventBus(logger=logging.getLogger("encore.test.regression"))
    mpv = MockMpv()
    original = mpv.send_command
    offered: list[str] = []

    def unreadable(name: str, *args: object) -> object:
        if name == "loadfile":
            offered.append(str(args[0]))
            raise MpvCommandError("loadfile", "No such file or directory")
        return original(name, *args)

    mpv.send_command = unreadable  # type: ignore[method-assign]
    playback = PlaybackService(player=MpvPlayer(lambda: mpv), events=events, clock=Clock())
    queue = QueueService(
        store=runtime_store,
        library=_Catalogue(songs),
        player=playback,
        events=events,
        config=QueueConfig(),
        clock=Clock(),
        logger=logging.getLogger("encore.test.regression.queue"),
    )
    queue.start()
    for song in songs:
        queue.enqueue(song.id)
    caplog.clear()

    assert queue.length == 0
    assert offered == [str(song.file_path) for song in songs], (
        "each file was offered exactly once, in the order they arrived"
    )
    assert "queue advance stopped walking" not in caplog.text


# -- the stop button that played the song again ---------------------------


def test_a_stop_leaves_the_song_at_the_head_and_starts_nothing(
    runtime_store: object,
) -> None:
    """A stop is a hold, not an ending.

    `SongFinished` is published for a stop, because the track did end and the queue has to
    settle its row — but advancing on that event made `_play_head` pick the item the stop had
    just returned to the head, and play it. Pressing stop started the song again.
    """

    songs = [_song(number) for number in range(1, 4)]
    store: RuntimeStore = runtime_store  # type: ignore[assignment]
    events = EventBus(logger=logging.getLogger("encore.test.regression"))
    mpv = MockMpv()
    player = MpvPlayer(lambda: mpv)
    playback = PlaybackService(player=player, events=events, clock=Clock())
    queue = QueueService(
        store=store,
        library=_Catalogue(songs),
        player=playback,
        events=events,
        config=QueueConfig(),
        clock=Clock(),
    )
    queue.start()
    for song in songs:
        queue.enqueue(song.id)
    before = len(_loaded(mpv))

    queue.stop()

    assert len(_loaded(mpv)) == before, "stop issued no new loadfile"
    assert playback.state is PlaybackState.IDLE
    assert [item.song_id for item in queue.active()] == [song.id for song in songs]
    assert [item.status for item in queue.active()] == [QueueItemStatus.PENDING] * 3


# -- a track handed to an engine that cannot take one ---------------------


def test_the_advance_does_not_offer_a_track_to_an_engine_in_error(
    runtime_store: object,
) -> None:
    """`Error` is neither idle nor playing, and `Loading` is not reachable from it.

    The advance used to call `player.play()` whenever the queue had stock. On an engine that
    had just crashed, that raised `IllegalTransitionError` from inside a `SongFinished`
    handler — the recovery came back with the queue still stopped on a `Playing` row, and the
    only way past it was a restart of the server.
    """

    songs = [_song(number) for number in range(1, 3)]
    store: RuntimeStore = runtime_store  # type: ignore[assignment]
    events = EventBus(logger=logging.getLogger("encore.test.regression"))
    engines = [MockMpv()]
    playback = PlaybackService(
        player=MpvPlayer(lambda: engines[0]),
        events=events,
        clock=Clock(),
    )
    queue = QueueService(
        store=store,
        library=_Catalogue(songs),
        player=playback,
        events=events,
        config=QueueConfig(),
        clock=Clock(),
    )
    queue.start()
    queue.enqueue(songs[0].id)
    queue.enqueue(songs[1].id)

    engines[0].crash()
    playback.tick()

    assert _state_of(playback) is PlaybackState.ERROR, "the poll noticed, and did not raise"
    assert queue.length == 1, "the dead track left the queue; the request behind it did not"
    assert [item.song_id for item in queue.active()] == [songs[1].id]

    engines[0] = MockMpv()  # the restart the supervisor would have done
    playback.recovered()

    assert queue.play_next() is True
    assert _state_of(playback) is PlaybackState.PLAYING, (
        "and the head of the queue is what came back"
    )
    assert engines[0].take_command("loadfile").args[0] == str(songs[1].file_path)


# -- the crash that fixed itself quietly --------------------------------


def test_a_death_detected_by_the_monitor_ends_the_track_before_it_announces_recovery(
    bus_and_supervisor: tuple[RecordingBus, PlaybackSupervisor, RecordingLauncher],
) -> None:
    """One event each, in the order the queue needs.

    The supervisor used to restart mpv and publish `PlaybackRecovered` without telling the
    service the engine had died. The queue's recovery handler starts the head only when the
    player is idle; finding a track still `Playing` in a state the service had never left, it
    started nothing. A jukebox that survives its own crash and then plays silence.
    """

    events, supervisor, launcher = bus_and_supervisor
    playback = PlaybackService(
        player=MpvPlayer(lambda: launcher.mpv),
        events=events,
        clock=Clock(),
    )
    supervisor.bind(playback)
    supervisor.start()
    playback.play(_song(1), queue_item_id=QueueItemId(1))
    events.seen.clear()

    launcher.alive = False
    supervisor.tick()

    assert [type(event).__name__ for event in events.seen] == [
        "SongFinished",
        "PlaybackRecovered",
    ]
    assert playback.state is PlaybackState.IDLE


class RecordingLauncher:
    """An mpv that can be killed, and that can be made unwilling to come back."""

    def __init__(self) -> None:
        self.mpv = MockMpv()
        self.alive = True
        self.launches = 0
        self.failures = 0

    def launch(self) -> MockMpv:
        self.launches += 1
        if self.failures:
            self.failures -= 1
            raise MpvGoneError("mpv did not open its socket")
        if self.launches > 1:
            self.mpv = MockMpv()
        self.alive = True
        return self.mpv

    def terminate(self) -> None:
        self.alive = False

    def diagnostics(self) -> str:
        return "mpv exited with code -11"


class RecordingBus(EventBus):
    def __init__(self) -> None:
        super().__init__(logger=logging.getLogger("encore.test.regression"))
        self.seen: list[object] = []
        self.subscribe(SongFinished, self.seen.append, name="tap")
        self.subscribe(PlaybackRecovered, self.seen.append, name="tap")

    def publish(self, event: object) -> object:  # type: ignore[override]
        return super().publish(event)  # type: ignore[arg-type]


@pytest.fixture
def bus_and_supervisor() -> tuple[RecordingBus, PlaybackSupervisor, RecordingLauncher]:
    launcher = RecordingLauncher()
    events = RecordingBus()
    supervisor = PlaybackSupervisor(
        launcher=launcher,
        events=events,
        config=PlaybackConfig(restart_backoff_seconds=[1.0]),
        clock=Clock(),
    )
    return events, supervisor, launcher


# -- the dashboard that lied --------------------------------------------


def test_health_does_not_call_a_dead_engine_healthy(
    bus_and_supervisor: tuple[RecordingBus, PlaybackSupervisor, RecordingLauncher],
) -> None:
    """`_channel` is an object; `alive` is a fact. Only one of them can be stale.

    The report used `self._channel is not None`, so a supervisor holding a socket to a
    process that had died said "healthy" — and said it in the one situation someone is
    reading the dashboard.
    """

    _events, supervisor, launcher = bus_and_supervisor
    supervisor.start()

    launcher.alive = False
    launcher.failures = 99  # every further launch fails, so the box stays down

    assert supervisor.health().status.value != "healthy", "the process is gone; that is the answer"

    supervisor.tick()

    assert not supervisor.running
    assert supervisor.health().status.value != "healthy", "and a failed restart did not change that"
    assert "code -11" in supervisor.health().detail or "not running" in supervisor.health().detail


# -- shutdown that raised ------------------------------------------------


def test_shutdown_does_not_raise_when_even_the_kill_is_slow(tmp_path: Path) -> None:
    """A process stuck in uninterruptible I/O is rare, and hanging the shutdown is worse.

    `stop()` waited once, killed, and waited again — the second wait unguarded, so the
    TimeoutExpired surfaced from the signal handler on the way out of the appliance.
    """

    from encore.playback.ipc import MpvProcess

    class StillThere:
        pid = 4242

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            """SIGTERM was delivered. The process is having trouble noticing."""

        def kill(self) -> None:
            """SIGKILL was delivered, with the same result."""

        def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
            raise subprocess.TimeoutExpired(["mpv"], 2.0)

    process = MpvProcess(
        ["mpv"], socket_path=tmp_path / "wedged.sock", spawn=lambda _: StillThere()
    )
    process._process = StillThere()

    process.stop()


# -- an IPC reply that was not JSON --------------------------------------


def test_a_malformed_ipc_reply_is_a_named_failure_not_a_traceback(tmp_path: Path) -> None:
    """A half line off a dying mpv used to raise `JSONDecodeError` from a progress poll.

    The property read that was answering would be `MpvGoneError` territory — the supervisor
    treats the two differently, and the wrong class here means a hang is diagnosed as a crash.
    """

    class Garbage:
        def sendall(self, _: bytes) -> None:
            """The command went out. What came back is the problem."""

        def recv(self, bufsize: int) -> bytes:  # noqa: ARG002
            return b"Mpv idle - not a json object\n"

        def settimeout(self, value: float | None) -> None:
            """A timeout is set and ignored, which is what a confused mpv does."""

        def shutdown(self, how: int) -> None:
            """Nothing to shut down."""

        def close(self) -> None:
            """Nothing to close."""

    ipc = JsonIpc(tmp_path / "garbage.sock", connect=lambda _: Garbage())

    with pytest.raises(MpvGoneError, match="not a JSON object"):
        ipc.send_command("get_property", "time-pos")


def test_json_encoding_of_a_command_never_carries_a_stray_newline() -> None:
    """A path with a newline in it would let one command look like two (SAPRS 14.10)."""

    line = json.dumps({"command": ["loadfile", "/music/a\nb.mp3"], "request_id": 1})

    assert "\n" not in line
