"""mpv's JSON IPC, and the process that owns the socket (SAPRS 7.1-7.4, ADR-005).

Two objects, because they fail differently:

* `JsonIpc` speaks the protocol — one JSON object per line in, replies and pushed
  events out, `request_id` correlating the two. It is what `MpvPlayer` uses and what
  `tests/support/mpv.py` doubles, through `CommandChannel`, which is deliberately the
  single method that double already implements.
* `MpvProcess`/`MpvLauncher` own the child process: the argv, the socket path, readiness,
  the kill. Everything hardware-specific goes through here, which is how SAPRS 7.7 keeps
  "playback code must not embed hardware assumptions" true — the only audio words in
  this file come from `AudioConfig`.

**Why the calls are synchronous and the playback is not** (SAPRS 7.4). `loadfile` returns
as soon as mpv has accepted the file; decoding and the first samples arrive later, which
is exactly the `Loading → Playing` edge in SAPRS 7.3. Waiting for the *reply* is a socket
round trip of milliseconds; waiting for the *audio* would block a worker on a decoder.
So the commands here are ordinary calls, and the asynchrony SAPRS asks for is the state
machine noticing what the engine did next (see `service.tick`).

`send_command` raises `MpvGoneError` when the socket is gone and `MpvTimeoutError` when mpv
is there but silent. The supervisor treats those differently, and that difference is the
only reason a hung player does not become a zombie that plays nothing forever.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import subprocess
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

from encore.config.models import AudioConfig, PlaybackConfig
from encore.playback.errors import MpvCommandError, MpvGoneError, MpvTimeoutError

__all__ = [
    "CommandChannel",
    "JsonIpc",
    "MpvLauncher",
    "MpvProcess",
    "PopenLike",
    "Socket",
    "socket_path_for",
]

#: How long one IPC round trip may take before the engine is considered stuck. Generous
#: against a Pi 4 that is decoding, opening a file and serving a page at once; short
#: enough that a hung mpv is a known failure a second later rather than a mystery at the
#: end of the party.
IPC_TIMEOUT_SECONDS: Final = 2.0

#: How long `start()` waits for mpv to create its socket. mpv is ready in tens of
#: milliseconds; this is the ceiling, not the expectation.
READY_TIMEOUT_SECONDS: Final = 5.0

#: Linux bounds an abstract-or-filesystem socket path; saying so at startup beats an
#: `OSError: Invalid argument` from inside a child process.
UNIX_PATH_LIMIT: Final = 107

_ACCEPT: Final = frozenset({"success"})


@runtime_checkable
class CommandChannel(Protocol):
    """Something that can be given an mpv command and answers with mpv's reply.

    Kept to one method on purpose: it is the whole of what the playback layer needs from
    a transport, and it is already the shape of `MockMpv.send_command`, so the double in
    `tests/support/mpv.py` is a faithful one with no adapter and no subclassing.
    """

    def send_command(self, name: str, *args: Any) -> Any: ...


class Socket(Protocol):
    """The part of a socket this client touches.

    Declared rather than assumed because it is the seam that lets the framing rules below
    be tested at all: a reply split across two reads cannot be made to happen with a real
    mpv on demand.
    """

    def sendall(self, data: bytes) -> None: ...

    def recv(self, bufsize: int) -> bytes: ...

    def settimeout(self, value: float | None) -> None: ...

    def shutdown(self, how: int) -> None: ...

    def close(self) -> None: ...


class PopenLike(Protocol):
    """A launched mpv, as far as the supervisor's questions go.

    `subprocess.Popen` satisfies it. The four methods are exactly the supervisor's vocabulary
    — is it there, stop it, stop it harder, how did it go — and nothing else, so a test can
    stage "exited at once" against "opened no socket" (SAPRS 7.6 steps 1-2).
    """

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = ...) -> int: ...


class JsonIpc:
    """A connected mpv IPC socket.

    Args:
        socket_path: The `--input-ipc-server` path mpv created.
        timeout: Per-round-trip ceiling, in seconds.
        connect: How to open that path. Production uses `_connect`, and a test can hand in a
            fake so that a reply split across two reads, or two replies in one read, is
            exercised without an mpv to do the splitting (ADR-005).

    Raises:
        MpvGoneError: Nothing is listening at `socket_path`.
    """

    def __init__(
        self,
        socket_path: Path,
        *,
        timeout: float = IPC_TIMEOUT_SECONDS,
        connect: Callable[[Path], Socket] | None = None,
    ) -> None:
        self._path = Path(socket_path)
        self._timeout = timeout
        self._buffer = b""
        self._next_id = 0
        self._events: list[dict[str, Any]] = []
        self._closed = False
        self._socket = (connect or _connect)(self._path)
        self._socket.settimeout(timeout)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def closed(self) -> bool:
        return self._closed

    def send_command(self, name: str, *args: Any) -> Any:
        """Send one command and return the `data` of its reply.

        Event lines that arrive while waiting are kept for `poll_events()` rather than
        dropped: mpv interleaves them, and losing `end-file` because it arrived beside a
        `get_property` reply would make progress reporting depend on luck.
        """

        if self._closed:
            raise MpvGoneError(f"{self._path} is closed")
        self._next_id += 1
        request_id = self._next_id
        payload: dict[str, Any] = {"command": [name, *args], "request_id": request_id}
        self._write(json.dumps(payload, ensure_ascii=False) + "\n")
        while True:
            message = self._decode(self._read_line())
            if message.get("request_id") == request_id:
                return _unwrap(name, message)
            if "event" in message:
                self._events.append(message)

    def _decode(self, line: str) -> dict[str, Any]:
        """Parse one reply line, or say that mpv is not answering properly.

        A truncated or non-JSON line means the connection is not carrying the protocol —
        which is `MpvGoneError` territory, and worth naming rather than letting a
        `JSONDecodeError` surface from inside a progress poll.
        """

        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            raise MpvGoneError(
                f"{self._path}: reply was not a JSON object: {line[:60]!r}"
            ) from error
        if not isinstance(message, dict):
            raise MpvGoneError(f"{self._path}: reply was not an object: {line[:60]!r}")
        return message

    def poll_events(self) -> list[dict[str, Any]]:
        """Events pushed since the last call, oldest first. Drained."""

        events, self._events = self._events, []
        return events

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(OSError):  # pragma: no cover - already half-closed by the peer
            self._socket.shutdown(socket.SHUT_RDWR)
        self._socket.close()

    def _write(self, line: str) -> None:
        try:
            self._socket.sendall(line.encode("utf-8"))
        except OSError as error:
            raise MpvGoneError(f"{self._path}: {error.strerror or error}") from error

    def _read_line(self) -> str:
        """One newline-terminated JSON object, or a named failure.

        The loop is the whole of the subtlety: `recv` returns short, and a line can
        arrive split across two reads. Anything but a timeout or a clean EOF is mpv
        going away mid-reply, which is `MpvGoneError` (SAPRS 7.6).
        """

        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = self._buffer[:newline]
                self._buffer = self._buffer[newline + 1 :]
                return line.decode("utf-8")
            try:
                chunk = self._socket.recv(65536)
            except TimeoutError as error:
                raise MpvTimeoutError(
                    f"{self._path}: no reply within {self._timeout:g}s"
                ) from error
            except OSError as error:
                raise MpvGoneError(f"{self._path}: {error.strerror or error}") from error
            if not chunk:
                raise MpvGoneError(f"{self._path}: mpv closed the socket")
            self._buffer += chunk


class MpvProcess:
    """One mpv child process, and the socket that talks to it.

    Args:
        argv: The full command line, built by `MpvLauncher`.
        socket_path: Where mpv will create its IPC socket; must not exist, and is removed
            if it does — a stale socket from a killed server is the common case.
        ready_timeout: Ceiling on how long `start()` waits for that socket.
        spawn: How to create the child. Defaults to `subprocess.Popen`; tests hand in a
            double, because "mpv never opened its socket" and "mpv exited first" are two
            different diagnoses that a real mpv takes seconds to distinguish (SAPRS 7.6).
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        socket_path: Path,
        ready_timeout: float = READY_TIMEOUT_SECONDS,
        poll_interval: float = 0.02,
        spawn: Callable[[list[str]], PopenLike] | None = None,
    ) -> None:
        self._argv = list(argv)
        self._socket_path = Path(socket_path)
        self._ready_timeout = ready_timeout
        self._poll_interval = poll_interval
        self._spawn = spawn or _popen
        self._process: PopenLike | None = None

    def start(self) -> JsonIpc:
        """Launch mpv and return a connected channel.

        Raises:
            MpvGoneError: The binary could not be executed, or the socket never appeared.
        """

        _remove_if_stale(self._socket_path)
        try:
            self._process = self._spawn(self._argv)
        except OSError as error:
            raise MpvGoneError(
                f"cannot start {self._argv[0]!r}: {error.strerror or error}"
            ) from error
        self._await_socket()
        return JsonIpc(self._socket_path, timeout=self._ready_timeout)

    def stop(self, *, graceful: bool = True) -> None:
        """Quit, then escalate. Never hangs.

        A `SIGKILL` without the `quit` command is what leaves a decoder half-written;
        waiting a bounded time for mpv to leave by itself costs nothing when it does and
        costs two seconds when it does not, which is the trade SAPRS 11.8's "clean exit"
        asks for.
        """

        process = self._process
        if process is None or process.poll() is not None:
            return
        if graceful:
            try:
                channel = JsonIpc(self._socket_path, timeout=0.5)
            except MpvGoneError:
                pass
            else:
                try:
                    channel.send_command("quit")
                except (MpvCommandError, MpvGoneError, MpvTimeoutError):
                    pass
                finally:
                    channel.close()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            # A second timeout is possible on a process stuck in uninterruptible I/O, and
            # at that point waiting longer helps nobody: SIGKILL is outstanding, the socket
            # path is removed by the next launch, and shutdown has to finish.
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2.0)

    @property
    def alive(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    @property
    def pid(self) -> int | None:
        process = self._process
        return None if process is None else process.pid

    @property
    def exit_code(self) -> int | None:
        """What mpv returned, if it has returned. `diagnostics` is for humans."""

        process = self._process
        return None if process is None else process.poll()

    def diagnostics(self) -> str:
        """The one line SAPRS 7.6 step 2 asks for: what died, and with what status.

        stderr is deliberately not captured — mpv is verbose and the journal is not, so a
        pipe nobody reads would eventually block the player mid-track. The exit code, the
        socket path and the fact that nothing was redirected is the honest trade.
        """

        if self._process is None:
            return "mpv was never started"
        if self.alive:
            return f"mpv running as pid {self.pid}"
        return f"mpv exited with code {self.exit_code}"

    def _await_socket(self) -> None:
        """Wait for mpv to create its socket, noticing a death before a timeout.

        The `poll` check is the part that matters: an unconfigured or missing binary exits
        immediately, and waiting out the timeout would report "mpv never opened a socket"
        where the truth is "mpv exited with code 1" (SAPRS 7.6 step 2).
        """

        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise MpvGoneError(
                    f"mpv exited with code {self.exit_code} before opening {self._socket_path}"
                )
            if self._socket_path.exists():
                return
            time.sleep(self._poll_interval)
        raise MpvGoneError(f"mpv did not open {self._socket_path} within {self._ready_timeout:g}s")


class MpvLauncher:
    """Builds mpv's command line from configuration and owns the current process.

    This is the only module in Encore that knows what an mpv option is called. SAPRS 7.7
    makes that a requirement rather than tidiness: audio output, device and volume come
    from `audio:`, so a different Pi needs a different YAML and not a different build.
    """

    def __init__(
        self,
        *,
        playback: PlaybackConfig,
        audio: AudioConfig,
        temp_dir: Path,
        ready_timeout: float = READY_TIMEOUT_SECONDS,
    ) -> None:
        self._playback = playback
        self._audio = audio
        self._temp_dir = Path(temp_dir)
        self._ready_timeout = ready_timeout
        self._process: MpvProcess | None = None

    def argv(self, socket_path: Path) -> list[str]:
        """The command line for one mpv instance.

        `--idle=yes` is not optional: without it mpv exits between tracks, and the
        supervisor would restart the engine for every song and read each restart as a
        crash.
        """

        gapless = "--gapless-audio=yes" if self._audio.gapless else "--gapless-audio=no"
        return [
            str(self._playback.mpv_path),
            "--idle=yes",
            "--no-video",
            "--force-window=no",
            "--no-terminal",
            "--really-quiet",
            f"--ao={self._audio.output}",
            f"--audio-device={self._audio.device}",
            f"--volume={self._audio.volume}",
            gapless,
            f"--input-ipc-server={socket_path}",
            "--input-ipc-run=0600",
        ]

    def launch(self) -> CommandChannel:
        """Start a new mpv, replacing any current one, and return its channel."""

        self.terminate()
        path = socket_path_for(self._temp_dir)
        process = MpvProcess(self.argv(path), socket_path=path, ready_timeout=self._ready_timeout)
        self._process = process
        try:
            return process.start()
        except MpvGoneError:
            self._process = None
            raise

    def terminate(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            process.stop()

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.alive

    def diagnostics(self) -> str:
        return "mpv is not running" if self._process is None else self._process.diagnostics()


def socket_path_for(temp_dir: Path, *, suffix: str = "") -> Path:
    """A per-process, per-attempt socket path inside `paths.temp_dir`.

    The directory is configuration (SAPRS 13.3), not a guess at `/tmp`, and the pid keeps
    two appliances on one machine from colliding. `suffix` distinguishes restarts, so a
    dying mpv's socket is never mistaken for the new one's.
    """

    directory = Path(temp_dir)
    directory.mkdir(parents=True, exist_ok=True)
    name = f"mpv-{os.getpid()}{suffix}.sock"
    path = directory / name
    if len(str(path)) > UNIX_PATH_LIMIT:
        raise ValueError(
            f"paths.temp_dir is too deep for a UNIX socket: {path} is "
            f"{len(str(path))} characters (limit {UNIX_PATH_LIMIT})"
        )
    return path


def _unwrap(command: str, message: dict[str, Any]) -> Any:
    """Return a reply's payload, raising this module's error for mpv's words."""

    status = str(message.get("error", "success"))
    if status not in _ACCEPT:
        details = str(message.get("details") or "")
        raise MpvCommandError(command, details or status)
    data: Any = message.get("data")
    return data


def _remove_if_stale(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as error:  # pragma: no cover - permissions, not the common case
        if error.errno != errno.EACCES:
            raise
        return


def _connect(path: Path) -> Socket:
    """Open one connection to mpv's socket, or name why not.

    One connection per command is what SAPRS 7.9 specifies and what mpv expects: the
    server closes an idle client, so a long-lived connection looks alive right up until
    the command that needed it.
    """

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(path))
    except OSError as error:
        sock.close()
        raise MpvGoneError(f"{path}: {error.strerror or error}") from error
    return sock


def _popen(argv: list[str]) -> PopenLike:
    """The real spawn, with the streams pointed away from the appliance.

    stderr is deliberately not captured: mpv is verbose, and a pipe nobody reads fills and
    blocks the player mid-track. The exit code is the diagnostic that matters (SAPRS 7.6).
    """

    return subprocess.Popen(  # noqa: S603 - argv is built from validated config, never from input
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
