"""mpv's JSON IPC at the byte level, and the process around it (ADR-005, SAPRS 7.6-7.7).

`MockMpv` accepts the same command objects the real client builds, but it accepts them from
a function call rather than from a socket. Everything that can go wrong between
`send_command` and mpv's reply — framing, coalescing, splitting, the request id that
correlates a reply with the command that caused it, an event arriving beside an answer —
is covered here instead, against a fake socket that replays prepared bytes.

The launcher half covers the startup failures an operator can otherwise never tell apart:
mpv that never opens its socket, mpv that exits first, and a `paths.temp_dir` the appliance
cannot prepare — which must be a named launch failure like the other two, because it is the
one of them that arrives from `tick()` during recovery.
"""

from __future__ import annotations

import json
import os
import socket as socket_module
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import closing, suppress
from pathlib import Path
from typing import Any

import pytest

from encore.config import AudioConfig, PlaybackConfig
from encore.playback import MpvLauncher, MpvProcess, socket_path_for
from encore.playback.errors import MpvCommandError, MpvGoneError, MpvTimeoutError
from encore.playback.ipc import JsonIpc

SOCKET = Path("/tmp/encore-test-mpv.sock")


class FakeSocket:
    """A socket that replays prepared bytes and records what was written.

    Chunks are handed out in the order given and a drained socket returns `b""`, which is
    what a closed one does — so a client that spins on an empty read fails a timeout test
    rather than hanging the suite.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.sent: list[bytes] = []
        self.closed = False
        self.timeout: float | None = None

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, bufsize: int) -> bytes:  # noqa: ARG002 - the client chooses its own chunk size
        return self.chunks.pop(0) if self.chunks else b""

    def settimeout(self, value: float | None) -> None:
        self.timeout = value

    def shutdown(self, how: int) -> None:  # noqa: ARG002
        self.closed = True

    def close(self) -> None:
        self.closed = True


def _ipc(chunks: list[bytes], *, path: Path = SOCKET, timeout: float = 5.0) -> JsonIpc:
    return JsonIpc(path, timeout=timeout, connect=lambda _: FakeSocket(chunks))


def _reply(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload) + "\n").encode()


# -- framing --------------------------------------------------------------


def test_two_replies_in_one_read_are_two_replies() -> None:
    ipc = _ipc(
        [
            _reply({"error": "success", "data": 1, "request_id": 1})
            + _reply({"error": "success", "data": 2, "request_id": 2})
        ]
    )

    assert ipc.send_command("get_property", "time-pos") == 1
    assert ipc.send_command("get_property", "duration") == 2


def test_a_reply_split_across_reads_is_one_reply() -> None:
    """The bug this prevents: treating each read as a message truncates the next one."""

    payload = _reply({"error": "success", "data": {"x": "y"}, "request_id": 1})
    ipc = _ipc([payload[:9], payload[9:]])

    assert ipc.send_command("get_property", "metadata") == {"x": "y"}


def test_a_partial_trailing_line_dies_with_the_connection() -> None:
    """A cut-off tail belongs to a dead mpv, and a half reply must not become a whole one."""

    ipc = _ipc([_reply({"error": "success", "data": 1, "request_id": 1}) + b'{"error": "suc'])

    assert ipc.send_command("get_property", "volume") == 1
    with pytest.raises(MpvGoneError, match="closed the socket"):
        ipc.send_command("get_property", "volume")


def test_a_line_that_is_not_json_is_named_not_crashed() -> None:
    ipc = _ipc([b"not json at all\n"])

    with pytest.raises(MpvGoneError, match="not a JSON object"):
        ipc.send_command("get_property", "volume")


def test_a_json_line_that_is_not_an_object_is_named_too() -> None:
    """`.get` on a list would raise an AttributeError from inside a progress poll."""

    ipc = _ipc([b"[1, 2, 3]\n"])

    with pytest.raises(MpvGoneError, match="not an object"):
        ipc.send_command("get_property", "volume")


def test_the_command_is_the_array_form_with_a_request_id() -> None:
    """mpv's deprecated object form carries no request id, so replies cannot be matched.

    The id restarts at 1 per connection, which is what makes one connection per command
    safe: a reply can only belong to the command this socket sent.
    """

    sock = FakeSocket([_reply({"error": "success", "data": None, "request_id": 1})])
    JsonIpc(SOCKET, connect=lambda _: sock).send_command("loadfile", "/music/a.flac", "replace")

    assert json.loads(sock.sent[0]) == {
        "command": ["loadfile", "/music/a.flac", "replace"],
        "request_id": 1,
    }
    assert sock.sent[0].endswith(b"\n"), "mpv reads commands by line"


def test_request_ids_increase_so_a_stale_reply_cannot_answer_the_wrong_command() -> None:
    sock = FakeSocket(
        [
            _reply({"error": "success", "data": "a", "request_id": 1}),
            _reply({"error": "success", "data": "b", "request_id": 2}),
        ]
    )
    ipc = JsonIpc(SOCKET, connect=lambda _: sock)

    assert ipc.send_command("get_property", "time-pos") == "a"
    assert ipc.send_command("get_property", "duration") == "b"
    assert [json.loads(s)["request_id"] for s in sock.sent] == [1, 2]


def test_events_that_arrive_beside_a_reply_are_kept_not_dropped() -> None:
    """mpv interleaves; losing `end-file` would make progress reporting depend on luck."""

    ipc = _ipc(
        [
            _reply({"event": "file-loaded"}),
            _reply({"error": "success", "data": 12.0, "request_id": 1}),
        ]
    )

    assert ipc.send_command("get_property", "time-pos") == 12.0
    assert [event["event"] for event in ipc.poll_events()] == ["file-loaded"]
    assert ipc.poll_events() == [], "events are drained, not re-reported"


def test_a_failed_command_raises_naming_the_command_and_mpvs_reason() -> None:
    ipc = _ipc([_reply({"error": "item not found", "request_id": 1})])

    with pytest.raises(MpvCommandError) as raised:
        ipc.send_command("get_property", "audio-delay")

    assert raised.value.command == "get_property"
    assert raised.value.detail == "item not found"


def test_a_success_with_no_data_is_not_the_same_as_an_absent_property() -> None:
    ipc = _ipc([_reply({"error": "success", "request_id": 1})])

    assert ipc.send_command("get_property", "pause") is None


def test_a_silent_mpv_times_out_with_the_wait_named() -> None:
    """mpv that is there but says nothing must not be mistaken for mpv that died.

    A hung player is killed and restarted; a dead one is only restarted. Treating the first
    as the second leaves a zombie that plays nothing for the rest of the night (SAPRS 7.6).
    """

    class SilentSocket(FakeSocket):
        def recv(self, bufsize: int) -> bytes:  # noqa: ARG002
            raise TimeoutError

    ipc = JsonIpc(SOCKET, timeout=2.0, connect=lambda _: SilentSocket([]))

    with pytest.raises(MpvTimeoutError, match="no reply within 2s"):
        ipc.send_command("get_property", "time-pos")


def test_the_timeout_is_set_on_the_socket_before_the_write() -> None:
    """Without this a wedged mpv blocks the read forever and nothing ever times out."""

    sock = FakeSocket([_reply({"error": "success", "data": 1, "request_id": 1})])
    JsonIpc(SOCKET, timeout=1.25, connect=lambda _: sock).send_command("get_property", "volume")

    assert sock.timeout == 1.25


def test_a_write_to_a_dead_socket_is_reported_as_gone() -> None:
    class BrokenSocket(FakeSocket):
        def sendall(self, _: bytes) -> None:
            raise OSError("Broken pipe")

    ipc = JsonIpc(SOCKET, connect=lambda _: BrokenSocket([]))

    with pytest.raises(MpvGoneError, match="Broken pipe"):
        ipc.send_command("stop")


def test_nothing_listening_is_a_named_failure_at_construction(tmp_path: Path) -> None:
    """An unconfigured or dead path says so here, not three commands later."""

    with pytest.raises(MpvGoneError):
        JsonIpc(tmp_path / "nothing-here.sock")


def test_a_closed_channel_refuses_commands_rather_than_hanging() -> None:
    sock = FakeSocket([_reply({"error": "success", "data": 1, "request_id": 1})])
    ipc = JsonIpc(SOCKET, connect=lambda _: sock)
    ipc.close()

    assert ipc.closed
    with pytest.raises(MpvGoneError, match="is closed"):
        ipc.send_command("get_property", "volume")
    ipc.close()
    assert sock.closed, "closing twice must not close the socket twice"


# -- the process ----------------------------------------------------------


class FakeChild:
    """A launched mpv: a pid, a liveness answer, and a way to die on cue."""

    def __init__(self, *, exit_code: int | None = None) -> None:
        self._exit = exit_code
        self.pid = 4321
        self.terminated = False
        self.killed = False

    def exit_with(self, code: int) -> None:
        """Tell the double it has been reaped, so `poll` stops saying "running"."""

        self._exit = code

    def poll(self) -> int | None:
        return self._exit

    def terminate(self) -> None:
        self.terminated = True
        # A terminated child is a reaped child; a double that stayed "running" after
        # SIGTERM would let a shutdown that hung on `poll` pass unnoticed.
        self._exit = 0

    def kill(self) -> None:
        self.killed = True
        self._exit = -9

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        # A real `Popen.wait` only returns once the child has been reaped, and a double that
        # stayed "running" afterwards would let a shutdown that hung on `poll` pass.
        if self._exit is None:
            self._exit = 0
        return self._exit


def test_the_command_line_carries_the_configuration_not_assumptions() -> None:
    """SAPRS 7.7: a different Pi needs different YAML, not different playback code."""

    launcher = MpvLauncher(
        playback=PlaybackConfig(mpv_path=Path("/usr/bin/mpv")),
        audio=AudioConfig(output="alsa", device="snd_aloop", volume=63, gapless=False),
        temp_dir=Path("/tmp"),
    )

    argv = launcher.argv(Path("/tmp/mpv-1.sock"))

    assert argv[0] == "/usr/bin/mpv"
    assert "--idle=yes" in argv, (
        "without it mpv exits between tracks and every exit looks like a crash"
    )
    assert "--ao=alsa" in argv
    assert "--audio-device=snd_aloop" in argv
    assert "--volume=63" in argv
    assert "--gapless-audio=no" in argv
    assert "--input-ipc-server=/tmp/mpv-1.sock" in argv
    assert not any(arg.startswith("--input-ipc-run") for arg in argv), (
        "that option does not exist before mpv 0.36, and mpv treats an unknown option as "
        "fatal at parse time (measured on 0.35.1: 'option not found', exit 1) — so passing it "
        "means the appliance starts no player at all on an older engine. Socket permissions "
        "are set in "
        "`MpvProcess._restrict_socket` instead, which works on every version."
    )
    assert "--no-video" in argv


def test_start_reports_an_engine_that_exited_before_opening_a_socket(tmp_path: Path) -> None:
    """The difference between "bad mpv path" and "bad file", in one message."""

    child = FakeChild(exit_code=1)
    process = MpvProcess(
        ["mpv"],
        socket_path=tmp_path / "absent.sock",
        ready_timeout=1.0,
        poll_interval=0.01,
        spawn=lambda _: child,
    )

    with pytest.raises(MpvGoneError, match="exited with code 1"):
        process.start()

    assert process.diagnostics() == "mpv exited with code 1"


def test_start_reports_an_engine_that_never_opened_a_socket(tmp_path: Path) -> None:
    """mpv that is running but silent at the socket is a different failure, and it waits."""

    child = FakeChild()
    process = MpvProcess(
        ["mpv"],
        socket_path=tmp_path / "late.sock",
        ready_timeout=0.05,
        poll_interval=0.01,
        spawn=lambda _: child,
    )

    with pytest.raises(MpvGoneError, match="did not open"):
        process.start()

    assert process.diagnostics() == "mpv running as pid 4321"


class MiniMpv:
    """A real unix socket that answers command lines the way mpv does.

    Only the launch and shutdown paths need this much: they `connect()` and expect a reply,
    which no in-process fake can stand in for. Binding happens in `spawn`, after
    `MpvProcess` has removed the stale socket file — bind first and the removal takes the
    listener's path away underneath it, which is exactly the race a real mpv loses too.

    It *answers* rather than merely accepting, so `stop()`'s graceful `quit` completes
    instead of waiting out its half-second timeout.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.commands: list[str] = []
        self._server: socket_module.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def spawn(self, argv: list[str]) -> FakeChild:  # noqa: ARG002 - usable as MpvProcess.spawn
        """Start listening, and report a child process that is running."""

        self._server = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        self._server.bind(str(self.path))
        self._server.listen(4)
        self._server.settimeout(0.05)
        self._thread = threading.Thread(target=self._run, daemon=True, name="mini-mpv")
        self._thread.start()
        return FakeChild()

    def _run(self) -> None:
        server = self._server
        assert server is not None, "started by spawn"
        while not self._stop.is_set():
            try:
                connection = server.accept()[0]
            except TimeoutError:
                continue
            except OSError:
                break
            with closing(connection):
                self._answer(connection)

    def _answer(self, connection: socket_module.socket) -> None:
        connection.settimeout(1.0)
        try:
            line = connection.recv(4096).decode("utf-8").splitlines()[0]
            message = json.loads(line)
            request_id = message.get("request_id", 1)
            self.commands.append(str(message["command"][0]))
        except (OSError, IndexError, KeyError, ValueError):
            return
        with suppress(OSError):
            connection.sendall(_reply({"error": "success", "data": None, "request_id": request_id}))

    def stop(self) -> None:
        """Idempotent: the fixture and the test may both reach for it."""

        self._stop.set()
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


