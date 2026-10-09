"""`PlaybackService`: SAPRS 7.3's diagram and 7.4's commands, against the mock player.

Everything here runs on `MockMpv`, which is not a shortcut: it accepts the same JSON
command objects the real client serialises, so a wrong command name or property still
fails (ADR-005, `docs/Developer/Testing.md`). What this file cannot prove is that sound
comes out, and nothing in it pretends to.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from encore.domain import (
    AlbumId,
    ArtistId,
    AudioFormat,
    PlaybackProgress,
    PlaybackState,
    QueueItemId,
    Song,
    SongId,
)
from encore.events import EventBus, SongFinished, SongStarted
from encore.events.playback import FinishedReason
from encore.playback import IllegalTransitionError, MpvPlayer, PlaybackService, TransitionPolicy
from encore.playback.errors import MpvCommandError
from tests.support.mpv import MockMpv

START = datetime(2026, 6, 1, 20, 0, tzinfo=UTC)


class FixedClock:
    def __init__(self, at: datetime = START) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at


class RecordingBus(EventBus):
    """The real bus, with the facts kept.

    Subclasses rather than wraps because the service's contract is "publish on the bus",
    and a fake bus that orders delivery differently would let a wrong publish order pass.
    """

    def __init__(self) -> None:
        super().__init__(logger=logging.getLogger("encore.test.playback"))
        self.published: list[object] = []

    def publish(self, event: object) -> object:  # type: ignore[override]
        self.published.append(event)
        return super().publish(event)  # type: ignore[arg-type]

    def of(self, kind: type[object]) -> list[object]:
        return [event for event in self.published if isinstance(event, kind)]


class FakeRecovery:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def recover(self, *, reason: str) -> bool:
        self.reasons.append(reason)
        return True


@pytest.fixture
def mpv() -> MockMpv:
    return MockMpv()


@pytest.fixture
def bus() -> RecordingBus:
    return RecordingBus()


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def service(mpv: MockMpv, bus: RecordingBus, clock: FixedClock) -> PlaybackService:
    player = MpvPlayer(lambda: mpv, volume=85.0)
    return PlaybackService(player=player, events=bus, clock=clock)


def _state_of(service: PlaybackService) -> PlaybackState:
    """Read the state through a call.

    mypy narrows `service.state is X` across the following lines and cannot know that
    `resume()` moved it, so an assertion on a property is written as an assertion on a
    local. The behaviour is asserted either way.
    """

    return service.state


def _song(id_: int = 1, *, duration: int = 200) -> Song:
    return Song(
        id=SongId(id_),
        title=f"Track {id_}",
        artist_id=ArtistId(1),
        album_id=AlbumId(1),
        file_path=Path(f"/music/track-{id_}.flac"),
        file_format=AudioFormat.FLAC,
        duration=timedelta(seconds=duration),
    )


ITEM = QueueItemId(11)


# -- beginning ------------------------------------------------------------


def test_a_fresh_service_is_idle_and_would_play_at_once(service: PlaybackService) -> None:
    assert service.state is PlaybackState.IDLE
    assert service.is_idle
    assert service.current is None


def test_play_loads_the_file_and_publishes_one_start(
    service: PlaybackService, bus: RecordingBus, mpv: MockMpv
) -> None:
    assert service.play(_song(), queue_item_id=ITEM) is True

    assert service.state is PlaybackState.PLAYING
    assert mpv.take_command("loadfile").args[0] == "/music/track-1.flac"
    started = bus.of(SongStarted)
    assert len(started) == 1
    event = started[0]
    assert isinstance(event, SongStarted)
    assert event.song_id == SongId(1)
    assert event.queue_item_id == ITEM


def test_the_track_remembers_the_request_that_started_it(service: PlaybackService) -> None:
    """`queue_item_id` travels with the track, not with the song (SAPRS 4.5, 8.3).

    Two requests for one song are two items, and the one that ends must be the one that
    started — otherwise the queue marks a row nobody played.
    """

    other = QueueItemId(12)
    service.play(_song(), queue_item_id=other)

    assert service.current is not None
    assert service.current.queue_item_id == other


def test_song_started_is_not_published_before_sound(
    service: PlaybackService, bus: RecordingBus, mpv: MockMpv
) -> None:
    """A cued track sits in `Loading`: asked for, silent (SAPRS 7.3)."""

    mpv.properties["idle-active"] = True
    assert service.load(_song(), queue_item_id=ITEM) is True

    assert service.state is PlaybackState.LOADING
    assert bus.of(SongStarted) == []


# -- commands -------------------------------------------------------------


def test_pause_and_resume_move_the_state_and_no_events(
    service: PlaybackService, bus: RecordingBus, mpv: MockMpv
) -> None:
    service.play(_song(), queue_item_id=ITEM)
    bus.published.clear()

    assert service.pause() is True
    assert _state_of(service) is PlaybackState.PAUSED
    assert mpv.send_command("get_property", "pause") is True

    assert service.resume() is True
    assert _state_of(service) is PlaybackState.PLAYING
    assert bus.published == [], "a pause is a state, not a fact that ended anything"


def test_pausing_an_idle_appliance_is_a_no_op_not_an_error(
    service: PlaybackService,
) -> None:
    assert service.pause() is False
    assert service.resume() is False
    assert service.state is PlaybackState.IDLE


def test_stop_reports_the_track_stopped_and_silences_the_engine(
    service: PlaybackService, bus: RecordingBus, mpv: MockMpv
) -> None:
    service.play(_song(), queue_item_id=ITEM)

    service.stop()

    assert service.state is PlaybackState.IDLE
    assert service.current is None
    assert "stop" in mpv.command_names()
    finished = bus.of(SongFinished)
    assert len(finished) == 1
    event = finished[0]
    assert isinstance(event, SongFinished)
    assert event.reason is FinishedReason.STOPPED


def test_skip_reports_the_track_skipped(service: PlaybackService, bus: RecordingBus) -> None:
    service.play(_song(), queue_item_id=ITEM)
    service.skip()

    (event,) = bus.of(SongFinished)
    assert isinstance(event, SongFinished)
    assert event.reason is FinishedReason.SKIPPED
    assert not event.counts_as_played


def test_a_track_ends_once(service: PlaybackService, bus: RecordingBus, mpv: MockMpv) -> None:
    """The publication guard: a command racing the tick must not double-count.

    Two `SongFinished` events for one track would write two history rows, and the play
    total would drift by one per race — the kind of discrepancy that takes a whole party to
    notice and a week to find.
    """

    service.play(_song(), queue_item_id=ITEM)
    mpv.properties.update({"time-pos": 100.0, "duration": 200.0})

    service.skip()
    service.stop()
    service.tick()
    service.skip()

    assert len(bus.of(SongFinished)) == 1


def test_stopping_when_idle_publishes_nothing(service: PlaybackService, bus: RecordingBus) -> None:
    service.stop()

    assert bus.published == []


def test_seek_is_a_position_change_while_playing(service: PlaybackService, mpv: MockMpv) -> None:
    service.play(_song(), queue_item_id=ITEM)

    assert service.seek(timedelta(seconds=90)) is True
    assert mpv.send_command("get_property", "time-pos") == 90.0
    assert service.progress.position == timedelta(seconds=90)


def test_seek_is_refused_when_there_is_nothing_to_seek_in(service: PlaybackService) -> None:
    assert service.seek(timedelta(seconds=10)) is False
    assert service.state is PlaybackState.IDLE


def test_playing_over_a_playing_track_is_an_undefined_transition(
    service: PlaybackService,
) -> None:
    """SAPRS 7.3 draws no `Playing → Loading` edge, and Encore does not invent one.

    The queue is the only caller and it checks `is_idle` first; reaching this error means a
    new caller started playing over something, which would drop a track with no event.
    """

    service.play(_song(), queue_item_id=ITEM)

    with pytest.raises(IllegalTransitionError, match="does not allow"):
        service.play(_song(2), queue_item_id=QueueItemId(12))


# -- progress (SAPRS 7.5) -------------------------------------------------


def test_tick_reports_position_and_duration(service: PlaybackService, mpv: MockMpv) -> None:
    service.play(_song(), queue_item_id=ITEM)
    mpv.properties.update({"time-pos": 42.5, "duration": 200.0})

    progress = service.tick()

    assert isinstance(progress, PlaybackProgress)
    assert progress.state is PlaybackState.PLAYING
    assert progress.song_id == SongId(1)
    assert progress.position == timedelta(seconds=42.5)
    assert progress.fraction == pytest.approx(0.2125)


def test_progress_property_does_not_read_the_engine(service: PlaybackService, mpv: MockMpv) -> None:
    """/now-playing must not multiply mpv round trips by multiplying requests (SAPRS 10.10)."""

    service.play(_song(), queue_item_id=ITEM)
    before = len(mpv.commands)

    assert service.progress.state is PlaybackState.PLAYING
    assert len(mpv.commands) == before


def test_a_finished_track_is_reported_completed_once(
    service: PlaybackService, bus: RecordingBus, mpv: MockMpv
) -> None:
    """The diagram's own edge, and one publication for it.

    `Playing → Finished → Idle` is what SAPRS 7.3 draws; `Playing → Stopping → Idle` is what
    a stop is. Both end at the same state, so only the event distinguishes them, and a log
    that cannot tell them apart cannot answer "did it end or did someone stop it".
    """

    service.play(_song(), queue_item_id=ITEM)
    mpv.properties.update({"eof-reached": True})

    service.tick()
    service.tick()

    events = [event for event in bus.of(SongFinished) if isinstance(event, SongFinished)]
    (event,) = events
    assert event.reason is FinishedReason.COMPLETED
    assert event.completion == pytest.approx(1.0)
    assert service.state is PlaybackState.IDLE


def test_an_unreadable_file_is_reported_as_a_failure(
    service: PlaybackService, bus: RecordingBus, mpv: MockMpv
) -> None:
    """SAPRS 11.9: a missing media file is a reported failure, not silence.

    mpv accepts `loadfile` for a path it then cannot open, and answers the *next* read
    with an error; the double refuses the command outright, which exercises the same
    branch of the service through the earlier of the two real paths.
    """

    original = mpv.send_command

    def refuse(name: str, *args: object) -> object:
        if name == "loadfile":
            raise MpvCommandError(name, "No such file or directory")
        return original(name, *args)

    mpv.send_command = refuse  # type: ignore[method-assign]
    assert service.play(_song(), queue_item_id=ITEM) is False

    (event,) = [item for item in bus.of(SongFinished) if isinstance(item, SongFinished)]
    assert isinstance(event, SongFinished)
    assert event.reason is FinishedReason.FAILED
    assert event.completion == 0.0
    assert service.state is PlaybackState.IDLE
    assert service.current is None


def test_completion_is_capped_at_one(
    service: PlaybackService, bus: RecordingBus, mpv: MockMpv
) -> None:
    """A position past a truncated file's declared length is not "120% played"."""

    service.play(_song(), queue_item_id=ITEM)
    mpv.properties.update({"time-pos": 400.0, "duration": 200.0})
    service.tick()

    service.skip()

    events = [event for event in bus.of(SongFinished) if isinstance(event, SongFinished)]
    (event,) = events
    assert event.completion == 1.0


