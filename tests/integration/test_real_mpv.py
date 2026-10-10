"""Encore against a real mpv (SAPRS Chapter 7, ADR-005, 14.4).

Every other playback test in the suite drives `MockMpv`, and that is the right trade for a
suite that must run on a machine with no player installed (SAPRS 14.4). The trade has a price,
and this file is where it is paid: a mock answers `loadfile` by setting the properties the
next read will return, while a real mpv answers by *promising to load the file* and getting
around to it some milliseconds later. Three things this suite found that no mock could show:

* `--input-ipc-run` does not exist before mpv 0.36, and mpv treats an unknown option as fatal.
  Launching the engine as written here produced "Error parsing option input-ipc-run (option
  not found)", exit code 1, and no socket — on mpv 0.35.1, which is what this machine has and
  what the installer will have to decide a floor about (SAPRS 7.7).
* For ~150 ms after a successful `loadfile`, `idle-active` is still `True`. A state machine
  that reads that as "nothing playing" publishes `SongFinished(COMPLETED)` in answer to a
  guest's request, and the queue walks itself to empty against a silent appliance.
* A file mpv refuses outright never becomes idle-with-a-track; it has to time out, which is
  what `LOAD_GRACE_SECONDS` exists for.

What is deliberately *not* asserted: that sound came out of a speaker. `--ao=null` is used so
these run on a headless machine and on CI's containers, which have no audio device — the claim
under test is the IPC contract and the state machine, not the amplifier.

The module skips when there is no mpv. It is marked `slow` even on machines that have one, so
the per-commit gate stays a few seconds rather than a few dozen, and it waits on real time
rather than an injected clock: a test that faked the clock would prove the state machine's
arithmetic and nothing about mpv, which is the entire point of the file.
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from encore.config.models import AudioConfig, PlaybackConfig
from encore.domain import (
    AlbumId,
    ArtistId,
    AudioFormat,
    HealthStatus,
    PlaybackState,
    QueueItemId,
    Song,
    SongId,
)
from encore.events import EventBus, PlaybackRecovered, SongFinished, SongStarted
from encore.playback import MpvPlayer, PlaybackService, PlaybackSupervisor
from encore.playback.ipc import MpvLauncher, socket_path_for
from encore.playback.service import LOAD_GRACE_SECONDS
from tests.support.media import generate_track, silent_track

pytestmark = [pytest.mark.integration, pytest.mark.slow]

MPV = shutil.which("mpv")

#: How long to wait for something mpv does asynchronously. Generous, because the assertion is
#: "it happens", and a failure that is really a slow laptop is worse than a slow test.
SETTLE_SECONDS = 6.0


def _skip_reason() -> str:
    return "no mpv binary on this machine (SAPRS 14.4: playback tests may not require one)"


class RealLauncher:
    """`EngineLauncher` over the production launcher.

    `terminate` rather than `kill` for the crash cases: these tests want mpv to die the way an
    appliance's dies, and `kill` on the process object would be a different code path.
    """

    def __init__(self, temp_dir: Path) -> None:
        self._mpv = MpvLauncher(
            playback=PlaybackConfig(mpv_path=Path(MPV or "/usr/bin/mpv")),
            audio=AudioConfig(output="null", device="none", volume=70),
            temp_dir=temp_dir,
        )
        self.launches = 0

    def launch(self) -> Any:
        self.launches += 1
        return self._mpv.launch()

    def terminate(self) -> None:
        self._mpv.terminate()

    @property
    def alive(self) -> bool:
        return self._mpv.alive

    def diagnostics(self) -> str:
        return self._mpv.diagnostics()

    def kill(self) -> None:
        """SIGKILL the engine, which is SAPRS 14.13's "mpv termination" in one call."""

        process = getattr(self._mpv, "_process", None)
        child = getattr(process, "_process", None)
        assert child is not None, "nothing to kill: mpv was never launched"
        child.kill()
        child.wait(timeout=5)


