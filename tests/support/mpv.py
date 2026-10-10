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
