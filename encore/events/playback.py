"""Playback facts (SAPRS 7.3-7.6, 8.7, AIG 8)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from encore.domain.identifiers import QueueItemId, SongId
from encore.domain.playback import PlaybackState
from encore.events.base import Event

__all__ = ["FinishedReason", "PlaybackRecovered", "SongFinished", "SongStarted"]


class FinishedReason(StrEnum):
    """Why a track stopped making sound (SAPRS 8.7, 8.8).

    The distinction is not decorative. Statistics count `COMPLETED` as a play;
    `SKIPPED` is the signal that a party did not want what was queued, and
    `FAILED` means the box could not play a file it had promised to (SAPRS 11.9).
    """

    COMPLETED = "completed"
    SKIPPED = "skipped"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class SongStarted(Event):
    """A track began producing sound (SAPRS 7.3, 8.7).

    Published when playback actually starts, not when it was asked for. The
    interval between the two is `PlaybackState.LOADING`, and conflating them is
    how a UI ends up showing a song that never arrived.
    """

    song_id: SongId
    queue_item_id: QueueItemId


@dataclass(frozen=True, slots=True, kw_only=True)
class SongFinished(Event):
    """A track stopped producing sound, for a stated reason (SAPRS 8.7, 8.9)."""

    song_id: SongId
    queue_item_id: QueueItemId
    reason: FinishedReason

    def __post_init__(self) -> None:
        Event.__post_init__(self)
        # Coerced rather than trusted. `reason` arrives from a repository row or an
        # API body as easily as from a Python constant, and a plain `"completed"` -
        # equal to the member, not an instance of it - would make `counts_as_played`
        # read the wrong way. The kind of error that surfaces as a statistics
        # discrepancy weeks later, so an unrecognised value raises here instead.
        if type(self.reason) is not FinishedReason:
            object.__setattr__(self, "reason", FinishedReason(self.reason))

    @property
    def counts_as_played(self) -> bool:
        """Whether statistics should record this as a listen (SAPRS 8 statistics)."""

        return self.reason is FinishedReason.COMPLETED


@dataclass(frozen=True, slots=True, kw_only=True)
class PlaybackRecovered(Event):
    """mpv came back after an unexpected exit (SAPRS 7.6).

    Attributes:
        reason: What the supervisor believed had happened, for the operator.
        restart_count: Restarts since the process started. A box that recovered
            once is fine; a box whose count climbs across a party is flapping,
            and `docs/Administrator-Guide.md` says that is the number to watch.
        resumed_state: Where playback landed - `IDLE` if the queue is empty,
            `PLAYING` if it did not miss a beat (SAPRS 7.6 step 6).
    """

    reason: str
    restart_count: int
    resumed_state: PlaybackState = PlaybackState.IDLE

    def __post_init__(self) -> None:
        Event.__post_init__(self)
        if self.restart_count < 1:
            raise ValueError(f"restart_count counts restarts, got {self.restart_count}")
        if not self.reason.strip():
            raise ValueError("PlaybackRecovered needs a reason an operator can act on")