class Recorder:
    """The facts, in the order they arrived."""

    def __init__(self) -> None:
        self.started: list[tuple[int, int]] = []
        self.finished: list[tuple[int, int, str]] = []

    def attach(self, events: EventBus) -> None:
        def on_started(event: SongStarted) -> None:
            self.started.append((int(event.song_id), int(event.queue_item_id)))

        def on_finished(event: SongFinished) -> None:
            self.finished.append((int(event.song_id), int(event.queue_item_id), event.reason.value))

        events.subscribe(SongStarted, on_started, name="real-mpv-recorder")
        events.subscribe(SongFinished, on_finished, name="real-mpv-recorder")


def _state_of(playback: PlaybackService) -> PlaybackState:
    """Read the state without letting mypy narrow a property across a mutation.

    Same trick as `tests/unit/test_playback_service.py`: an `assert x.state is PAUSED` tells
    the checker the property *is* PAUSED forever, so the next `is PLAYING` assertion becomes
    "non-overlapping" and the test's own logic goes with it. A function call is opaque.
    """

    return playback.state


def _wait_for(predicate: Callable[[], bool], *, timeout: float = SETTLE_SECONDS) -> bool:
    """Poll `predicate` until it holds. Returns whether it ever did.

    Plain polling, for the cases that need no `tick()` — a process dying, a socket appearing.
    Anything about playback state wants `Appliance.wait` instead, because the engine is only
    read when the timer fires.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _song(temp_dir: Path, number: int, *, seconds: float = 1.0, broken: bool = False) -> Song:
    """One real file: a silent MP3, or bytes that only claim to be one.

    The MP3 matters more than it looks. `tests/support/media.py`'s containers were written for
    metadata, and the repository's own documentation said they are "not enough for decoding".
    Against mpv 0.35 that turns out to be pessimistic for MP3 — it decodes and reports a
    length — and true for FLAC, whose frame-less container mpv refuses. So this file uses MP3,
    and `test_the_synthetic_corpus_is_what_we_claim_it_is` keeps that honest either way.
    """

    suffix = "mp3"
    path = temp_dir / f"track-{number}.{suffix}"
    if broken:
        path.write_bytes(b"NOT AN MP3 FILE" * 64)
    elif not path.exists():
        generate_track(
            silent_track(title=f"Track {number}", duration_seconds=seconds, fmt=suffix), path
        )
    return Song(
        id=SongId(number),
        title=f"Track {number}",
        artist_id=ArtistId(1),
        album_id=AlbumId(1),
        file_path=path,
        file_format=AudioFormat.MP3,
        duration=timedelta(seconds=seconds),
    )


class Appliance:
    """Player, service and monitor wired the way `apps/server` will wire them."""

    def __init__(self, temp_dir: Path) -> None:
        self.events = EventBus(logger=logging.getLogger("encore.real-mpv"))
        self.launcher = RealLauncher(temp_dir)
        self.supervisor = PlaybackSupervisor(
            launcher=self.launcher,
            events=self.events,
            # Zero would be a truer party, but this suite waits rather than sleeps through a
            # backoff ladder, and the ladder is asserted where a clock is injected.
            config=PlaybackConfig(restart_backoff_seconds=[0.1]),
            logger=logging.getLogger("encore.real-mpv.supervisor"),
        )
        self.player = MpvPlayer(self.supervisor.channel, volume=70.0)
        self.playback = PlaybackService(
            player=self.player,
            events=self.events,
            recovery=self.supervisor,
            logger=logging.getLogger("encore.real-mpv.service"),
        )
        self.supervisor.bind(self.playback)
        self.recorder = Recorder()
        self.recorder.attach(self.events)

    def tick(self) -> None:
        """One monitor pass plus one engine reading, which is what a 1 Hz timer does."""

        self.supervisor.tick()
        self.playback.tick()

    def wait(self, predicate: Callable[[], bool], *, timeout: float = SETTLE_SECONDS) -> bool:
        """Tick while waiting for `predicate`, and say whether it ever held.

        Ticking is the point of this rather than `_wait_for`: playback state is a reading of
        mpv, and a test that polls `service.state` without asking the engine never advances its
        own machine. That is also why the appliance's UI updates once per second and not
        continuously — SAPRS 7.5's "roughly once per second" is this method's interval.
        """

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
            self.tick()
        return predicate()

    def play_until_finished(self, song: Song, *, item: int = 1) -> None:
        """Play a real track to its real end, ticking through both edges."""

        assert self.playback.play(song, queue_item_id=QueueItemId(item)) is True
        assert self.wait(lambda: _state_of(self.playback) is not PlaybackState.LOADING), (
            f"mpv never left Loading; diagnostics: {self.launcher.diagnostics()}"
        )
        while _state_of(self.playback) is PlaybackState.PLAYING:
            time.sleep(0.1)
            self.tick()
        self.tick()

    def close(self) -> None:
        # `stop` rather than the launcher's `terminate`: the supervisor owns the channel it
        # opened, and closing it is how this file proves no socket is left behind.
        self.supervisor.stop()


@pytest.fixture
def appliance(tmp_path: Path) -> Iterator[Appliance]:
    made = Appliance(tmp_path)
    try:
        yield made
    finally:
        made.close()


# -- the engine we build is the engine mpv accepts -------------------------


@pytest.mark.skipif(MPV is None, reason=_skip_reason())
def test_the_command_line_we_build_starts_a_real_mpv(tmp_path: Path) -> None:
    """The claim SAPRS 7.7 makes in YAML, checked against the binary it names.

    Mocks cannot fail this, which is why it is here: every option in `argv()` is a string a
    fake never parses. An unsupported option is fatal to mpv at parse time — before the socket
    exists — so a launch that looks configurable and is actually wrong reads to an operator as
    "the appliance will not start".
    """

    temp_dir = tmp_path / "run"
    launcher = MpvLauncher(
        playback=PlaybackConfig(mpv_path=Path(MPV or "/usr/bin/mpv")),
        audio=AudioConfig(output="null", device="none", volume=75),
        temp_dir=temp_dir,
    )
    channel = launcher.launch()
    try:
        assert channel.send_command("get_property", "idle-active") is True
        version = channel.send_command("get_property", "mpv-version")
        assert isinstance(version, str), version
        assert version.startswith("mpv "), version
    finally:
        launcher.terminate()
    assert not launcher.alive, "`quit` should have been enough; SIGKILL is the fallback"


@pytest.mark.skipif(MPV is None, reason=_skip_reason())
def test_the_socket_is_ours_alone(tmp_path: Path) -> None:
    """Least privilege, enforced by us rather than by an mpv option (AEP 17).

    The directory is 0700 and the socket 0600 after we see it. Both matter: anything that can
    name the socket can send `loadfile`, which is "change what this room hears" with no
    authentication by design (SAPRS 3.2's LAN trust ends at the appliance's own user).
    """

    temp_dir = tmp_path / "run"
    launcher = MpvLauncher(
        playback=PlaybackConfig(mpv_path=Path(MPV or "/usr/bin/mpv")),
        audio=AudioConfig(output="null", device="none"),
        temp_dir=temp_dir,
    )
    launcher.launch()
    try:
        path = socket_path_for(temp_dir)
        # The launcher names its socket with this pid, so the file we check is the one it made.
        assert path.exists(), f"{path} never appeared"
        assert (temp_dir.stat().st_mode & 0o777) == 0o700
        assert (path.stat().st_mode & 0o777) == 0o600
    finally:
        launcher.terminate()


# -- the state machine, against a machine that loads files slowly ----------


@pytest.mark.skipif(MPV is None, reason=_skip_reason())
def test_a_requested_track_reaches_playing_and_is_announced_once(
    appliance: Appliance, tmp_path: Path
) -> None:
    """`Loading` is a real state with a real duration, not a formality.

    One `SongStarted`, and it arrives when the file is actually open — never a `SongFinished`
    for a track that had not begun. The regression this pins is the reason the phase's
    "playback start" benchmark reads 18 ms and a real appliance takes longer: the mock was
    synchronous and the engine is not.

    The window is asserted as "LOADING or PLAYING, and never anything else", not as "LOADING",
    because how long a file takes to open is mpv's business: on a page-cached local MP3 it can
    beat the first property read, and a test that required the intermediate state would be a
    timing assertion dressed as a state one.
    """

    song = _song(tmp_path, 1, seconds=1.0)
    appliance.playback.play(song, queue_item_id=QueueItemId(7))

    assert _state_of(appliance.playback) in {PlaybackState.LOADING, PlaybackState.PLAYING}, (
        f"a load that was accepted left the machine in {_state_of(appliance.playback).value}"
    )
    assert appliance.recorder.finished == [], "a track cannot finish before it started"

    assert appliance.wait(lambda: _state_of(appliance.playback) is PlaybackState.PLAYING), (
        f"mpv never started; {appliance.launcher.diagnostics()}"
    )
    appliance.tick()

    assert appliance.recorder.started == [(1, 7)], "one start, naming the request"
    assert appliance.recorder.finished == []
    assert appliance.wait(lambda: appliance.playback.progress.duration > timedelta(0)), (
        "mpv never reported a length; `duration` is unavailable for a moment after `file-loaded`, "
        "which is a fact about demuxing and not about this state machine"
    )


@pytest.mark.skipif(MPV is None, reason=_skip_reason())
def test_a_track_that_runs_out_ends_once(appliance: Appliance, tmp_path: Path) -> None:
    """The eof edge, with mpv deciding when it happens rather than a test."""

    appliance.play_until_finished(_song(tmp_path, 2, seconds=0.5))

    assert appliance.recorder.started == [(2, 1)]
    assert [(song, item, reason) for song, item, reason in appliance.recorder.finished] == [
        (2, 1, "completed")
    ]
    assert _state_of(appliance.playback) is PlaybackState.IDLE
    for _ in range(3):
        appliance.tick()
    assert len(appliance.recorder.finished) == 1, "idle ticks must not re-report the ending"


@pytest.mark.skipif(MPV is None, reason=_skip_reason())
def test_paused_makes_no_progress_and_resuming_does(appliance: Appliance, tmp_path: Path) -> None:
    """SAPRS 7.4's pause, measured against the engine's own clock.

    Position is read twice across a third of a second. It is asserted *not* to move, which a
    mock can only mimic by being told, and it is asserted to move after resume, which is the
    half that catches a resume that merely clears a flag.
    """

    appliance.playback.play(_song(tmp_path, 3, seconds=3.0), queue_item_id=QueueItemId(1))
    assert appliance.wait(lambda: _state_of(appliance.playback) is PlaybackState.PLAYING)
    assert appliance.playback.pause() is True
    assert _state_of(appliance.playback) is PlaybackState.PAUSED

    time.sleep(0.15)
    first = appliance.playback.progress.position
    time.sleep(0.4)
    second = appliance.playback.progress.position
    assert abs((second - first).total_seconds()) < 0.05, f"paused but advanced {second - first}"

    assert appliance.playback.resume() is True
    assert appliance.wait(lambda: appliance.playback.progress.position > second), (
        "resume returned True and the engine disagreed"
    )
    assert _state_of(appliance.playback) is PlaybackState.PLAYING


@pytest.mark.skipif(MPV is None, reason=_skip_reason())
def test_a_file_mpv_cannot_open_is_reported_not_left_loading(
    appliance: Appliance, tmp_path: Path
) -> None:
    """The grace deadline, on the engine's own behaviour rather than on ours.

    mpv accepts `loadfile` for a file it then fails to demux, and sits idle. Without a
    deadline the appliance would hold the queue item `Playing` and the party would wait for a
    song that does not exist; with one, the failure is a fact like any other and the queue
    moves on (SAPRS 11.9).
    """

    broken = tmp_path / "garbage.mp3"
    broken.write_bytes(b"NOT AN MP3 FILE" * 64)
    song = _song(tmp_path, 4)
    object.__setattr__(song, "file_path", broken)  # frozen dataclass; the test owns the file

    start = time.monotonic()
    appliance.playback.play(song, queue_item_id=QueueItemId(1))
    deadline = start + LOAD_GRACE_SECONDS + 2.0
    while time.monotonic() < deadline and not appliance.recorder.finished:
        time.sleep(0.1)
        appliance.tick()

    assert appliance.recorder.finished == [(4, 1, "failed")], (
        f"the refused load never became a fact in {LOAD_GRACE_SECONDS:g}s"
    )
    assert _state_of(appliance.playback) is PlaybackState.IDLE
    appliance.play_until_finished(_song(tmp_path, 5, seconds=0.4), item=2)
    assert appliance.recorder.finished[1] == (5, 2, "completed"), "the queue recovered by itself"


# -- recovery, which is what the supervisor is for -------------------------


@pytest.mark.skipif(MPV is None, reason=_skip_reason())
def test_a_killed_mpv_ends_its_track_and_comes_back(appliance: Appliance, tmp_path: Path) -> None:
    """SAPRS 7.6 end to end, with a real process dying.

    The order is the point: `SongFinished(FAILED)` before `PlaybackRecovered`, because the
    queue's recovery handler starts the head item only when the player is idle, and a queue
    that wakes to find a track still marked `Playing` starts nothing at all.
    """

    appliance.playback.play(_song(tmp_path, 6, seconds=5.0), queue_item_id=QueueItemId(1))
    assert appliance.wait(lambda: _state_of(appliance.playback) is PlaybackState.PLAYING)
    appliance.recorder.started.clear()

    appliance.launcher.kill()

    assert appliance.wait(lambda: len(appliance.recorder.finished) == 1), (
        "the death produced no fact about the track that died with it"
    )
    assert appliance.recorder.finished[0][2] == "failed"
    assert _wait_for(
        lambda: appliance.launcher.alive and _state_of(appliance.playback) is PlaybackState.IDLE,
    ), "mpv did not come back"
    assert appliance.supervisor.restart_count >= 1
    health = appliance.supervisor.health()
    assert health.status is HealthStatus.HEALTHY, health.detail


@pytest.mark.skipif(MPV is None, reason=_skip_reason())
def test_playback_continues_across_a_restart_with_the_real_engine(
    appliance: Appliance, tmp_path: Path
) -> None:
    """The claim ADR-005 stakes: a crash costs one track, not the party."""

    recovered = 0

    def on_recovered(event: PlaybackRecovered) -> None:
        nonlocal recovered
        recovered += 1

    appliance.events.subscribe(PlaybackRecovered, on_recovered, name="real-mpv-recovery")

    appliance.playback.play(_song(tmp_path, 7, seconds=4.0), queue_item_id=QueueItemId(1))
    assert appliance.wait(lambda: _state_of(appliance.playback) is PlaybackState.PLAYING)
    appliance.launcher.kill()
    assert appliance.wait(lambda: recovered > 0), "no recovery fact"

    # A fresh track on the *new* mpv, which is where a stale channel would show up.
    assert appliance.playback.play(_song(tmp_path, 8, seconds=1.0), queue_item_id=QueueItemId(2))
    assert appliance.wait(lambda: appliance.recorder.started[-1:] == [(8, 2)]), (
        "the restarted engine never announced the next track"
    )


# -- the claim about our own fixtures ---------------------------------------


@pytest.mark.skipif(MPV is None, reason=_skip_reason())
def test_the_synthetic_corpus_is_what_we_claim_it_is(appliance: Appliance, tmp_path: Path) -> None:
    """`tests/support/media.py`'s honesty box, checked with the tool it is a claim about.

    The repository says its silent containers are "enough for metadata, not enough for
    decoding". MP3 turns out to decode (mpv reports a length and position), FLAC does not
    (mpv refuses the file and stays idle). Both halves are asserted here so the sentence in
    `docs/Developer/Testing.md` cannot rot into either an overclaim or an unnecessary
    limitation, and so a change to the generator that quietly breaks MP3 fails somewhere.
    """

    mp3 = _song(tmp_path, 9, seconds=0.6)
    appliance.play_until_finished(mp3)
    assert appliance.recorder.finished[-1][2] == "completed", "the MP3 corpus stopped decoding"
    assert appliance.playback.progress.duration >= timedelta(0)

    flac = tmp_path / "silent.flac"
    generate_track(silent_track(title="Silent", duration_seconds=0.6, fmt="flac"), flac)
    channel = appliance.supervisor.channel()
    channel.send_command("stop")
    channel.send_command("loadfile", str(flac), "replace")
    time.sleep(0.6)
    assert channel.send_command("get_property", "idle-active") is True, (
        "mpv now decodes the frame-less FLAC the generator writes; the docs and the "
        "skip policy should be updated rather than this assertion quietly relaxed"
    )
