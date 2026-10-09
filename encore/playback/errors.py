"""Playback failures, named (SAPRS 7.6, 7.9, 11.9, AEP 15).

The rule this module exists for is that mpv stays behind `encore/playback/` (SAPRS 7.9),
and an exception is part of that boundary: if a socket error or a subprocess exit code
reached a controller, the isolation would be nominal. Every type here is therefore about
what Encore was *trying to do*, not about what the operating system said.

Two of them — `MpvCommandError` and `MpvGoneError` — are also the errors a test double
must raise to be faithful to the protocol, so `tests/support/mpv.py` imports them rather
than inventing its own.
"""

from __future__ import annotations

__all__ = [
    "EngineUnavailableError",
    "IllegalTransitionError",
    "MpvCommandError",
    "MpvGoneError",
    "MpvTimeoutError",
    "PlaybackError",
]


class PlaybackError(RuntimeError):
    """Base for everything that can go wrong while making sound."""


class MpvCommandError(PlaybackError):
    """mpv answered, and the answer was not `success`.

    A reply, not a silence: the engine is alive and refused. The common causes are an
    unavailable property (`time-pos` while idle) and a file it cannot open, which is
    SAPRS 14.10's "missing media file" arriving as a reply rather than a crash.
    """

    def __init__(self, command: str, detail: str = "") -> None:
        super().__init__(f"mpv refused {command!r}" + (f": {detail}" if detail else ""))
        self.command = command
        self.detail = detail


class MpvGoneError(PlaybackError):
    """The command could not be sent because mpv is not there any more (SAPRS 7.6).

    This is the signal the supervisor watches for. It says nothing about *why* mpv
    died, which is deliberate: the diagnosis is the restart's `reason` and the log's
    `diagnostics`, and a crash that reads like a filesystem error sends an operator to
    the wrong page of the guide.
    """


class MpvTimeoutError(PlaybackError):
    """mpv neither replied nor died within the IPC timeout.

    Distinct from `MpvGoneError` because the recovery differs: a hung player is killed
    and restarted, a dead one is only restarted, and treating the first as the second
    is how a zombie mpv ends a party.
    """


class EngineUnavailableError(PlaybackError):
    """mpv cannot be started at all (SAPRS 7.2, 7.6).

    Raised after the backoff schedule is exhausted, so the message an operator reads is
    "this box will not play", not a stack trace from `Popen`. A missing binary is the
    installer's bug and the Administrator Guide says so.
    """

    def __init__(self, detail: str, *, attempts: int) -> None:
        super().__init__(f"{detail} ({attempts} attempts)")
        self.attempts = attempts


class IllegalTransitionError(PlaybackError):
    """A state change SAPRS 7.3 does not allow was attempted.

    The transition table lives in `encore.domain.playback` and is the diagram in the
    specification, so reaching this error means a code path invented a state change the
    diagram does not describe. It is raised rather than logged because the alternative
    is a player whose state nobody can predict.
    """

    def __init__(self, current: object, requested: object) -> None:
        super().__init__(
            f"SAPRS 7.3 does not allow {current} -> {requested}; "
            "see encore/domain/playback.py for the permitted edges"
        )
        self.current = current
        self.requested = requested