@pytest.fixture
def mini_mpvs(tmp_path: Path) -> Iterator[Callable[[str], MiniMpv]]:
    """Miniature players that are guaranteed to be listening no more."""

    made: list[MiniMpv] = []

    def make(name: str = "mpv.sock") -> MiniMpv:
        server = MiniMpv(tmp_path / name)
        made.append(server)
        return server

    yield make
    for server in made:
        server.stop()


def test_start_waits_for_the_socket_then_connects(mini_mpvs: Callable[[str], MiniMpv]) -> None:
    server = mini_mpvs("ready.sock")
    process = MpvProcess(
        ["mpv"], socket_path=server.path, ready_timeout=5.0, poll_interval=0.01, spawn=server.spawn
    )

    channel = process.start()

    assert isinstance(channel, JsonIpc)
    assert process.alive
    assert process.pid == 4321
    assert process.diagnostics() == "mpv running as pid 4321"

    channel.close()
    process.stop()

    assert server.commands == ["quit"], "mpv is asked to leave before it is made to"
    assert not process.alive, "mpv that has been asked to quit and is gone is not alive"


def test_a_stale_socket_from_a_previous_run_is_removed_first(tmp_path: Path) -> None:
    """The common case after `kill -9`: the file is there, nothing is listening."""

    path = tmp_path / "stale.sock"
    path.touch()
    child = FakeChild(exit_code=1)
    process = MpvProcess(
        ["mpv"], socket_path=path, ready_timeout=0.05, poll_interval=0.01, spawn=lambda _: child
    )

    with pytest.raises(MpvGoneError, match="exited with code 1"):
        process.start()

    assert not path.exists(), "the stale file was removed before mpv was asked to create it"


