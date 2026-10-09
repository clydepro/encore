"""`PlaybackSupervisor`: SAPRS 7.6's seven steps, driven by a fake launcher and a clock.

The supervisor is the part of Encore that has to be right when everything else is broken, so
these tests stage failures rather than the happy path: mpv that dies mid-track, mpv that
refuses to come back, mpv that comes back and dies again at once.

There is no thread and no `time.sleep` anywhere in this file. A restart waiting out its
backoff is a timestamp compared against the injected clock, which is what lets the ladder be
tested in milliseconds and keeps the request that noticed the death from blocking (ADR-005).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from encore.config.models import PlaybackConfig
from encore.domain import AlbumId, ArtistId, AudioFormat, PlaybackState, QueueItemId, Song, SongId
from encore.events import EventBus, SongFinished
from encore.events.playback import FinishedReason
from encore.playback import (
    EngineUnavailableError,
    MpvPlayer,
    PlaybackService,
    PlaybackSupervisor,
)
from encore.playback.errors import MpvGoneError
from tests.support.mpv import MockMpv

START = datetime(2026, 6, 1, 20, 0, tzinfo=UTC)
CONFIG = PlaybackConfig(restart_backoff_seconds=[1.0, 5.0, 30.0])


class FixedClock:
    """A clock the test moves. Backoff is arithmetic, so it should be provable as such."""

    def __init__(self) -> None:
        self.at = START

    def now(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


class FakeLauncher:
    """An `EngineLauncher` whose mpv can be killed, and whose refusals can be scripted.

    Attributes:
        failures: How many `launch` calls raise before mpv comes back — 1 is a hiccup, a
            large number is a machine with bad RAM or a missing binary.
        alive: Set false by a test to stand in for the process dying.
    """

    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.launches = 0
        self.terminations = 0
        self.alive = False
        self.channels: list[MockMpv] = []

    def launch(self) -> MockMpv:
        self.launches += 1
        if self.failures:
            self.failures -= 1
            raise MpvGoneError("mpv did not open its socket")
        self.alive = True
        channel = MockMpv()
        self.channels.append(channel)
        return channel

    def terminate(self) -> None:
        self.terminations += 1
        self.alive = False

    def diagnostics(self) -> str:
        return "mpv exited with code -11" if not self.alive else "mpv running as pid 4321"


class RecordingBus(EventBus):
    """The real bus, keeping the facts.

    Subclasses rather than wraps: the contract under test is "publish on the bus in this
    order", and a fake that reordered delivery would let a wrong sequence pass.
    """

    def __init__(self) -> None:
        super().__init__(logger=logging.getLogger("encore.test.supervisor"))
        self.published: list[object] = []

    def publish(self, event: object) -> object:  # type: ignore[override]
        self.published.append(event)
        return super().publish(event)  # type: ignore[arg-type]

    def of(self, kind: type[object]) -> list[object]:
        return [event for event in self.published if isinstance(event, kind)]


class RecordingService:
    """A `PlaybackNotifier` that remembers what it was told, and asks for the restart.

    The real service is tested in `test_playback_service.py`. What is under scrutiny here is
    the shape of the collaboration: the supervisor reports the *process*, this side publishes
    the *track*, and the restart is requested from the service side so that one death produces
    one fact (ADR-004). `recovery` is wired by `_supervisor`, which is the mutual reference
    `container.py` sets up in production.
    """

    def __init__(self) -> None:
        self.lost: list[str] = []
        self.recoveries: list[PlaybackState] = []
        self.state = PlaybackState.IDLE
        self.recovery: object | None = None

    def engine_lost(self, reason: str) -> None:
        self.lost.append(reason)
        self.state = PlaybackState.ERROR
        if self.recovery is not None:
            self.recovery.recover(reason=reason)  # type: ignore[attr-defined]

    def recovered(self, *, resumed: PlaybackState = PlaybackState.IDLE) -> None:
        self.recoveries.append(resumed)
        self.state = resumed


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def bus() -> RecordingBus:
    return RecordingBus()


@pytest.fixture
def launcher() -> FakeLauncher:
    return FakeLauncher()


def _waiting(supervisor: PlaybackSupervisor) -> bool:
    """Whether a restart is armed. A call, so mypy cannot narrow the property across lines."""

    return supervisor.report.waiting


def _supervisor(
    launcher: FakeLauncher,
    bus: RecordingBus,
    clock: FixedClock,
    *,
    service: object = None,
    config: PlaybackConfig = CONFIG,
) -> PlaybackSupervisor:
    supervisor = PlaybackSupervisor(
        launcher=launcher,
        events=bus,
        config=config,
        service=service,  # type: ignore[arg-type]
        clock=clock,
        logger=logging.getLogger("encore.test.supervisor"),
    )
    if isinstance(service, RecordingService):
        service.recovery = supervisor
    return supervisor


# -- starting -------------------------------------------------------------


def test_start_launches_once_and_reports_healthy(
    launcher: FakeLauncher, bus: RecordingBus, clock: FixedClock
) -> None:
    supervisor = _supervisor(launcher, bus, clock)

    channel = supervisor.start()

    assert channel is launcher.channels[0]
    assert launcher.launches == 1
    assert supervisor.running
    assert [type(event).__name__ for event in bus.published] == ["HealthChanged"]


def test_no_player_is_launched_before_someone_needs_one(
    launcher: FakeLauncher, bus: RecordingBus, clock: FixedClock
) -> None:
    """A server that boots to an empty queue should not be holding the audio device."""

    supervisor = _supervisor(launcher, bus, clock)

    assert not supervisor.running

    supervisor.channel()

    assert launcher.launches == 1


def test_startup_fails_loudly_when_the_engine_will_not_come_up(
    launcher: FakeLauncher, bus: RecordingBus, clock: FixedClock
) -> None:
    """SAPRS 11.7: "running and silent" is the one state an appliance must not ship in."""

    launcher.failures = 99
    supervisor = _supervisor(launcher, bus, clock)

    with pytest.raises(EngineUnavailableError, match=r"(cannot start|did not open)") as raised:
        supervisor.start()

    assert raised.value.attempts == 1
    assert supervisor.health().status.value == "unavailable"


# -- detecting (SAPRS 7.6 steps 1-2) --------------------------------------


def test_a_death_is_reported_once_and_the_engine_is_back_without_anyone_sleeping(
    launcher: FakeLauncher, bus: RecordingBus, clock: FixedClock
) -> None:
    service = RecordingService()
    supervisor = _supervisor(launcher, bus, clock, service=service)
    supervisor.start()
    bus.published.clear()
    launcher.alive = False

    assert supervisor.tick() is True

    assert service.lost == ["mpv exited with code -11"]
    assert launcher.launches == 2
    assert clock.at == START, "a backoff is a timestamp, not a blocked request"


def test_a_healthy_engine_costs_no_events(
    launcher: FakeLauncher, bus: RecordingBus, clock: FixedClock
) -> None:
    service = RecordingService()
    supervisor = _supervisor(launcher, bus, clock, service=service)
    supervisor.start()
    bus.published.clear()

    for _ in range(5):
        clock.advance(1.0)
        assert supervisor.tick() is True

    assert bus.published == []
    assert supervisor.restart_count == 0


# -- restarting (SAPRS 7.6 steps 3-5) -------------------------------------


def test_a_restart_that_cannot_launch_waits_one_rung_of_the_ladder(
    launcher: FakeLauncher, bus: RecordingBus, clock: FixedClock
) -> None:
    service = RecordingService()
    supervisor = _supervisor(launcher, bus, clock, service=service)
    supervisor.start()
    launcher.alive = False
    launcher.failures = 1

    supervisor.tick()

    assert launcher.launches == 2, "one attempt, refused"
    assert _waiting(supervisor)
    clock.advance(0.5)
    supervisor.tick()
    assert launcher.launches == 2, "half the wait is not the wait"

    bus.published.clear()
    clock.advance(0.5)

    assert supervisor.tick() is True

    assert launcher.launches == 3
    assert supervisor.restart_count == 1
    assert [type(event).__name__ for event in bus.published] == [
        "PlaybackRecovered",
        "HealthChanged",
    ], "the fact comes before the status line that summarises it"
    assert service.recoveries == [PlaybackState.IDLE]


def test_each_refused_attempt_waits_longer_than_the_last(
    launcher: FakeLauncher, bus: RecordingBus, clock: FixedClock
) -> None:
    """The ladder is the difference between a reboot and a machine that hammers a dead disk.

    A restart that *succeeds* is not on the ladder: SAPRS 7.6 asks for a restart, and an
    engine that comes back and dies again a minute later is a different incident, reported
    as one after the other by `restart_count` and `FLAPPING_RESTARTS`.
    """

    supervisor = _supervisor(launcher, bus, clock)
    supervisor.start()
    launcher.alive = False
    launcher.failures = 99

    ladder = [1.0, 5.0, 30.0]
    for rung, expected in enumerate(ladder, start=1):
        clock.advance(expected)
        supervisor.tick()
        assert launcher.launches == 1 + rung, f"rung {rung} of the ladder was skipped"


def test_a_restart_that_fails_arms_the_next_attempt_and_downgrades_health(
    launcher: FakeLauncher, bus: RecordingBus, clock: FixedClock
) -> None:
    supervisor = _supervisor(launcher, bus, clock)
    supervisor.start()
    launcher.alive = False
    launcher.failures = 99

    supervisor.tick()

    assert not supervisor.running
    assert _waiting(supervisor)
    assert supervisor.health().status.value == "degraded"

    clock.advance(1.0)
    supervisor.tick()

    assert _waiting(supervisor), "the ladder is still climbing; it did not give up"
    assert supervisor.health().status.value == "degraded"


def test_the_health_line_names_the_reason(
    launcher: FakeLauncher, bus: RecordingBus, clock: FixedClock
) -> None:
    """`/health` is read by someone standing in a hallway, in a hurry."""

    supervisor = _supervisor(launcher, bus, clock)
    supervisor.start()
    launcher.alive = False
    launcher.failures = 99

    supervisor.tick()

    assert "code -11" in supervisor.health().detail


def test_recovering_an_engine_that_is_already_up_launches_nothing(
    launcher: FakeLauncher, bus: RecordingBus, clock: FixedClock
) -> None:
    """Two callers, one mpv. The audio device cannot be shared by two.

    This is reachable because a service in `Error` asks for the restart again on each
    notice of the same death; the supervisor is where the duplicate stops.
    """

    supervisor = _supervisor(launcher, bus, clock)
    supervisor.start()

    assert supervisor.recover(reason="a second opinion") is True

    assert launcher.launches == 1


# -- the queue keeps moving (SAPRS 7.6 step 7) ---------------------------


def test_a_crash_mid_track_ends_the_track_as_a_failure_before_the_recovery_is_announced(
    launcher: FakeLauncher, bus: RecordingBus, clock: FixedClock
) -> None:
    """The supervisor and the real service, wired the way `container.py` wires them.

    The order is the point: if `PlaybackRecovered` were published first, the queue's handler
    would look at an engine that still had a track in it, find it not idle, and start
    nothing — a jukebox that survives its own crash and then plays silence.
    """

    player = MockMpv()
    launcher.channels.append(player)
    launcher.alive = True
    service = PlaybackService(player=MpvPlayer(lambda: channel_of(launcher)), events=bus)
    supervisor = _supervisor(launcher, bus, clock, service=service)
    supervisor.start()
    service.play(_song(), queue_item_id=QueueItemId(7))
    bus.published.clear()

    launcher.alive = False
    supervisor.tick()

    kinds = [type(event).__name__ for event in bus.published]
    assert kinds == ["SongFinished", "PlaybackRecovered"], (
        "a status line that did not change is not republished, and the fact must come first"
    )
    finished = bus.of(SongFinished)[0]
    assert isinstance(finished, SongFinished)
    assert finished.reason is FinishedReason.FAILED
    assert finished.queue_item_id == QueueItemId(7)
    assert service.state is PlaybackState.IDLE, "came back through the recovery edge"


def channel_of(launcher: FakeLauncher) -> MockMpv:
    """The live channel, restarting the way the service would if a read found it dead."""

    return launcher.channels[-1]


# -- giving up and stopping ----------------------------------------------


def test_a_pending_restart_is_not_forgotten_by_a_stop(
    launcher: FakeLauncher, bus: RecordingBus, clock: FixedClock
) -> None:
    """Shutdown during a backoff window must not schedule a launch after the bus is gone."""

    supervisor = _supervisor(launcher, bus, clock)
    supervisor.start()
    launcher.alive = False
    launcher.failures = 99
    supervisor.tick()
    assert _waiting(supervisor)

    supervisor.stop()

    assert not _waiting(supervisor)
    assert launcher.terminations == 1
    clock.advance(10.0)
    assert supervisor.tick() is False
    assert launcher.launches == 2, "the start, and the one attempt that was armed"


def test_stopping_twice_terminates_once(
    launcher: FakeLauncher, bus: RecordingBus, clock: FixedClock
) -> None:
    supervisor = _supervisor(launcher, bus, clock)
    supervisor.start()

    supervisor.stop()
    supervisor.stop()

    assert launcher.terminations == 1
    assert not supervisor.running


def test_asking_for_a_channel_while_a_restart_is_pending_says_so(
    launcher: FakeLauncher, bus: RecordingBus, clock: FixedClock
) -> None:
    """`channel()` cannot invent a connection, and must not quietly start a second mpv."""

    supervisor = _supervisor(launcher, bus, clock)
    supervisor.start()
    launcher.alive = False
    launcher.failures = 99
    supervisor.tick()
    launches = launcher.launches

    with pytest.raises(MpvGoneError):
        supervisor.channel()

    assert launcher.launches == launches


def _song(id_: int = 1) -> Song:
    return Song(
        id=SongId(id_),
        title=f"Track {id_}",
        artist_id=ArtistId(1),
        album_id=AlbumId(1),
        file_path=Path(f"/music/track-{id_}.flac"),
        file_format=AudioFormat.FLAC,
        duration=timedelta(seconds=200),
    )
