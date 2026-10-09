"""Playback state (SAPRS 4.6, 7.3).

Two things live here. `PlaybackState` is the vocabulary SAPRS 4.6 defines.
`ALLOWED_TRANSITIONS` is the machine SAPRS 7.3 draws, expressed as data: the
diagram is a requirement, and a supervisor that reaches a state the diagram does
not allow has a bug worth failing on rather than a surprise worth logging.

Owning mpv belongs to `encore/playback/` (milestone 8). Owning what its states
mean belongs here, so the queue, the admin interface and the SSE stream can all
ask the same question without importing the player.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from encore.domain.identifiers import SongId

__all__ = ["ALLOWED_TRANSITIONS", "PlaybackOutcome", "PlaybackProgress", "PlaybackState"]


class PlaybackState(StrEnum):
    """What the playback engine is doing (SAPRS 4.6)."""

    IDLE = "idle"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPING = "stopping"
    FINISHED = "finished"
    ERROR = "error"
    RECOVERING = "recovering"


class PlaybackOutcome(StrEnum):
    """How a track ended, as recorded in playback history (SAPRS 5.7, 7.2, 8.6).

    Distinct from `PlaybackState`, which describes what the engine is doing *now*.
    An outcome is written once, on the row that history keeps forever, and it is the
    value the statistics and the admin play log are grouped by. The four members are
    the four endings SAPRS describes: the song finished, someone skipped it, it was
    stopped or cleared, or the engine failed.
    """

    COMPLETED = "completed"
    SKIPPED = "skipped"
    STOPPED = "stopped"
    FAILED = "failed"

    @property
    def counts_as_played(self) -> bool:
        """Whether this ending belongs in "songs played" (SAPRS 10.3).

        A skip is not a play, and neither is a stop: both say the listener heard part of
        it, which is a different question from the one a host reads off the dashboard.
        `FAILED` is not a play either — the appliance promised a track and could not
        deliver one (SAPRS 11.9).

        The rule lives on the enum rather than in the statistics service so that every
        counter in the application answers the same question the same way, and so a new
        outcome has to be classified here before it can be counted anywhere.

        This property read `is not FAILED` until issue #23, which counted every skipped
        track as a play while its own docstring said the opposite. `SongFinished` had it
        right, and `tests/regression/test_issue_23_search_playback_queue.py` now pins the
        two together so they cannot drift again.
        """

        return self is PlaybackOutcome.COMPLETED


#: The SAPRS 7.3 diagram, including its `Error -> Recovering -> Idle/Playing`
#: edge. A state whose entry is not listed here is unreachable by design.
_TRANSITIONS: Final[dict[PlaybackState, frozenset[PlaybackState]]] = {
    PlaybackState.IDLE: frozenset({PlaybackState.LOADING, PlaybackState.STOPPING}),
    PlaybackState.LOADING: frozenset(
        {PlaybackState.PLAYING, PlaybackState.STOPPING, PlaybackState.ERROR}
    ),
    PlaybackState.PLAYING: frozenset(
        {
            PlaybackState.PAUSED,
            PlaybackState.FINISHED,
            PlaybackState.STOPPING,
            PlaybackState.ERROR,
        }
    ),
    PlaybackState.PAUSED: frozenset(
        {PlaybackState.PLAYING, PlaybackState.STOPPING, PlaybackState.ERROR}
    ),
    PlaybackState.STOPPING: frozenset({PlaybackState.IDLE, PlaybackState.ERROR}),
    PlaybackState.FINISHED: frozenset({PlaybackState.IDLE}),
    PlaybackState.ERROR: frozenset({PlaybackState.RECOVERING, PlaybackState.IDLE}),
    PlaybackState.RECOVERING: frozenset(
        {PlaybackState.IDLE, PlaybackState.PLAYING, PlaybackState.ERROR}
    ),
}


#: The table, read-only. A transition map a running process could edit would turn
#: one buggy handler into an unexplainable night, so the mutation is closed off
#: at import rather than policed in review.
ALLOWED_TRANSITIONS: Final[Mapping[PlaybackState, frozenset[PlaybackState]]] = MappingProxyType(
    _TRANSITIONS
)


def can_transition(current: PlaybackState, following: PlaybackState) -> bool:
    """True when SAPRS 7.3 permits `current` to be followed by `following`."""

    return following in ALLOWED_TRANSITIONS[current]


@dataclass(frozen=True, slots=True, kw_only=True)
class PlaybackProgress:
    """One progress observation (SAPRS 7.5).

    Roughly once per second. Position and duration are kept in seconds because
    that is the unit mpv reports, and converting at the boundary rather than in
    the player keeps the arithmetic the UI does cheap.
    """

    state: PlaybackState
    song_id: SongId | None = None
    position: timedelta = timedelta(0)
    duration: timedelta = timedelta(0)

    def __post_init__(self) -> None:
        if self.position < timedelta(0):
            raise ValueError(f"position must not be negative, got {self.position}")
        if self.duration < timedelta(0):
            raise ValueError(f"duration must not be negative, got {self.duration}")

    @property
    def fraction(self) -> float:
        """0.0-1.0 through the track, or 0.0 when nothing is playing.

        A track of unknown length (a corrupt or streaming file) reports 0.0
        rather than raising: progress is display data, and a missing artwork or
        duration must not stop the UI (SAPRS 6.7, 11.9).
        """

        total = self.duration.total_seconds()
        if total <= 0:
            return 0.0
        return min(1.0, max(0.0, self.position.total_seconds() / total))

    @property
    def is_audible(self) -> bool:
        """True when sound should be coming out of the box right now."""

        return self.state is PlaybackState.PLAYING
