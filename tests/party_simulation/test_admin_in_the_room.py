"""An operator in the room while the room is busy (SAPRS 14.11, AIG 14, AIG 18).

`test_queue_and_playback.py` established the shape of a party and what conservation means at
that scale; it also noted, honestly, that SAPRS 14.11 asks for guests queueing songs "while an
administrator presses things" and that milestone 14 was not built yet. It is now, so the
administrator in this file presses the things the administrative interface actually presses —
`remove`, `clear`, `skip`, `pause`, `resume` — through the same `QueueService` the HTTP routes
call, on the same rig, at the rates from `profiles/baseline.toml`.

The queue's methods rather than HTTP for one reason and not for another: the HTTP layer over a
party would measure the thread hop, and ADR-012 makes that hop a serialization point, so a
hundred concurrent admin requests would all be *correct* by construction and the interesting
failures would be invisible. What is worth finding is what an operator can do to a queue that
is being written by forty guests at the same time: a removal that reorders what is left, a clear
that silences the room, a skip that plays the same song twice, a ceiling that an
administratively-added item walks past. Those are all properties of the rules, and the rules are
below HTTP.

Three of the assertions are worth naming before they are read:

* **`clear` leaves the room audible.** The one administrative action with no undo has to stop
  *requests*, not music — a queue-clear that silenced the speakers mid-song would turn a tidy-up
  into an apology.
* **A removal cannot reorder what is left.** FIFO is the queue's only promise to a guest
  (SAPRS 8.1), and an operator taking one request out of the middle is the most likely way that
  promise breaks at scale.
* **The ceiling holds against everyone.** `queue.max_items` is a policy about the room, not about
  guests; an administrative path that could exceed it would be a second, quieter way to fill the
  queue.

Run with `--run-slow`.
"""

from __future__ import annotations

import random

import pytest

from encore.domain import PlaybackState, QueueItemId, QueueItemStatus, Song
from encore.repositories.library import LibraryStore
from encore.repositories.runtime import RuntimeStore
from encore.search import SearchService
from tests.party_simulation.test_queue_and_playback import Clock, Party
from tests.support.party_profiles import default_profile

pytestmark = [pytest.mark.slow, pytest.mark.party_simulation]

#: Guest-minutes per real second, as in the queue simulation. An hour of party in a second of
#: test finds the same ordering faults, because the faults are in the rules.
MINUTES = 6

#: What the operator does, and how often, in requests rather than seconds. An operator at a party
#: is not pressing a button every minute — they are noticing something once or twice an hour, and
#: a simulation that had them clear the queue forty times would be testing a different job.
SKIP_EVERY = 97
REMOVE_EVERY = 41
PAUSE_EVERY = 211
CLEAR_AT = 300


class Operator:
    """The administrative interface, reduced to the calls its routes make.

    One object so that "what the operator did" is a list a test can read afterwards — the
    conservation ledger in `Party` counts guests, and an operator's actions have to be counted
    separately or the sum will not close.

    Args:
        party: The appliance, with its queue and its clock.
        rng: The party's seeded stream. Shared with the guests on purpose: an operator who acts
            on a different random sequence than the room is an operator who never interferes
            with it in the same way twice.
    """

    def __init__(self, party: Party, rng: random.Random) -> None:
        self.party = party
        self.rng = rng
        self.skips = 0
        self.removals = 0
        self.skip_removals = 0
        self.failed_removals = 0
        self.clears = 0
        self.cleared = 0
        self.pauses = 0
        self.resumes = 0

    def every(self, count: int) -> None:
        """One more guest request happened; possibly, this is when the operator acts."""

        if count % CLEAR_AT == 0:
            # The number the button reports, not the number of times it was pressed: an
            # operator who cleared an empty queue did nothing, and the ledger has to agree.
            self.cleared += self.party.queue.clear()
        if count % SKIP_EVERY == 0:
            self.skips += 1
            self.party.queue.skip()
        if count % REMOVE_EVERY == 0:
            self.remove_one()
        if count % PAUSE_EVERY == 0 and self.party.queue.pause():
            # The pause is the interesting half: an operator pausing a paused appliance
            # gets `False`, and counting attempts would report a pause that never happened.
            self.pauses += 1
            self.party.queue.resume()
            self.resumes += 1

    def remove_one(self) -> None:
        """Take one request out, by its own id, the way the page does.

        The row is chosen from everything the queue holds, including the item on the speakers:
        the queue page draws a remove button beside Now Playing too, and an operator who clicks it
        gets a skip rather than a silence (SAPRS 8.8). Choosing only from the waiting rows would
        leave the more interesting half of `QueueService.remove` — the half that has to publish a
        fact instead of an UPDATE — untested at party scale.
        """

        rows = self.party.queue.active()
        if not rows:
            self.failed_removals += 1
            return
        victim = rows[self.rng.randrange(len(rows))]
        if not self.party.queue.remove(QueueItemId(victim.id)):
            self.failed_removals += 1
            return
        # `remove` reports the same `True` for two different events, and the ledger has to
        # tell them apart: taking out a waiting request ends an item without a `SongFinished`,
        # while taking out the *playing* one is a skip, which ends it with one (SAPRS 8.8).
        # Count them the same way and the conservation sum below double-counts a row.
        if victim.status is QueueItemStatus.PLAYING:
            self.skip_removals += 1
        else:
            self.removals += 1