# -- recovery (SAPRS 7.6) -------------------------------------------------


def test_a_dead_engine_publishes_failure_then_asks_for_a_restart(
    bus: RecordingBus, mpv: MockMpv, clock: FixedClock
) -> None:
    recovery = FakeRecovery()
    service = PlaybackService(
        player=MpvPlayer(lambda: mpv), events=bus, clock=clock, recovery=recovery
    )
    service.play(_song(), queue_item_id=ITEM)
    bus.published.clear()

    mpv.crash()
    service.engine_lost("mpv exited with code -9")

    assert service.state is PlaybackState.ERROR
    assert recovery.reasons == ["mpv exited with code -9"]
    (event,) = [item for item in bus.of(SongFinished) if isinstance(item, SongFinished)]
    assert event.reason is FinishedReason.FAILED


def test_the_same_crash_is_not_reported_twice(
    bus: RecordingBus, mpv: MockMpv, clock: FixedClock
) -> None:
    """One death, one fact.

    The second request *is* forwarded to the supervisor: a service that stayed in `Error`
    while a restart attempt failed silently would never come back, and `recover()` is the
    place that decides whether another launch is due (it refuses while a backoff is
    outstanding). What must not happen twice is the publication — a queue that settled the
    same item from two events would advance twice and drop a song nobody played.
    """

    recovery = FakeRecovery()
    service = PlaybackService(
        player=MpvPlayer(lambda: mpv), events=bus, clock=clock, recovery=recovery
    )
    service.play(_song(), queue_item_id=ITEM)
    mpv.crash()

    service.engine_lost("dead")
    service.engine_lost("dead")

    assert len(bus.of(SongFinished)) == 1
    assert service.state is PlaybackState.ERROR
    assert len(recovery.reasons) == 2, "the report is once; the ask is re-issued"


