"""Mock mpv interface (PBK 16, SAPRS Chapter 7, AIG 10).

Unit tests must never require the real `mpv` binary (SAPRS 14.4), so the IPC
surface Encore will drive is modelled here in miniature:

- mpv's JSON IPC accepts one JSON object per line, shaped
  ``{"command": [...], "request_id": n}``.
- Replies carry ``error`` (``"success"`` when OK) and the matching ``request_id``.
- Asynchronous events are pushed unprompted as ``{"event": "...", ...}``.

`MockMpv` is the process-side double for tests written before the playback
milestone exists: it records commands, answers property reads, queues events and
can be killed to exercise recovery.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Final

from encore.playback.errors import MpvCommandError, MpvGoneError

#: Properties Encore reads for progress reporting (SAPRS 7.5). Kept aligned with
#: `encore/playback/player.py`'s `_PROPERTIES`: an unknown name raises `KeyError` here
#: rather than "property unavailable", which is what makes a renamed property a failing
#: test instead of a silent difference between the double and the real player.
DEFAULT_PROPERTIES: Final[dict[str, Any]] = {
    # mpv's *property* is `idle-active`; `idle` is the option and the command. Both are
    # kept because a double that answers either is a double that cannot hide the mix-up.
    "idle-active": True,
    "idle": True,
    "pause": False,
    "time-pos": 0.0,
    "duration": 0.0,
    "eof-reached": False,
    "volume": 100,
    "metadata": {},
    "filename": "",
}

#: Kept importable from here for the tests written before the playback milestone existed:
#: the double raises the errors the real client raises, so a handler written against the
#: mock cannot be wrong about the real thing.
MpvError = MpvCommandError

#: What mpv's `file_error` says when the refusal is about the machine rather than the file.
#: Transcribed from 0.35.1 on the reference hardware, where it is the *same* sentence for
#: three different faults: `--ao=alsa` opening a card with nothing on the other side of it, and
#: `--ao=pulse` and `--ao=pipewire` with no server the service account can reach (#48).
#: `tests/integration/test_real_mpv.py` checks this string against a real engine, because a
#: double that invents it would let the classifier pass on a machine that says something else.
AUDIO_OUTPUT_FAILURE: Final = "audio output initialization failed"
MpvCrashedError = MpvGoneError


@dataclass(frozen=True)
class MpvCommand:
    """A parsed command line received by `MockMpv`."""

    name: str
    args: tuple[Any, ...] = ()


@dataclass(frozen=True)
class MpvEvent:
    """An asynchronous event pushed by mpv (immutable, per AEP 19)."""

    name: str
    data: dict[str, Any] = field(default_factory=dict)


def encode_command(name: str, *args: Any, request_id: int | None = None) -> str:
    """Serialise an mpv command exactly as the JSON IPC expects it."""

    payload: dict[str, Any] = {"command": [name, *args]}
    if request_id is not None:
        payload["request_id"] = request_id
    return json.dumps(payload, ensure_ascii=False) + "\n"


def decode_command(line: str) -> dict[str, Any]:
    """Parse one JSON IPC line sent to mpv."""

    decoded: dict[str, Any] = json.loads(line)
    return decoded


class MockMpv:
    """In-process stand-in for an mpv instance driven over JSON IPC."""

    def __init__(self, *, name: str = "mock-mpv") -> None:
        self.name = name
        self.running = True
        self.closed = False
        self.properties: dict[str, Any] = dict(DEFAULT_PROPERTIES)
        self.commands: list[MpvCommand] = []
        self.events: deque[MpvEvent] = deque()
        self._refusal: dict[str, Any] | None = None

    def __repr__(self) -> str:
        state = "running" if self.running else "stopped"
        return f"<MockMpv {self.name} {state} commands={len(self.commands)}>"

    # -- transport ---------------------------------------------------------

    def send(self, line: str) -> dict[str, Any]:
        """Handle one JSON IPC line and return the reply mpv would send."""

        if not self.running:
            raise MpvCrashedError("mpv is not running")
        payload = decode_command(line)
        argv: list[Any] = list(payload["command"])
        request_id: int | None = payload.get("request_id")
        command = MpvCommand(name=str(argv[0]), args=tuple(argv[1:]))
        self.commands.append(command)
        try:
            data = self._dispatch(command)
        except MpvError as error:
            return {"error": "failure", "details": str(error), "request_id": request_id}
        return {"error": "success", "data": data, "request_id": request_id}

    def send_command(self, name: str, *args: Any) -> Any:
        """Convenience wrapper: encode, send and return the reply payload."""

        reply = self.send(encode_command(name, *args))
        if reply["error"] != "success":
            raise MpvError(f"{name} failed: {reply.get('details', 'unknown error')}")
        data: Any = reply.get("data")
        return data

    def _dispatch(self, command: MpvCommand) -> Any:
        name, args = command.name, command.args
        match name:
            case "loadfile":
                self._loadfile(args)
            case "stop":
                self.properties.update(
                    {"idle": True, "idle-active": True, "time-pos": 0.0, "eof-reached": False}
                )
            case "set_property":
                self._set_property(str(args[0]), args[1])
            case "get_property":
                return self.properties[str(args[0])]
            case "observe_property":
                self.emit("properties-changed", {"name": str(args[-1])})
            case "terminate":
                self.running = False
                self.emit("shutdown")
            case _:
                raise MpvError(f"unsupported command: {name}")
        return None

    def _loadfile(self, args: tuple[Any, ...]) -> None:
        mode = str(args[1]) if len(args) > 1 else "replace"
        refusal, self._refusal = self._refusal, None
        if refusal is not None:
            # The command is accepted and the file is not: nothing loads, the engine stays
            # idle, and the verdict arrives afterwards as `end-file` — the ordering #48 was
            # found in, and the one `refuse_file` cannot show because it describes an engine
            # that has refused already rather than one about to.
            self.properties.update(DEFAULT_PROPERTIES)
            self.emit("end-file", refusal)
            if mode != "replace":
                self.emit("queue-loaded", {"mode": mode})
            return
        self.properties.update(
            {
                "filename": str(args[0]),
                "idle": False,
                "idle-active": False,
                "time-pos": 0.0,
                # Both are cleared by loading a file, and a double that left them set is the
                # kind of convenient lie that makes an end-of-track loop look normal: mpv
                # reports `eof-reached` for the file it loaded, not for the one before it, and
                # a queue advance that trusted the stale flag would drain the queue in one tick.
                "eof-reached": False,
                "pause": False,
            }
        )
        self.emit("file-loaded", {"path": str(args[0])})
        if mode != "replace":
            self.emit("queue-loaded", {"mode": mode})

    def _set_property(self, key: str, value: Any) -> None:
        self.properties[key] = value
        self.emit("property-change", {"name": key, "data": value})

    # -- test-side helpers -------------------------------------------------

    def emit(self, name: str, data: dict[str, Any] | None = None) -> None:
        """Push an event as if mpv had written it to the IPC socket."""

        self.events.append(MpvEvent(name=name, data=dict(data or {})))

    def poll_events(self) -> list[dict[str, Any]]:
        """Drain the queue in mpv's line shape, as `JsonIpc.poll_events` does.

        Present because the real transport has this surface and `MpvPlayer.observe()` consults
        it for the `end-file` verdict the polled properties cannot express. A double without it
        would silently make that code path untestable without the binary — and note that
        draining means an event read here is not also there for `next_event`, which is what a
        real socket does to a test that expected to pop one (SAPRS 14.4's trade, paid here).
        """

        drained, self.events = list(self.events), deque()
        return [{"event": event.name, **event.data} for event in drained]

    def refuse_file(self, reason: str = "unrecognized file format") -> None:
        """Say what mpv says about a file it opened and could not demux.

        The command is accepted, the properties never appear, and an `end-file` arrives with
        `reason: "error"` — measured on 0.35.1. This is how a test without the binary can tell
        the appliance "that one failed" rather than leaving it to infer it from a timeout.
        """

        self.properties.update(DEFAULT_PROPERTIES)
        self.emit("end-file", {"reason": "error", "file_error": reason})

    def refuse_output(self, reason: str = AUDIO_OUTPUT_FAILURE) -> None:
        """Say what mpv says about a sound device it cannot open.

        Shaped exactly like `refuse_file` and meant entirely differently, which is the whole of
        #48: unless the double can tell those two apart, the appliance's ability to tell them
        apart needs a broken sound card to test. Read `PlaybackFault` for what the difference is
        answered with.
        """

        self.refuse_file(reason)

    def refuse_next_load(self, detail: str) -> None:
        """Accept the next `loadfile`, then refuse it, in that order.

        The ordering is the point. A real engine answers `loadfile` by accepting it and failing
        afterwards, so a service test that wants to watch a refusal unfold needs the refusal to
        arrive with the load rather than after it — otherwise the double reports a track playing
        first, and the `SongStarted` that follows is a fiction no broken file or broken sound
        card ever produced (#48).
        """

        self._refusal = {"reason": "error", "file_error": detail}

    def crash(self) -> None:
        """Simulate an unexpected mpv death (SAPRS 7.6, SAPRS 14.10)."""

        self.running = False
        self.emit("log", {"level": "error", "message": "mpv terminated unexpectedly"})

    def close(self) -> None:
        """Let go of the connection, as `JsonIpc` does.

        Part of the surface a caller of a channel may use, so that "did the replacement path
        close the socket it replaced?" is answerable in a test without a real descriptor. It
        does not stop the engine: closing a connection and killing a process are different
        things, and the supervisor does both on purpose.
        """

        self.closed = True

    def command_names(self) -> list[str]:
        return [command.name for command in self.commands]

    def take_command(self, name: str) -> MpvCommand:
        """Return and remove the oldest recorded command called `name`."""

        for index, command in enumerate(self.commands):
            if command.name == name:
                return self.commands.pop(index)
        raise AssertionError(f"no recorded command {name!r}; saw {self.command_names()}")

    def next_event(self) -> MpvEvent:
        """Pop the next queued event, failing loudly when the queue is empty."""

        if not self.events:
            raise AssertionError("no queued mpv events")
        return self.events.popleft()

    def expect_event(self, name: str) -> MpvEvent:
        """Pop the next queued event and assert its type."""

        event = self.next_event()
        assert event.name == name, f"expected event {name!r}, got {event.name!r}"
        return event


class FakeEngine:
    """An `EngineLauncher` over `MockMpv`, for a test that needs a whole appliance.

    `apps/server/appliance.py:build` takes a launcher rather than a channel, because the
    supervisor starts and stops the process it is talking to; a test that handed it a
    ready-made socket could not exercise a restart at all. Each `launch()` after the first
    returns a *new* double, which is what makes "did the player pick up the new channel?" a
    question with an answer in the test rather than a claim in a docstring.

    `alive` is the property the supervisor polls (SAPRS 7.6's detection path), so it follows
    the mock's process state rather than being a flag nobody had to keep honest.
    """

    def __init__(self) -> None:
        self.mpv = MockMpv()
        self.launches = 0
        self.terminations = 0

    def launch(self) -> MockMpv:
        self.launches += 1
        if self.launches > 1:
            self.mpv = MockMpv()
        return self.mpv

    def terminate(self) -> None:
        self.terminations += 1
        self.mpv.crash()

    @property
    def alive(self) -> bool:
        return self.mpv.running

    def diagnostics(self) -> str:
        return f"mock mpv ({self.launches} launch{'es' if self.launches != 1 else ''})"

    def command_names(self) -> list[str]:
        """Every command this engine's channels have been given, oldest first."""

        return self.mpv.command_names()

    def count(self, name: str) -> int:
        return self.command_names().count(name)
