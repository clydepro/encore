"""Tests for the mock mpv interface used by playback tests (PBK 16)."""

from __future__ import annotations

import json

import pytest

from tests.support.mpv import (
    MockMpv,
    MpvCommand,
    MpvCrashedError,
    MpvError,
    decode_command,
    encode_command,
)


def test_ipc_line_round_trip() -> None:
    line = encode_command("loadfile", "/music/song.flac", "replace", request_id=7)
    payload = decode_command(line)
    assert payload["command"] == ["loadfile", "/music/song.flac", "replace"]
    assert payload["request_id"] == 7
    assert line.endswith("\n")


def test_successful_command_reports_success() -> None:
    mpv = MockMpv()
    reply = mpv.send(encode_command("get_property", "idle", request_id=1))
    assert reply["error"] == "success"
    assert reply["data"] is True
    assert reply["request_id"] == 1


def test_loadfile_marks_player_busy_and_emits_event() -> None:
    mpv = MockMpv()
    mpv.send_command("loadfile", "mock://track-1")
    assert mpv.send_command("get_property", "idle") is False
    assert mpv.send_command("get_property", "filename") == "mock://track-1"
    assert mpv.take_command("loadfile") == MpvCommand("loadfile", ("mock://track-1",))
    assert mpv.expect_event("file-loaded").data["path"] == "mock://track-1"


def test_pause_and_resume_are_observable_through_properties() -> None:
    mpv = MockMpv()
    mpv.send_command("loadfile", "mock://track-2")
    mpv.next_event()

    mpv.send_command("set_property", "pause", True)
    assert mpv.send_command("get_property", "pause") is True
    mpv.expect_event("property-change")

    mpv.send_command("set_property", "pause", False)
    assert mpv.send_command("get_property", "pause") is False
    mpv.expect_event("property-change")


def test_append_mode_is_distinct_from_replace() -> None:
    mpv = MockMpv()
    mpv.send_command("loadfile", "mock://track-3", "append")
    mpv.expect_event("file-loaded")
    assert mpv.expect_event("queue-loaded").data["mode"] == "append"


def test_unknown_command_fails_loudly() -> None:
    mpv = MockMpv()
    with pytest.raises(MpvError, match="unsupported command"):
        mpv.send_command("teleport", "mock://somewhere")


def test_reply_error_is_surfaced() -> None:
    mpv = MockMpv()
    reply = mpv.send(encode_command("teleport", request_id=3))
    assert reply["error"] == "failure"
    assert reply["request_id"] == 3


def test_crash_prevents_further_commands() -> None:
    """Recovery tests need a process that is demonstrably dead (SAPRS 7.6)."""

    mpv = MockMpv()
    mpv.crash()
    assert mpv.running is False
    with pytest.raises(MpvCrashedError):
        mpv.send(encode_command("get_property", "time-pos"))


def test_terminate_stops_the_process() -> None:
    mpv = MockMpv()
    mpv.send_command("terminate")
    assert mpv.expect_event("shutdown").name == "shutdown"
    assert mpv.running is False


def test_commands_are_recorded_in_order() -> None:
    mpv = MockMpv()
    mpv.send_command("loadfile", "mock://a")
    mpv.send_command("set_property", "pause", True)
    mpv.send_command("stop")
    assert mpv.command_names() == ["loadfile", "set_property", "stop"]


def test_loading_a_file_clears_the_previous_tracks_state() -> None:
    """The double's most important obligation: `eof-reached` belongs to a file, not to a process.

    mpv reports end-of-file for the track it loaded, and starting a new one resets the flag.
    A mock that left it set made a queue advance look like it had walked the whole list in one
    tick — which is a behaviour the real appliance cannot have, and the reason the recovery
    and progress tests would have passed against a lie.
    """

    mpv = MockMpv()
    mpv.send_command("loadfile", "mock://a")
    mpv.properties["eof-reached"] = True
    mpv.properties["pause"] = True

    mpv.send_command("loadfile", "mock://b")

    assert mpv.properties["eof-reached"] is False
    assert mpv.properties["pause"] is False
    assert mpv.properties["filename"] == "mock://b"
    assert mpv.properties["time-pos"] == 0.0


def test_event_payload_is_valid_json() -> None:
    mpv = MockMpv()
    line = encode_command("set_property", "volume", 42)
    assert json.loads(line)["command"][0] == "set_property"
    mpv.send_command("set_property", "volume", 42)
    assert mpv.next_event().data["data"] == 42
