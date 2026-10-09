"""Playback facts (SAPRS 7.3-7.6, 8.7, AIG 8)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from encore.domain.identifiers import QueueItemId, SongId
from encore.domain.playback import PlaybackOutcome, PlaybackState
from encore.events.base import Event

__all__ = [
    "COMPLETION_TOLERANCE",
    "FinishedReason",
    "PlaybackRecovered",
    "SongFinished",
    "SongStarted",
]

#: The widest `completion` accepted. 1.0 is "heard all of it"; the half above is
#: tolerance for a ratio arriving from a repository row or an API body, where a value of
#: 1.4 means somebody did not clamp it. The playback service clamps to 1.0 on the way
#: out, so anything past this bound did not come from the engine and is a bug worth a
#: stack trace rather than a statistic worth quietly rounding.
COMPLETION_TOLERANCE = 1.5


class FinishedReason(StrEnum):
    """Why a track stopped making sound (SAPRS 8.7, 8.8).

    The distinction is not decorative. Statistics count `COMPLETED` as a play;
    `SKIPPED` is the signal that a party did not want what was queued, and
    `FAILED` means the box could not play a file it had promised to (SAPRS 11.9).

    The members are `PlaybackOutcome`'s, and for a reason: one enum describes the
    fact on the bus and the other the row in `playback_history`, and a party's play
    log that disagreed with its own events would be unexplainable. The five values
    match by construction and `outcome` is the mapping, asserted in
    `tests/unit/test_events.py` rather than by inspection.
    """

    COMPLETED = "completed"
    SKIPPED = "skipped"
    STOPPED = "stopped"
    FAILED = "failed"

    @property
    def outcome(self) -> PlaybackOutcome:
        """The same fact as it is stored in history (SAPRS 5.7).

        A property rather than a parallel attribute because there is exactly one
        honest way to spell it: by value, which cannot drift from the row.
        """

        return PlaybackOutcome(self.value)

    @property
    def counts_as_played(self) -> bool:
        """Whether statistics should record this as a listen (SAPRS 8 statistics)."""

        return self.outcome.counts_as_played


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
    """A track stopped producing sound, for a stated reason (SAPRS 8.7, 8.9).

    Attributes:
        completion: How much of the track the listener heard, as a ratio (1.0 = to
            the end). It is here because `playback_history.completion` is (SAPRS 5.7)
            and only the engine knows it: by the time the queue is told to advance,
            the position it would have to ask for has already been reset. A value
            above 1.0 is refused rather than rounded — mpv reports a position past a
            truncated file's declared length, and a 120%-played statistic is the kind
            of number a host uses to decide the library is wrong.
    """

    song_id: SongId
    queue_item_id: QueueItemId
    reason: FinishedReason
    completion: float = 0.0

    def __post_init__(self) -> None:
        Event.__post_init__(self)
        # Coerced rather than trusted. `reason` arrives from a repository row or an
        # API body as easily as from a Python constant, and a plain `"completed"` -
        # equal to the member, not an instance of it - would make `counts_as_played`
        # read the wrong way. The kind of error that surfaces as a statistics
        # discrepancy weeks later, so an unrecognised value raises here instead.
        if type(self.reason) is not FinishedReason:
            object.__setattr__(self, "reason", FinishedReason(self.reason))
        if not 0.0 <= self.completion <= COMPLETION_TOLERANCE:
            raise ValueError(f"completion is a ratio of the track played, got {self.completion}")

    @property
    def counts_as_played(self) -> bool:
        """Whether statistics should record this as a listen (SAPRS 8 statistics)."""

        return self.reason.counts_as_played


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
