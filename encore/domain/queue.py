"""Queue item (SAPRS 4.5, Chapter 8).

One request to play one song. Two items may reference the same song and both
stand (SAPRS 8.3): `QueueItem` has exactly one song slot and nothing on it
compares items to one another, so deduplication is not expressible here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from encore.domain.identifiers import QueueItemId, SongId
from encore.utilities.clock import ensure_aware, utc_now

__all__ = ["QueueItem", "QueueItemStatus"]


class QueueItemStatus(StrEnum):
    """Where one item stands in its own lifetime (SAPRS 4.5).

    Not the same thing as `PlaybackState`, which describes the audio engine.
    An item that was never started is still `REMOVED` once an administrator
    takes it out (SAPRS 8.8), and `SKIPPED` if playback passed it by.
    """

    PENDING = "pending"
    PLAYING = "playing"
    FINISHED = "finished"
    SKIPPED = "skipped"
    REMOVED = "removed"


#: Statuses that still occupy a position in the FIFO.
ACTIVE_STATUSES: frozenset[QueueItemStatus] = frozenset(
    {QueueItemStatus.PENDING, QueueItemStatus.PLAYING}
)


@dataclass(frozen=True, slots=True, kw_only=True)
class QueueItem:
    """One request to play a song.

    Attributes:
        id: Unique queue identifier (SAPRS 4.5). Distinct from `song_id`
            precisely because the same song may be requested twice.
        song_id: What to play.
        position: 1-based ordinal in the FIFO at the time of observation
            (SAPRS 8.5). Derived from enqueue order, never from completion
            order.
        status: Lifetime state of this item.
        enqueued_at: When the request arrived. Guests are anonymous
            (ADR-007), so this is the only fact about the request beyond what
            to play: there is no requester field, and adding one would break
            SAPRS 1.3.
        played_at: When this item started playing, once it did. SAPRS 4.5 lists it
            and the queue table stores it, which is what lets the admin interface
            answer "how long has this one been on" without a history read.
    """

    id: QueueItemId
    song_id: SongId
    position: int
    status: QueueItemStatus = QueueItemStatus.PENDING
    enqueued_at: datetime = field(default_factory=utc_now)
    played_at: datetime | None = None

    def __post_init__(self) -> None:
        ensure_aware(self.enqueued_at, field_name="enqueued_at")
        if self.played_at is not None:
            ensure_aware(self.played_at, field_name="played_at")
        if self.position < 1:
            raise ValueError(f"position is 1-based, got {self.position}")

    @property
    def is_active(self) -> bool:
        """True while the item still occupies a queue position."""

        return self.status in ACTIVE_STATUSES