def test_stopping_an_engine_that_was_never_started_is_not_an_error(tmp_path: Path) -> None:
    """SAPRS 11.8's clean exit is best effort at shutdown, never a traceback in the log."""

    child = FakeChild(exit_code=0)
    process = MpvProcess(
        ["mpv"], socket_path=tmp_path / "gone.sock", ready_timeout=0.01, spawn=lambda _: child
    )

    process.stop()

    assert not child.terminated, "nothing was running to terminate"


def test_stopping_an_engine_that_already_exited_does_not_signal_it(
    mini_mpvs: Callable[[str], MiniMpv],
) -> None:
    """Signalling a reaped pid races the operating system and loses."""

    server = mini_mpvs("exit.sock")
    children: list[FakeChild] = []

    def spawn(argv: list[str]) -> FakeChild:
        child = server.spawn(argv)
        children.append(child)
        return child

    process = MpvProcess(
        ["mpv"], socket_path=server.path, ready_timeout=5.0, poll_interval=0.01, spawn=spawn
    )
    channel = process.start()
    channel.close()
    children[0].exit_with(0)

    process.stop()

    assert not children[0].terminated, "a reaped pid must not be signalled again"


def test_stop_escalates_to_a_kill_when_quit_is_ignored(
    mini_mpvs: Callable[[str], MiniMpv],
) -> None:
    """A wedged mpv still has to leave: shutdown cannot wait on it forever."""

    class StubbornChild(FakeChild):
        def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
            raise subprocess.TimeoutExpired(["mpv"], 2.0)

    server = mini_mpvs("stubborn.sock")
    children: list[StubbornChild] = []

    def spawn(argv: list[str]) -> StubbornChild:
        child = StubbornChild()
        server.spawn(argv)
        children.append(child)
        return child

    process = MpvProcess(
        ["mpv"], socket_path=server.path, ready_timeout=5.0, poll_interval=0.01, spawn=spawn
    )
    channel = process.start()
    channel.close()

    process.stop()

    assert server.commands == ["quit"], "the graceful route is attempted even when it is ignored"
    assert not children[0].terminated, "the IPC quit is the graceful signal; no SIGTERM is sent"
    assert children[0].killed, "and mpv is taken out of its misery rather than hung on shutdown"


