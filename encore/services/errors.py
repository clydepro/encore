"""Queue failures, in the words a guest or a host needs (AEP 15, SAPRS 8.4, 10.7).

Two types, because there are exactly two ways a queue request can be refused, and the
difference is what the interface has to say about it:

* the queue is *full* — try later, and the number is worth showing (SAPRS 8.4 requires the
  maximum to be explicit and user-visible);
* the song is *not there* — try something else, because waiting will not help.

Milestone 11 maps these to 409 and 404 respectively (SAPRS 10.7); they are separate types
so that mapping is a lookup rather than a guess from a message string.
"""

from __future__ import annotations

__all__ = ["QueueError", "QueueFullError", "SongNotAvailableError"]


class QueueError(RuntimeError):
    """Base for a queue request Encore will not carry out."""


class QueueFullError(QueueError):
    """The queue has reached `queue.max_items` (SAPRS 8.4).

    Attributes:
        limit: The configured ceiling, so the refusal can name it. SAPRS 8.4 permits a
            maximum only if it is "explicit and user-visible", and a refusal that cannot
            say what the limit was fails that condition.

    The limit exists to bound a screen and a night, not to ration requests: 25-50 items is
    the normal queue the specification describes, and the default of 200 is above every
    party profile in `tests/party_simulation/profiles/`.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(f"the queue is full at {limit} items; try again when one ends")
        self.limit = limit


class SongNotAvailableError(QueueError):
    """The library has no such song (SAPRS 4.4, 14.10).

    Raised on the way in, never on the way out: a song that was queued and has since left
    the library (a rebuild between the two, ADR-009's known consequence) is skipped by the
    queue rather than refused, because the request was legitimate when it arrived.
    """

    def __init__(self, song_id: object) -> None:
        super().__init__(f"song {song_id} is not in the library")
        self.song_id = song_id
