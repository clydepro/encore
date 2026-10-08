"""Queue facts (SAPRS 8.9, AIG 8)."""

from __future__ import annotations

from dataclasses import dataclass

from encore.domain.identifiers import QueueItemId, SongId
from encore.events.base import Event

__all__ = ["QueueAdvanced", "SongQueued"]


@dataclass(frozen=True, slots=True, kw_only=True)
class SongQueued(Event):
    """A guest asked for a song and the request was accepted (SAPRS 8.2).

    Attributes:
        song_id: What was requested.
        queue_item_id: The entry that now exists. Separate from `song_id` because
            two guests may create two entries for one song (SAPRS 8.3).
        position: 1-based place in the FIFO.
        queue_length: Entries in the queue after this one was added - the number
            "Up Next" renders (SAPRS 9.9).

    There is no requester field. Guests are anonymous (ADR-007, SAPRS 1.3), and
    anything that told two guests apart would end the party-game premise even if
    no screen ever displayed it.
    """

    song_id: SongId
    queue_item_id: QueueItemId
    position: int
    queue_length: int

    def __post_init__(self) -> None:
        Event.__post_init__(self)
        if self.position < 1:
            raise ValueError(f"position is 1-based, got {self.position}")
        if self.queue_length < self.position:
            raise ValueError(
                f"the queue cannot be shorter ({self.queue_length}) than the position it "
                f"reports ({self.position})"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class QueueAdvanced(Event):
    """The FIFO moved on (SAPRS 8.7, 8.9).

    Published whether the move was a completion, a skip or an administrative
    removal; those are told apart by which item left, not by a different type.

    Attributes:
        finished_queue_item_id: The entry that stopped being current, if any.
        now_playing: The song the queue selected next, or None when the queue
            emptied (SAPRS 8.9 "queue emptied"). Announcing that playback of it
            actually began is `SongStarted`'s job (SAPRS 8.7), so silence is
            reported here and sound is reported there - the interface needs both
            facts and they are not the same fact.
        queue_length: Entries remaining.
    """

    finished_queue_item_id: QueueItemId | None = None
    now_playing: SongId | None = None
    queue_length: int = 0

    def __post_init__(self) -> None:
        Event.__post_init__(self)
        if self.queue_length < 0:
            raise ValueError(f"queue_length cannot be negative, got {self.queue_length}")
