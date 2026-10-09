"""mpv control: JSON IPC client, state machine, supervisor and recovery.

The Playback Service is the only component that controls the audio engine
(SAPRS 7.1). Playback code never imports HTTP or presentation code and never
embeds hardware-specific assumptions (SAPRS 7.7, 7.9).

Four modules, one per thing that can go wrong:

* `ipc` — the socket, the JSON line protocol, and the child process that owns it.
* `player` — mpv's vocabulary: which property means "finished", which command is a
  gapless load.
* `service` — `PlaybackService`: SAPRS 7.4's commands, the 7.3 state machine, and the
  facts on the bus.
* `supervisor` — `PlaybackSupervisor`: launch, monitor, detect, restart, reconnect,
  report health (SAPRS 7.2).
* `transition` — the gapless/crossfade policy, which is configuration made audible.
* `errors` — the failures, named so that a socket never reaches a controller.

Nothing here imports `encore.api`, `encore.controllers`, Jinja, SQLite or the library:
the file to play arrives as an argument (SAPRS 7.9). `tests/unit/test_architecture_guardrails.py`
checks that rather than trusting this sentence.
"""

from __future__ import annotations

from encore.playback.errors import (
    EngineUnavailableError,
    IllegalTransitionError,
    MpvCommandError,
    MpvGoneError,
    MpvTimeoutError,
    PlaybackError,
)
from encore.playback.ipc import (
    CommandChannel,
    JsonIpc,
    MpvLauncher,
    MpvProcess,
    PopenLike,
    Socket,
    socket_path_for,
)
from encore.playback.player import EngineObservation, MpvPlayer
from encore.playback.service import EngineRecovery, PlaybackService, PlayingTrack
from encore.playback.supervisor import (
    COMPONENT,
    EngineLauncher,
    PlaybackSupervisor,
    RecoveryReport,
)
from encore.playback.transition import TransitionPolicy

__all__ = [
    "COMPONENT",
    "CommandChannel",
    "EngineLauncher",
    "EngineObservation",
    "EngineRecovery",
    "EngineUnavailableError",
    "IllegalTransitionError",
    "JsonIpc",
    "MpvCommandError",
    "MpvGoneError",
    "MpvLauncher",
    "MpvPlayer",
    "MpvProcess",
    "MpvTimeoutError",
    "PlaybackError",
    "PlaybackService",
    "PlaybackSupervisor",
    "PlayingTrack",
    "PopenLike",
    "RecoveryReport",
    "Socket",
    "TransitionPolicy",
    "socket_path_for",
]
