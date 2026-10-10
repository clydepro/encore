"""`MpvPlayer`: mpv's vocabulary, and the six properties that answer "what is happening".

The player is the only module that names an mpv property. Everything above it speaks in
states and seconds, which is what makes SAPRS 7.7's promise testable: a different player
build would change this file and nothing else.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from encore.domain import PlaybackState
from encore.playback import MpvPlayer
from encore.playback.errors import MpvCommandError, MpvGoneError
from tests.support.mpv import MockMpv

TRACK = Path("/music/first/track-1.flac")


@pytest.fixture
def mpv() -> MockMpv:
    return MockMpv()


@pytest.fixture
def player(mpv: MockMpv) -> MpvPlayer:
    return MpvPlayer(lambda: mpv, volume=85.0)


def _settings(mpv: MockMpv, name: str) -> list[object]:
    """Every value written to one property, in order. `take_command` only gives the oldest."""

    return [
        command.args[1]
        for command in mpv.commands
        if command.name == "set_property" and command.args and command.args[0] == name
    ]


def test_a_loaded_file_replaces_the_playlist_rather_than_appending_to_it(
    player: MpvPlayer, mpv: MockMpv
) -> None:
    """The queue is the playlist. mpv holding a second copy of the order is where two
    orders disagree (SAPRS 8.5)."""

    player.load(TRACK)

    command = mpv.take_command("loadfile")
    assert command.args == (str(TRACK), "replace")


def test_loading_paused_cues_the_track_without_sound(player: MpvPlayer, mpv: MockMpv) -> None:
    player.load(TRACK, paused=True)

    assert mpv.take_command("loadfile")
    assert mpv.send_command("get_property", "pause") is True
    assert mpv.properties["pause"] is True


def test_an_idle_engine_reads_as_idle(player: MpvPlayer) -> None:
    observation = player.observe()

    assert observation.state is PlaybackState.IDLE
    assert observation.filename == ""
    assert observation.position == timedelta(0)


def test_a_running_engine_reads_as_playing(player: MpvPlayer, mpv: MockMpv) -> None:
    player.load(TRACK)

    observation = player.observe()

    assert observation.state is PlaybackState.PLAYING
    assert observation.has_file


def test_a_paused_engine_reads_as_paused(player: MpvPlayer, mpv: MockMpv) -> None:
    player.load(TRACK)
    player.set_paused(True)

    assert player.observe().state is PlaybackState.PAUSED


def test_the_end_of_a_file_outranks_the_pause_it_left_behind(
    player: MpvPlayer, mpv: MockMpv
) -> None:
    """mpv sets `pause` as part of reaching eof.

    Reading that as "paused" would leave the queue waiting for a resume nobody asked for,
    which is a silent appliance and a full screen (SAPRS 8.7).
    """

    player.load(TRACK)
    mpv.properties.update({"pause": True, "eof-reached": True})

    observation = player.observe()

    assert observation.state is PlaybackState.FINISHED
    assert observation.finished


def test_seek_is_an_absolute_number_of_seconds(player: MpvPlayer, mpv: MockMpv) -> None:
    """`seek` defaults to relative semantics depending on flags; seconds cannot be misread."""

    player.load(TRACK)
    player.seek_to(timedelta(seconds=95.5))

    assert _settings(mpv, "time-pos") == [95.5]
    assert player.observe().position == timedelta(seconds=95.5)


def test_seeking_before_the_start_is_refused_before_it_reaches_the_engine(
    player: MpvPlayer, mpv: MockMpv
) -> None:
    before = len(mpv.commands)

    with pytest.raises(ValueError, match="cannot seek before"):
        player.seek_to(timedelta(seconds=-1))

    assert len(mpv.commands) == before


def test_stop_clears_the_file_so_the_next_reading_is_idle(player: MpvPlayer, mpv: MockMpv) -> None:
    player.load(TRACK)

    player.stop()

    assert mpv.take_command("stop")
    assert player.observe().state is PlaybackState.IDLE


def test_volume_can_be_borrowed_by_a_fade_and_handed_back(player: MpvPlayer, mpv: MockMpv) -> None:
    player.set_volume(40.0)
    assert mpv.take_command("set_property").args == ("volume", 40.0)

    player.restore_volume()

    assert mpv.send_command("get_property", "volume") == 85.0


def test_volume_is_rounded_rather_than_streamed(player: MpvPlayer, mpv: MockMpv) -> None:
    """A fade writes one command per tick, so an unrounded float is a new value every time.

    mpv accepts 41.299999999999997 but the log then has 60 numbers a second in it, and a
    diagnostic that cannot be read is not a diagnostic.
    """

    player.set_volume(41.299999999999997)

    assert mpv.take_command("set_property").args == ("volume", 41.3)


def test_a_property_this_build_does_not_have_reads_as_absent(
    player: MpvPlayer, mpv: MockMpv
) -> None:
    """`eof-reached` needs mpv 0.32 and `idle-active` has changed name once.

    Absent is a normal answer; it is not a reason to stop reporting progress.
    """

    original = mpv.send_command

    def unknown(name: str, *args: object) -> Any:
        if name == "get_property" and args and args[0] == "eof-reached":
            raise MpvCommandError("get_property", "item not found")
        return original(name, *args)

    mpv.send_command = unknown  # type: ignore[method-assign]
    player.load(TRACK)

    assert player.observe().state is PlaybackState.PLAYING
    assert player.observe().filename == str(TRACK)


def test_a_property_read_that_fails_because_the_engine_died_propagates(
    player: MpvPlayer, mpv: MockMpv
) -> None:
    """The distinction the supervisor depends on: refused is normal, gone is not."""

    player.load(TRACK)
    mpv.crash()

    with pytest.raises(MpvGoneError):
        player.observe()


def test_properties_exposes_the_raw_reading_for_diagnostics(
    player: MpvPlayer, mpv: MockMpv
) -> None:
    player.load(TRACK)
    mpv.properties["duration"] = 200.0

    values = player.properties()

    assert values["filename"] == str(TRACK)
    assert values["duration"] == 200.0
    assert values["idle-active"] is False


def test_a_negative_duration_from_a_stream_becomes_zero_not_an_exception(
    player: MpvPlayer, mpv: MockMpv
) -> None:
    """mpv answers -1 for "unknown length" on a stream or a damaged header.

    A negative duration would make `PlaybackProgress.fraction` negative, and a progress bar
    that goes backwards is the kind of thing a guest notices before an operator does.
    """

    player.load(TRACK)
    mpv.properties["duration"] = -1.0

    assert player.observe().duration == timedelta(0)


def test_a_source_that_raises_gone_is_not_swallowed_by_the_flag_helpers(
    player: MpvPlayer, mpv: MockMpv
) -> None:
    """`_flag` tolerates a refused read; it must not tolerate a dead one."""

    mpv.crash()

    with pytest.raises(MpvGoneError):
        player.observe()


def test_mpvs_verdict_on_a_file_it_refused_arrives_with_the_reading(
    player: MpvPlayer, mpv: MockMpv
) -> None:
    """The properties cannot tell a refused file from a finished one; `end-file` can.

    Measured on mpv 0.35.1: a file whose demux fails leaves the engine idle, with no filename
    and no position — the same reading a 400 ms track leaves when it ends between two polls.
    The verdict is the only difference, and it travels with the properties rather than
    replacing them, because the rest of the design still reads at ~1 Hz (ADR-005).
    """

    mpv.refuse_file("unrecognized file format")

    observation = player.observe()

    assert observation.end_verdict == "error"
    assert observation.refused
    assert observation.end_detail == "unrecognized file format"
    assert observation.state is PlaybackState.IDLE, (
        "the verdict adds a fact about the file; it does not hide what the engine is doing"
    )


def test_a_verdict_is_read_once_because_a_socket_drains(player: MpvPlayer, mpv: MockMpv) -> None:
    """The second reading has nothing to say, which is why the service must act on the first."""

    mpv.refuse_file()

    assert player.observe().refused
    assert player.observe().end_verdict is None


def test_an_engine_that_asks_for_the_file_again_has_not_given_a_verdict(
    player: MpvPlayer, mpv: MockMpv
) -> None:
    """`redirect` and `stop` are mpv's own business.

    A demuxer redirecting to a second entry is a retry of the same request, and a `stop` is a
    command Encore sent: reporting either as an ending would answer a skip with a fact the
    queue has already acted on.
    """

    mpv.emit("end-file", {"reason": "redirect"})
    mpv.emit("end-file", {"reason": "stop"})

    assert player.observe().end_verdict is None


def test_the_last_verdict_in_a_batch_is_the_one_that_counts(
    player: MpvPlayer, mpv: MockMpv
) -> None:
    """Several `end-file` lines can arrive between two readings; the newest describes now."""

    mpv.emit("end-file", {"reason": "eof"})
    mpv.refuse_file("file not found")

    observation = player.observe()

    assert observation.end_verdict == "error"
    assert observation.end_detail == "file not found"


def test_a_transport_with_no_events_has_no_verdict() -> None:
    """`CommandChannel` does not require an event surface, so neither does this.

    Keeping `poll_events` off the protocol is the argument ADR-011 makes about `Player`: a
    double that models commands should not have to model a connection. The cost is that "the
    engine said nothing" has to be a real answer rather than an `AttributeError`, and this is
    where that is pinned.
    """

    class CommandsOnly:
        def send_command(self, name: str, *args: object) -> Any:  # noqa: ARG002
            values = {"idle-active": True, "pause": False, "eof-reached": False}
            return values.get(str(args[0]))

    channel = CommandsOnly()
    observation = MpvPlayer(lambda: channel).observe()

    assert observation.state is PlaybackState.IDLE
    assert observation.end_verdict is None


def test_a_refusal_without_a_reason_still_names_the_verdict(
    player: MpvPlayer, mpv: MockMpv
) -> None:
    """Some builds flag the failure in `file_error` as a boolean and log the text elsewhere.

    The verdict is the fact Encore acts on; the detail only helps an operator, and "mpv gave no
    reason" is a truer log line than an absent one.
    """

    mpv.emit("end-file", {"reason": "error", "file_error": True})

    observation = player.observe()

    assert observation.refused
    assert observation.end_detail == "mpv gave no reason"