def test_the_socket_path_is_per_process_and_within_the_kernel_limit(tmp_path: Path) -> None:
    path = socket_path_for(tmp_path)

    assert path.parent == tmp_path
    assert path.name == f"mpv-{os.getpid()}.sock"
    assert socket_path_for(tmp_path, suffix="-1").name == f"mpv-{os.getpid()}-1.sock"
    assert len(str(path)) <= 107


def test_a_temp_dir_too_deep_for_a_unix_socket_says_so_at_startup(tmp_path: Path) -> None:
    """Beyond 107 characters `bind` fails inside the child, where nothing explains it."""

    deep = tmp_path.joinpath("-" * 60, "-" * 60)

    with pytest.raises(ValueError, match="too deep"):
        socket_path_for(deep)


def test_the_socket_directory_is_private_before_mpv_is_asked_for_it(tmp_path: Path) -> None:
    """0700, and not left to the umask.

    Whoever can name the socket can send `loadfile`, which is "change what this room hears"
    with no authentication by design. The mode is asserted here rather than only in
    `test_real_mpv.py` because setting it needs no mpv, and the machine that has one is the
    machine that would otherwise be the only one to check.
    """

    run = tmp_path / "run"

    path = socket_path_for(run)

    assert path.parent == run
    assert (run.stat().st_mode & 0o777) == 0o700


def test_a_temp_dir_that_cannot_be_prepared_is_a_named_launch_failure(tmp_path: Path) -> None:
    """`OSError` from a misconfigured `paths.temp_dir` would escape `tick()`.

    The supervisor backs off on `MpvGoneError` and reports health on it; it does not catch a
    bare `PermissionError` or `NotADirectoryError`, so an appliance pointed at a directory it
    cannot create would raise from a timer rather than say "mpv cannot start, trying again in
    Ns" — the same difference in kind as the two failures above, and reachable from
    configuration alone.
    """

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    launcher = MpvLauncher(
        playback=PlaybackConfig(mpv_path=Path("/usr/bin/mpv")),
        audio=AudioConfig(output="null", device="none"),
        temp_dir=blocker / "run",
    )

    with pytest.raises(MpvGoneError, match="cannot prepare"):
        launcher.launch()

    assert launcher.alive is False, "a failed launch leaves nothing to reap"
