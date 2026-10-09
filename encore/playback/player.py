"""The mpv vocabulary: what the engine is told and what it reports (SAPRS 7.4, 7.5).

`encore/playback/ipc.py` knows how to send a line of JSON. This module knows what to put
in it: which property means "the track ended", which command is a gapless load, what
"unavailable" implies about a player with nothing loaded. That is the half of ADR-005 that
matters — the vocabulary is the interface, and a wrong property name is indistinguishable
from a working player until a real mpv answers.

Two rules keep this module from becoming a second state machine:

* It **reports** a `PlaybackState` derived from what mpv says and **never decides** a
  transition. The service owns the diagram (SAPRS 7.3) and its legality; the player owns
  the reading.
* It swallows exactly one kind of failure — a property that does not exist because
  nothing is loaded (`MpvCommandError`) — and re-raises everything else. A player that
  answers "0 seconds" for a crash is how an appliance plays silence and reports success.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Final, Protocol

from encore.domain import PlaybackState
from encore.playback.errors import MpvCommandError
from encore.playback.ipc import CommandChannel

__all__ = ["EngineObservation", "MpvPlayer", "PropertySource"]

#: The properties `observe()` reads, named once. `idle-active` rather than `idle`
#: because the latter is an option and a command in mpv; the *property* is
#: `idle-active`, and reading the wrong one answers "unavailable" instead of True.
_PROPERTIES: Final = (
    "idle-active",
    "pause",
    "time-pos",
    "duration",
    "eof-reached",
    "filename",
)


class PropertySource(Protocol):
    """Where a live channel comes from.

    A provider rather than a channel because the channel does not outlive a crash: after
    the supervisor restarts mpv, the next read must go to the *new* process (SAPRS 7.6
    step 5, "reconnect IPC") without the player being rebuilt.
    """

    def __call__(self) -> CommandChannel: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineObservation:
    """One reading of the engine.

    Attributes:
        state: What mpv appears to be doing. Derived, not authoritative — see the module
            docstring; the service decides whether it can *get* there.
        position: Where in the file it is. Zero when nothing is loaded.
        duration: The file's length, zero when mpv has not decoded a header yet.
        filename: The loaded file, empty when idle. Compared against what the service
            asked for, which is how a load that silently did nothing is caught.
    """

    state: PlaybackState = PlaybackState.IDLE
    position: timedelta = timedelta(0)
    duration: timedelta = timedelta(0)
    filename: str = ""

    @property
    def has_file(self) -> bool:
        return bool(self.filename)

    @property
    def finished(self) -> bool:
        return self.state is PlaybackState.FINISHED


class MpvPlayer:
    """mpv's command surface, as Python methods.

    Args:
        source: Returns the live channel. `PlaybackSupervisor.channel` in production, a
            `lambda: mock_mpv` in tests — `MockMpv.send_command` satisfies the protocol
            with no adapter, which is the whole point of the seam.
        volume: The configured output level, restored by `resume` and by every load, so a
            crossfade that ended early cannot leave the appliance quiet.
    """

    def __init__(self, source: PropertySource, *, volume: float = 100.0) -> None:
        self._source = source
        self._volume = volume

    # -- commands (SAPRS 7.4) ---------------------------------------------

    def load(self, path: Path, *, paused: bool = False) -> None:
        """Replace whatever is loaded with `path` — the gapless transition (SAPRS 7.8).

        `replace` rather than `append`: the queue is the playlist. Handing mpv a playlist
        would put the ordering SAPRS 8.5 promises in a second place, and a second place
        is where it disagrees with the first.
        """

        self._command("loadfile", str(path), "replace")
        self.set_paused(paused)

    def stop(self) -> None:
        self._command("stop")

    def set_paused(self, paused: bool) -> None:
        self._command("set_property", "pause", bool(paused))

    def set_volume(self, volume: float) -> None:
        self._command("set_property", "volume", round(volume, 2))

    def restore_volume(self) -> None:
        self.set_volume(self._volume)

    def seek_to(self, position: timedelta) -> None:
        """Jump to an absolute position (SAPRS 7.4's "seek where appropriate").

        `set_property time-pos` rather than the `seek` command: `seek` defaults to
        relative-to-the-end semantics depending on flags, and an absolute number of
        seconds is the one form that cannot be misread.
        """

        if position < timedelta(0):
            raise ValueError(f"cannot seek before the start of a track, got {position}")
        self._command("set_property", "time-pos", position.total_seconds())

    # -- readings (SAPRS 7.5) ---------------------------------------------

    def observe(self) -> EngineObservation:
        """One ~1 Hz reading, in as few round trips as mpv allows.

        Six properties, six replies. A single `get_property` per line is what the JSON IPC
        offers; the alternative is `observe_property` with a callback, which needs a thread
        and a queue to be read at all (see the reasoning in `service.tick`).
        """

        channel = self._source()
        idle = _flag(channel, "idle-active")
        paused = _flag(channel, "pause")
        eof = _flag(channel, "eof-reached")
        filename = _text(channel, "filename")
        position = _seconds(channel, "time-pos")
        duration = _seconds(channel, "duration")
        return EngineObservation(
            state=_derive(idle=idle, paused=paused, eof=eof, has_file=bool(filename)),
            position=position,
            duration=duration,
            filename=filename,
        )

    def properties(self) -> dict[str, Any]:
        """Every property `observe()` reads, raw. For a health report and for logs."""

        channel = self._source()
        return {name: _read(channel, name) for name in _PROPERTIES}

    def _command(self, name: str, *args: object) -> Any:
        return self._source().send_command(name, *args)


def _derive(*, idle: bool, paused: bool, eof: bool, has_file: bool) -> PlaybackState:
    """The reading, in words. Order matters: eof wins over pause.

    A track that has run out reaches `eof-reached` while `idle` is still False, and mpv
    may report `pause` set by the end-of-file handling. Calling that "paused" would leave
    the queue waiting for a resume that nobody asked for.
    """

    if idle or not has_file:
        return PlaybackState.IDLE
    if eof:
        return PlaybackState.FINISHED
    if paused:
        return PlaybackState.PAUSED
    return PlaybackState.PLAYING


def _read(channel: CommandChannel, name: str) -> Any:
    """One property, or None if this player has no reason to know it yet."""

    try:
        return channel.send_command("get_property", name)
    except MpvCommandError:
        return None


def _flag(channel: CommandChannel, name: str) -> bool:
    return bool(_read(channel, name))


def _text(channel: CommandChannel, name: str) -> str:
    value = _read(channel, name)
    return "" if value is None else str(value)


def _seconds(channel: CommandChannel, name: str) -> timedelta:
    """A duration property as a `timedelta`, tolerating mpv's three answers.

    None (nothing loaded), a float (normal), or -1 (mpv's own "unknown", which a stream
    or a damaged header can produce). All three become a length the caller can display as
    "—", because progress is presentation and must never raise (SAPRS 11.9).
    """

    value = _read(channel, name)
    if value is None or isinstance(value, bool):
        return timedelta(0)
    try:
        seconds = float(value)
    except (TypeError, ValueError):  # pragma: no cover - mpv answers with numbers
        return timedelta(0)
    return timedelta(seconds=seconds) if seconds > 0 else timedelta(0)