def run_party(
    party: Party,
    songs: list[Song],
    *,
    guests: int,
    adds_per_guest_per_minute: int,
    seed: int = 2_027,
) -> Operator:
    """`MINUTES` of a party, with an operator walking past it.

    Returns:
        The operator, so the test can say how much operator there was.
    """

    rng = random.Random(seed)  # noqa: S311 - a seeded stream for reproducibility, not security
    operator = Operator(party, rng)
    second = 0
    for _minute in range(MINUTES):
        for _ in range(guests):
            for _ in range(adds_per_guest_per_minute):
                party.enqueue(songs[rng.randrange(len(songs))].id)
                second += 1
                operator.every(second)
                for step in range(4):
                    party.tick()
                    # A track in this simulation is eight requests long. `MockMpv` ends a song
                    # only when it is told to, so this line — not a clock — is what decides
                    # whether the queue has anything in it for the operator to work on.
                    party.mpv.properties["eof-reached"] = step == 3 and second % 8 == 0
    return operator


def drain(party: Party) -> None:
    """Let the room finish what is left, so the conservation sum can close."""

    for _ in range(party.queue.length + 4):
        party.tick()
        party.mpv.properties["eof-reached"] = True


@pytest.fixture
def corpus(library_store: LibraryStore) -> list[Song]:
    search = SearchService(index=library_store.search, catalogue=library_store, page_size=200)
    songs = [hit.song for hit in search.songs('"track"*')]
    assert songs, "a party with nothing to ask for proves nothing"
    return songs


# -- the ledger ------------------------------------------------------------


def test_an_operator_losing_requests_loses_nothing_else(
    library_store: LibraryStore, runtime_store: RuntimeStore, corpus: list[Song]
) -> None:
    """Every request ends up in exactly one column.

    `requests = refused + outcomes + cleared + waiting`, where `outcomes` counts the songs that
    finished (however they finished) and `cleared` counts the rows that were taken out by hand or
    by a clear. The arithmetic is the assertion: an item that vanished twice, or played and was
    also removed, breaks the sum, and there is no other way to notice at this scale.
    """

    profile = default_profile()
    party = Party(library_store, runtime_store, max_items=400, clock=Clock())
    try:
        operator = run_party(
            party,
            corpus,
            guests=profile.guests,
            adds_per_guest_per_minute=profile.queue_adds_per_guest_per_minute,
        )
        drain(party)
        ledger = party.ledger

        assert ledger.requests == profile.guests * profile.queue_adds_per_guest_per_minute * MINUTES
        assert operator.removals, "no operator, no test"
        assert operator.cleared
        assert sum(ledger.outcomes.values()) + operator.removals + operator.cleared == (
            ledger.requests - ledger.refused
        ), "requests went somewhere the ledger cannot see"
        assert party.queue.length == 0, "the room emptied and the queue emptied with it"
        assert party.playback.state is PlaybackState.IDLE
    finally:
        party.close()


def test_clearing_the_queue_does_not_silence_the_room(
    library_store: LibraryStore, runtime_store: RuntimeStore, corpus: list[Song]
) -> None:
    """The action with no undo, and the one property that makes it safe to offer.

    `clear()` returns the number of *waiting* items it removed, and the track on the speakers is
    not one of them (SAPRS 8.4). A simulation rather than a unit test because the property is
    about timing: an operator who clears during a song ending would see a silence, and only a
    run that has both happening finds the difference between "the current item" and "the item
    that just finished".
    """

    party = Party(library_store, runtime_store, max_items=400, clock=Clock())
    try:
        # One song on the speakers and a room behind it. The double reports
        # `eof-reached` only when this file says so, which is what lets a queue build up
        # without a party: the track that is playing has not ended, and 59 requests are
        # waiting for it to.
        party.enqueue(corpus[0].id)
        party.tick()
        for index in range(1, 60):
            party.enqueue(corpus[index % len(corpus)].id)

        playing_before = party.queue.now_playing()
        assert playing_before is not None, (
            "something has to be on the speakers for this to mean anything"
        )

        cleared = party.queue.clear()

        assert cleared > 0, "there was a queue to clear"
        assert party.queue.length == 1, "what is playing is not 'waiting'"
        assert party.playback.state is not PlaybackState.IDLE, "the room is still audible"
        now = party.queue.now_playing()
        assert now is not None
        assert now.song == playing_before.song
    finally:
        party.close()


