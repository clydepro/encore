"""Service failures, in the words a guest or a host needs (AEP 15, SAPRS 8.4, 10.7).

Two families live here: the queue's, which a guest can cause, and the administrative ones,
which an operator can. They share a module because milestone 11's error table maps
*exception type* to status and code in one place — a mapping duplicated per package is a
mapping that disagrees somewhere — and because both families exist so that no route has to
invent its own wording for a refusal.

Two queue types, because there are exactly two ways a queue request can be refused, and the
difference is what the interface has to say about it:

* the queue is *full* — try later, and the number is worth showing (SAPRS 8.4 requires the
  maximum to be explicit and user-visible);
* the song is *not there* — try something else, because waiting will not help.

SAPRS 10.7 maps these to 409 and 404 respectively; they are separate types so that mapping
is a lookup rather than a guess from a message string.
"""

from __future__ import annotations

__all__ = [
    "AdminError",
    "AuthenticationError",
    "CredentialExistsError",
    "CrossSiteError",
    "QueueError",
    "QueueFullError",
    "RateLimitError",
    "SongNotAvailableError",
    "WeakCredentialError",
]


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


class AdminError(RuntimeError):
    """Base for an administrative request Encore will not carry out (SAPRS 10.8).

    Separate from `QueueError` because the two families answer different questions: a queue
    refusal is a guest being told something about the party, and an administrative one is
    the appliance deciding something about the operator. SAPRS 10.7 lists their status codes
    in the same sentence, and the error table maps them from one place either way.
    """


class AuthenticationError(AdminError):
    """No valid session, or the password was wrong (SAPRS 10.7's 401).

    One type for both, deliberately: "you are not signed in" and "that is not the
    password" are the same answer to anyone who is not already the administrator, and the
    interface that distinguishes them is the one giving a guesser a signal. A *message*
    may say more — this module's messages are for the operator's own screen, and
    `encore/api/errors.py` decides what crosses the wire.
    """


class RateLimitError(AdminError):
    """The failure window is full (SAPRS 10.7's 429, ADR-013's global limiter).

    Attributes:
        retry_after_seconds: How long the appliance will refuse to answer. Counted for the
            appliance rather than per client, because Encore keeps no list of clients
            (ADR-007) and a per-address window is a table of who connected.
    """

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            f"too many failed sign-in attempts; try again in {retry_after_seconds} seconds"
        )
        self.retry_after_seconds = retry_after_seconds


class CrossSiteError(AdminError):
    """A state-changing request whose `Origin` is another host (SAPRS 10.7's 403).

    The cookie is `SameSite=Strict`, so a browser should never send it here; this is the
    second look rather than the first, and it is 403 rather than 401 because the caller is
    authenticated and still not permitted — the two answers mean different things to an
    operator, and SAPRS 10.7 lists both.
    """


class WeakCredentialError(AdminError):
    """The proposed password is too short to type on a shared network (SAPRS 12.3)."""


class CredentialExistsError(AdminError):
    """First-run setup was offered a box that already has a credential.

    409 rather than 401 because the sentence the operator needs is "someone already owns
    this appliance", which is a state conflict and not an authentication failure
    (ADR-013's first-run window).
    """