def test_recovery_comes_back_through_the_error_edge(
    mpv: MockMpv, bus: RecordingBus, clock: FixedClock
) -> None:
    recovery = FakeRecovery()
    service = PlaybackService(
        player=MpvPlayer(lambda: mpv), events=bus, clock=clock, recovery=recovery
    )
    service.play(_song(), queue_item_id=ITEM)
    mpv.crash()
    service.engine_lost("dead")

    service.recovered()

    assert service.state is PlaybackState.IDLE


def test_a_tick_against_a_gone_player_notices_without_an_explicit_report(
    bus: RecordingBus, mpv: MockMpv, clock: FixedClock
) -> None:
    """The service is told by the socket, not by a poller, when a command fails mid-track."""

    recovery = FakeRecovery()
    service = PlaybackService(
        player=MpvPlayer(lambda: mpv), events=bus, clock=clock, recovery=recovery
    )
    service.play(_song(), queue_item_id=ITEM)
    bus.published.clear()
    mpv.crash()

    service.tick()

    assert recovery.reasons, "detection came from the failed property read"
    assert service.state is PlaybackState.ERROR


# -- transitions and gapless (SAPRS 7.8) ---------------------------------


def test_gapless_off_is_a_single_launch_option() -> None:
    assert TransitionPolicy(gapless=False).mpv_options() == ("--gapless-audio=no",)
    assert TransitionPolicy(gapless=True).mpv_options() == ("--gapless-audio=yes",)