def test_an_operator_picking_things_out_of_the_middle_keeps_the_line(
    library_store: LibraryStore, runtime_store: RuntimeStore, corpus: list[Song]
) -> None:
    """FIFO, with holes in it.

    What is played must still be a subsequence of what arrived, in arrival order — the same test
    `test_queue_and_playback.py` runs on a room with nobody managing it, run here with an
    operator deleting rows at random. That is the version worth having: a removal that shifted
    the remaining items into a different order would leave the queue *length* correct, the ledger
    balanced, and the party hearing songs in an order nobody asked for.
    """

    profile = default_profile()
    party = Party(library_store, runtime_store, max_items=400, clock=Clock())
    try:
        run_party(
            party,
            corpus,
            guests=profile.guests,
            adds_per_guest_per_minute=profile.queue_adds_per_guest_per_minute,
        )
        drain(party)
        played = [song_id for song_id in party.ledger.order if song_id in party.ledger.played]
        assert played, "nothing played, so the assertion below would be vacuous"

        position = 0
        for song_id in played:
            found = party.ledger.arrivals.index(song_id, position)
            position = found + 1

        assert party.ledger.played, "the ledger has names, not only positions"
    finally:
        party.close()


def test_the_ceiling_holds_when_an_operator_is_the_one_filling_it(
    library_store: LibraryStore, runtime_store: RuntimeStore, corpus: list[Song]
) -> None:
    """`queue.max_items` is a rule about the room, not about guests.

    The administrative surface offers `play_next`, which starts something immediately when the
    appliance is idle — the one action that could walk past the ceiling, because it does not
    arrive as a request. It is checked here rather than asserted in a unit test because the
    property is "at no point in a party did the queue exceed its limit", which is a statement
    about a run.
    """

    ceiling = 24
    party = Party(library_store, runtime_store, max_items=ceiling, clock=Clock())
    widest = 0
    try:
        for index in range(ceiling * 3):
            party.enqueue(corpus[index % len(corpus)].id)
            widest = max(widest, party.queue.length)
            if index % 5 == 0:
                party.tick()
                party.mpv.properties["eof-reached"] = True
            if index % 13 == 0:
                party.queue.play_next()
                widest = max(widest, party.queue.length)

        assert party.ledger.refused > 0, "the ceiling was reached, or the test proved nothing"
        assert widest <= ceiling, f"the queue held {widest} items at a ceiling of {ceiling}"
    finally:
        party.close()


def test_a_paused_room_still_takes_requests(
    library_store: LibraryStore, runtime_store: RuntimeStore, corpus: list[Song]
) -> None:
    """Pause is not stop, and the difference is what a guest notices.

    A paused appliance keeps accepting requests and keeps its FIFO; a stopped one returns the
    current item to the head. The assertion that matters at party scale is that nothing started
    *while* the room was paused — a tick that advanced playback under a pause would be a song
    nobody heard, and the ledger would count it as played.
    """

    party = Party(library_store, runtime_store, max_items=400, clock=Clock())
    try:
        party.enqueue(corpus[0].id)
        party.tick()
        assert party.queue.pause() is True
        held = sum(party.ledger.outcomes.values())
        loaded = party.mpv.calls.count("loadfile") if hasattr(party.mpv, "calls") else 0

        for index in range(1, 8):
            party.enqueue(corpus[index % len(corpus)].id)
            for _ in range(3):
                party.tick()

        assert sum(party.ledger.outcomes.values()) == held, "a paused track advanced anyway"
        paused = party.playback.state
        assert paused is PlaybackState.PAUSED
        assert party.queue.length == 8, "the room kept asking, and the queue kept taking"
        assert party.queue.up_next(limit=10), "and the requests are in the list"

        party.queue.resume()
        resumed = party.playback.state
        assert resumed is PlaybackState.PLAYING
        for _ in range(party.queue.length + 2):
            party.tick()
            party.mpv.properties["eof-reached"] = True

        assert party.queue.length == 0
        assert sum(party.ledger.outcomes.values()) > held, "and then the room heard all of it"
        del loaded
    finally:
        party.close()
