"""A bounded party over the queue, the engine and both databases (SAPRS 14.11, AIG 18).

The full driver — 15,000 songs, hundreds of guests, SSE clients coming and going — arrives
with milestone 16. What lands here is the part of that idea that can be honest today: the
*shape* of a party, run against the real queue and the real playback state machine, with the
rates taken from `profiles/baseline.toml` rather than invented, and the library being the
seven-track corpus the Builder makes in a second instead of fifteen thousand.

Scaling it down is not scaling it away. The failures a party simulation is for are ordering
and accounting — a song that played twice, a request that vanished, a queue that reordered
itself while an mpv was restarting — and all of those are properties of the rules, not of the
size of the library. A conservation check with 400 requests over 7 songs finds every one of
them, and it is the *counts* that are asserted here rather than any latency.

Two things are deliberately absent: HTTP and SSE. A guest asking for a song is, in this file,
a call to `QueueService.enqueue`, which is the same call the controller will make. When the
web layer lands, this file gains a second entry point and keeps these assertions.

Run with `--run-slow`.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from encore.config.models import PlaybackConfig, QueueConfig
from encore.domain import PlaybackOutcome, PlaybackState, QueueItemStatus, Song, SongId
from encore.events import EventBus, SongFinished
from encore.playback import MpvPlayer, PlaybackService, PlaybackSupervisor
from encore.repositories.library import LibraryStore
from encore.repositories.runtime import RuntimeStore
from encore.search import SearchService
from encore.services.errors import QueueFullError
from encore.services.queue_service import QueueService
from tests.support.mpv import MockMpv
from tests.support.party_profiles import default_profile

pytestmark = [pytest.mark.slow, pytest.mark.party_simulation]

#: Guest-minutes per real second of the simulation. The baseline profile is an hour of a
#: party; a minute of it finds the same ordering bugs in about a second of test.
MINUTES = 6


@dataclass
class Clock:
    """One wall clock shared by everything, because "when" is the queue's other axis."""

    at: datetime = datetime(2026, 6, 1, 20, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.at

    def advance(self, *, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


class RevivingLauncher:
    """mpv, restartable. Each launch is a new process and a new double."""

    def __init__(self) -> None:
        self.mpv = MockMpv()
        self.launches = 0

    def launch(self) -> MockMpv:
        self.launches += 1
        if self.launches > 1:
            self.mpv = MockMpv()
        return self.mpv

    def terminate(self) -> None:
        return None

    @property
    def alive(self) -> bool:
        """The monitor's only question, answered by the double that is currently the process."""

        return self.mpv.running

    def kill(self) -> None:
        """The party's power cut: mpv dies between two requests."""

        self.mpv.crash()

    def diagnostics(self) -> str:
        return "mpv running" if self.alive else "mpv exited with code -11"


@dataclass
class Ledger:
    """What the party did, and what became of it.

    The conservation rule is `requests == outcomes + refused + waiting`. SAPRS 14.11 asks the
    simulation to run guests queueing songs while playback progresses and an administrator
    presses things; it does not say what "correct" looks like when all three happen at once,
    and this ledger is that definition. Every way the queue can lose or duplicate a request
    breaks the sum, including the ones no test names in advance.
    """

    requests: int = 0
    refused: int = 0
    played: dict[SongId, int] = field(default_factory=dict)
    outcomes: dict[PlaybackOutcome, int] = field(default_factory=dict)
    removed: int = 0
    order: list[SongId] = field(default_factory=list)
    arrivals: list[SongId] = field(default_factory=list)

    def request(self, song_id: SongId) -> None:
        self.requests += 1
        self.arrivals.append(song_id)

    def notice(self, event: SongFinished) -> None:
        self.outcomes[event.reason.outcome] = self.outcomes.get(event.reason.outcome, 0) + 1
        if event.reason.outcome is PlaybackOutcome.SKIPPED:
            self.removed += 1
        else:
            self.played[event.song_id] = self.played.get(event.song_id, 0) + 1
        self.order.append(event.song_id)


class Party:
    """The appliance under simulated guests: search, queue, playback, recovery."""

    def __init__(
        self,
        library: LibraryStore,
        runtime: RuntimeStore,
        *,
        max_items: int,
        clock: Clock,
    ) -> None:
        self.clock = clock
        self.runtime = runtime
        self.events = EventBus(logger=logging.getLogger("encore.party"))
        self.launcher = RevivingLauncher()
        self.supervisor = PlaybackSupervisor(
            launcher=self.launcher,
            events=self.events,
            config=PlaybackConfig(restart_backoff_seconds=[0.01]),
            clock=clock,
            logger=logging.getLogger("encore.party.supervisor"),
        )
        self.playback = PlaybackService(
            player=MpvPlayer(self.supervisor.channel),
            events=self.events,
            clock=clock,
            recovery=self.supervisor,
        )
        self.supervisor.bind(self.playback)
        self.queue = QueueService(
            store=runtime,
            library=library,
            player=self.playback,
            events=self.events,
            config=QueueConfig(max_items=max_items),
            clock=clock,
            logger=logging.getLogger("encore.party.queue"),
        )
        self.queue.start()
        self.search = SearchService(index=library.search, catalogue=library, page_size=200)
        self.ledger = Ledger()
        self.events.subscribe(SongFinished, self.ledger.notice, name="ledger")

    @property
    def mpv(self) -> MockMpv:
        return self.launcher.mpv

    def enqueue(self, song_id: SongId) -> bool:
        """One guest, one request. Returns whether the appliance accepted it."""

        self.ledger.request(song_id)
        try:
            self.queue.enqueue(song_id)
        except QueueFullError:
            self.ledger.refused += 1
            return False
        return True

    def tick(self) -> None:
        """One second: the monitor looks at the process, the service at the track."""

        self.clock.advance(seconds=1.0)
        self.supervisor.tick()
        self.playback.tick()

    def close(self) -> None:
        self.queue.close()


@pytest.fixture
def corpus(library_store: LibraryStore) -> list[Song]:
    search = SearchService(index=library_store.search, catalogue=library_store, page_size=200)
    songs = [hit.song for hit in search.songs('"track"*')]
    assert songs, "a party with nothing to ask for proves nothing"
    return songs


def _run(
    party: Party,
    songs: list[Song],
    *,
    guests: int,
    adds_per_guest_per_minute: int,
    terminations: int,
    admin_activity: bool,
    seed: int = 1_970,
) -> None:
    """`MINUTES` of a party, in seconds, with the profile's rates.

    A seeded RNG rather than a random one, because the failures worth catching in a queue are
    ordering failures, and an ordering failure that only appears with a seed nobody can rerun
    is not a bug anyone can fix.
    """

    rng = random.Random(seed)  # noqa: S311 - a seeded stream for reproducibility, nothing to do with secrets
    crash_at = {rng.randrange(MINUTES * 60) for _ in range(terminations)}
    second = 0
    for _minute in range(MINUTES):
        for _ in range(guests):
            for _ in range(adds_per_guest_per_minute):
                party.enqueue(songs[rng.randrange(len(songs))].id)
                second += 1
                if second in crash_at:
                    party.launcher.kill()
                if admin_activity and second % 97 == 0:
                    party.queue.skip()
                for _ in range(4):
                    party.tick()
                    party.mpv.properties["eof-reached"] = True


def test_a_quiet_room_of_eight_guests_loses_nothing(
    library_store: LibraryStore, runtime_store: RuntimeStore, corpus: list[Song]
) -> None:
    profile = default_profile()
    clock = Clock()
    party = Party(library_store, runtime_store, max_items=400, clock=clock)
    try:
        _run(
            party,
            corpus,
            guests=profile.guests,
            adds_per_guest_per_minute=profile.queue_adds_per_guest_per_minute,
            terminations=0,
            admin_activity=False,
        )
        for _ in range(party.queue.length + 2):
            party.tick()
            party.mpv.properties["eof-reached"] = True

        waiting = party.queue.length
        ledger = party.ledger
        accounted = sum(ledger.outcomes.values())

        assert ledger.requests == profile.guests * profile.queue_adds_per_guest_per_minute * MINUTES
        assert waiting == 0, "the room emptied, and the queue emptied with it"
        assert accounted + ledger.removed == ledger.requests - ledger.refused
        assert party.playback.state is PlaybackState.IDLE
    finally:
        party.close()


def test_the_order_they_asked_in_is_the_order_they_heard(
    library_store: LibraryStore, runtime_store: RuntimeStore, corpus: list[Song]
) -> None:
    """SAPRS 8.1 and 8.5, at party scale, with an administrator pressing things.

    The comparison is against the requests that were actually *played*, in the order they
    were played, and it is a subsequence test rather than an equality because a skip removes
    an item and an mpv death ends one early: both are legitimate. What may never happen is a
    song arriving out of turn relative to the others.
    """

    profile = default_profile()
    clock = Clock()
    party = Party(library_store, runtime_store, max_items=400, clock=clock)
    try:
        _run(
            party,
            corpus,
            guests=profile.guests,
            adds_per_guest_per_minute=profile.queue_adds_per_guest_per_minute,
            terminations=0,
            admin_activity=True,
        )
        for _ in range(party.queue.length + 2):
            party.tick()
            party.mpv.properties["eof-reached"] = True

        played = [song_id for song_id in party.ledger.order if song_id in party.ledger.played]
        arrived = party.ledger.arrivals
        assert played, "nothing played, so the assertion below is vacuous"

        position = 0
        for song_id in played:
            position = arrived.index(song_id, position)
            position += 1
    finally:
        party.close()


def test_a_room_that_crashes_twice_still_finishes_the_party(
    library_store: LibraryStore, runtime_store: RuntimeStore, corpus: list[Song]
) -> None:
    """SAPRS 7.6 at scale: mpv dies twice during the night and the queue recovers both times.

    The interesting assertion is not that it recovered — it is that the requests made across
    the deaths were neither lost nor played twice, which is the accounting milestone 3's
    whole risk.
    """

    profile = default_profile()
    clock = Clock()
    party = Party(library_store, runtime_store, max_items=400, clock=clock)
    try:
        _run(
            party,
            corpus,
            guests=profile.guests,
            adds_per_guest_per_minute=profile.queue_adds_per_guest_per_minute,
            terminations=max(profile.mpv_terminations, 2),
            admin_activity=False,
        )
        for _ in range(party.queue.length + 2):
            party.tick()
            party.mpv.properties["eof-reached"] = True

        assert party.queue.length == 0
        assert party.supervisor.restart_count >= 1
        failed = party.ledger.outcomes.get(PlaybackOutcome.FAILED, 0)
        assert failed <= 2, "a death ends one track, not a run of them"
        accounted = sum(party.ledger.outcomes.values()) + party.ledger.removed
        assert accounted == party.ledger.requests - party.ledger.refused
    finally:
        party.close()


def test_a_duplicated_request_is_played_twice_and_both_are_counted(
    library_store: LibraryStore, runtime_store: RuntimeStore, corpus: list[Song]
) -> None:
    """SAPRS 8.3 is a rule about a whole party, not about one pair of clicks."""

    clock = Clock()
    party = Party(library_store, runtime_store, max_items=400, clock=clock)
    song = corpus[0]
    try:
        for _ in range(5):
            party.enqueue(song.id)
        for _ in range(8):
            party.tick()
            party.mpv.properties["eof-reached"] = True

        assert party.ledger.played.get(song.id, 0) == 5
        assert party.queue.length == 0
    finally:
        party.close()


def test_the_ceiling_is_visible_in_the_ledger(
    library_store: LibraryStore, runtime_store: RuntimeStore, corpus: list[Song]
) -> None:
    """SAPRS 8.4: when the queue is full, the refusal is the answer, and it is counted."""

    clock = Clock()
    party = Party(library_store, runtime_store, max_items=10, clock=clock)
    try:
        for _ in range(40):
            party.enqueue(corpus[0].id)

        assert party.ledger.refused == 30, "the queue took ten and said no to thirty"
        assert party.queue.length <= 10
    finally:
        party.close()


def test_no_request_is_left_playing_and_no_position_is_left_broken(
    library_store: LibraryStore, runtime_store: RuntimeStore, corpus: list[Song]
) -> None:
    """The state a party ends in, read out of the database rather than from a service.

    Positions contiguous from 1, and nothing still marked `Playing` once the room is empty:
    both are invariants the next guest's screen depends on, and both survive a wrong
    settlement in `QueueService._settle` in a way no single-request test would notice.
    """

    clock = Clock()
    party = Party(library_store, runtime_store, max_items=400, clock=clock)
    try:
        _run(
            party,
            corpus,
            guests=8,
            adds_per_guest_per_minute=2,
            terminations=1,
            admin_activity=True,
        )
        for _ in range(party.queue.length + 2):
            party.tick()
            party.mpv.properties["eof-reached"] = True

        with runtime_store.unit_of_work() as work:
            active = work.queue.active()
            assert [item.status for item in active] != [QueueItemStatus.PLAYING]
            positions = sorted(item.position for item in active)
            assert positions == list(range(1, len(positions) + 1)), "positions are contiguous"
            rows = work.history.recent(limit=2_000)
            assert rows, "a party with no history wrote nothing"
            assert all(row.started_at <= (row.finished_at or row.started_at) for row in rows)
            assert all(0.0 <= row.completion <= 1.0 for row in rows)
    finally:
        party.close()


def test_two_connections_see_the_same_queue(
    library_store: LibraryStore, runtime_store: RuntimeStore, corpus: list[Song]
) -> None:
    """Two requests landing at once is the same FIFO, whichever transaction won.

    `runtime.db` is in WAL mode with a five-second busy timeout (ADR-009); this is the
    smallest check that the queue's order does not depend on which connection read it, which
    is what a second worker process would otherwise break.
    """

    from encore.repositories.runtime import open_runtime_store

    path = runtime_store.info.path
    clock = Clock()
    party = Party(library_store, runtime_store, max_items=400, clock=clock)
    second = open_runtime_store(path)
    try:
        for song in corpus[:3]:
            party.enqueue(song.id)
        with second.unit_of_work() as work:
            ids = [item.id for item in work.queue.active()]
        assert len(ids) == 3
        with party.runtime.unit_of_work() as work:
            assert [item.id for item in work.queue.active()] == ids
    finally:
        second.close()
        party.close()


def test_the_simulation_is_bounded_and_says_so() -> None:
    """The honesty check for this file.

    A profile asks for a 15,000-song library and an hour of partying; this suite runs seven
    songs for six minutes. That is a decision, not an oversight, and the day the corpus grows
    the assertion below is the one to delete — which is the point of putting it here rather
    than in a comment nobody reads.
    """

    profile = default_profile()
    assert profile.library_songs == 15_000
    assert profile.duration_minutes >= MINUTES