def test_a_configured_fade_sets_the_volume_it_wants(
    bus: RecordingBus, mpv: MockMpv, clock: FixedClock
) -> None:
    policy = TransitionPolicy(gapless=True, crossfade=timedelta(seconds=4), volume=80.0)
    service = PlaybackService(
        player=MpvPlayer(lambda: mpv, volume=80.0), events=bus, clock=clock, policy=policy
    )

    service.play(_song(), queue_item_id=ITEM)
    mpv.properties.update({"time-pos": 1.0, "duration": 200.0})
    service.tick()
    assert mpv.send_command("get_property", "volume") == pytest.approx(20.0)

    mpv.properties["time-pos"] = 100.0
    service.tick()
    assert mpv.send_command("get_property", "volume") == 80.0

    mpv.properties["time-pos"] = 198.0
    service.tick()
    assert mpv.send_command("get_property", "volume") == pytest.approx(40.0)


def test_without_a_fade_the_service_never_touches_the_volume(
    bus: RecordingBus, mpv: MockMpv, clock: FixedClock
) -> None:
    service = PlaybackService(
        player=MpvPlayer(lambda: mpv, volume=80.0),
        events=bus,
        clock=clock,
        policy=TransitionPolicy(crossfade=timedelta(0)),
    )

    service.play(_song(), queue_item_id=ITEM)
    mpv.properties.update({"time-pos": 199.0, "duration": 200.0})
    service.tick()

    volumes = [c for c in mpv.commands if c.args and c.args[0] == "volume"]
    assert volumes == [], "a fade that is configured off must not cost two IPC commands a second"


def test_a_skip_mid_fade_leaves_the_engine_at_full_level(
    bus: RecordingBus, mpv: MockMpv, clock: FixedClock
) -> None:
    """The bug this prevents is a quiet appliance for the rest of the night."""

    policy = TransitionPolicy(crossfade=timedelta(seconds=6), volume=90.0)
    service = PlaybackService(
        player=MpvPlayer(lambda: mpv, volume=90.0), events=bus, clock=clock, policy=policy
    )
    service.play(_song(), queue_item_id=ITEM)
    mpv.properties.update({"time-pos": 1.0, "duration": 200.0})
    service.tick()
    assert mpv.send_command("get_property", "volume") < 90.0

    service.skip()

    assert mpv.send_command("get_property", "volume") == 90.0


def test_progress_survives_a_state_change_without_reading_the_engine(
    service: PlaybackService, mpv: MockMpv
) -> None:
    """`_snapshot` carries position and forward: a missed one reads as a stuck bar."""

    service.play(_song(), queue_item_id=ITEM)
    mpv.properties.update({"time-pos": 30.0, "duration": 180.0})
    service.tick()

    service.pause()
    assert service.progress.position == timedelta(seconds=30.0)
    service.resume()
    assert service.progress.state is PlaybackState.PLAYING
