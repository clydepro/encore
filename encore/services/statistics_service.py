"""The Statistics Service: the counters, and the one honest view of them (AIG 7, SAPRS 5.7, 10.3).

The last name on AIG 7's list, and the reason it was last is worth stating: phase 3's
lesson, recorded in `ai/context/milestones.md`, is that a service written before its
caller exists quietly stops being true. Its caller is the dashboard, so it arrives with the
dashboard.

What already existed is better than it looks. `RuntimeStatisticsRepository` is an atomic
`ON CONFLICT DO UPDATE` counter keyed by `(name, day)`, and `PlaybackHistoryRepository` is
one row per track that ended. So this service is not a tally — it is the *policy* over two
tables that can disagree:

* **Which facts mean which counters.** A `SongFinished` is a listen, a skip, or a fault,
  and `PlaybackOutcome.counts_as_played` already decides which of those the enum believes
  (and `tests/regression/test_issue_23_search_playback_queue.py` pins that sentence to the
  counter). The mapping below is a table for the same reason `fragments.py` writes its
  region map as data: a reviewer can read the whole policy in one block and check it
  against SAPRS 10.3, and a fact that stops existing is caught by a test rather than by a
  discrepancy noticed in March.
* **Today and total, always together.** `increment_both` is one transaction; two calls is a
  crash waiting between them, and a "total" that is not the sum of its days is the sort of
  number a host repeats to a venue.
* **What is *not* counted here.** `PLAYLIST_PLAYS`, `PLAYLIST_BUILDS`, `PEER_QUERIES` and
  `PEER_JOINS` are v1.1 and v2 keys (SAPRS 16.3, 16.5) that exist so the vocabulary is
  written down once; the report prints only the keys v1 can fill, rather than four zeros
  that look like measurements. `busiest_hour()` is left to the party-simulation report it
  was written for: it loads a window of history, and a dashboard should not pay for a
  histogram nobody asked for.

**Why counting happens on the bus and in three named methods.** Everything a fact can reach
arrives through a subscription — plays, skips, faults, queue adds, recoveries — because
that is AIG 4's "services communicate through the Event Bus" applied to a service that must
not slow the thing it watches, and because a fact that needed a second publisher would be
two sources for one number. Three counters are invisible to the bus: a search, an artist
page and an album page are requests, not facts, so `searched()`, `artist_viewed()` and
`album_viewed()` exist and are the only way those three move. They are named methods rather
than `record(key)` so that no layer above this one has to import a repository enum to say
"count this" — `encore/search/service.py` declines to increment its own key for exactly this
reason.

**One write, one thread.** A counter is a `runtime.db` write, so every method here belongs
on the appliance thread (ADR-012) and the HTTP layer reaches it through `read()` like every
other service call. A bus handler runs inline in the publication, and every publisher in
this tree publishes *outside* its own `unit_of_work()`; a second write transaction nested
inside a first, from the same thread, is how WAL meets itself and loses. That invariant is
asserted by `tests/integration/test_admin_interface.py`, because nothing else in the code
says it out loud.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Protocol

from encore.domain import SongId
from encore.events import Event
from encore.events.bus import EventBus, Subscription
from encore.events.playback import FinishedReason, PlaybackRecovered, SongFinished
from encore.events.queue import SongQueued
from encore.repositories.runtime.statistics import StatisticKey
from encore.utilities.clock import Clock, SystemClock

__all__ = ["DEFAULT_TREND_DAYS", "FACT_COUNTERS", "StatisticsReport", "StatisticsService"]

#: Days of history the dashboard asks for. A week fits under a sparkline on a phone and
#: still answers "was Saturday busier", which is the only question an operator asks a chart.
DEFAULT_TREND_DAYS = 7

#: Which fact moves which counter. A fact whose key list is empty counts nothing, which is
#: a statement about the fact rather than an omission: `SongStarted` and `QueueAdvanced`
#: describe the same play that `SongFinished` records, and counting them too would be the
#: four-plays-per-song statistic that issue #23's regression test exists to prevent.
FACT_COUNTERS: Mapping[type[Event], tuple[StatisticKey, ...]] = MappingProxyType(
    {
        SongQueued: (StatisticKey.QUEUE_ADDED,),
        SongFinished: (StatisticKey.SONGS_PLAYED,),
        PlaybackRecovered: (StatisticKey.RECOVERIES,),
    }
)

#: The keys v1 can fill, in the order a screen prints them. Everything else in
#: `StatisticKey` belongs to a feature that has not been built (SAPRS 16.3, 16.5).
REPORTED: frozenset[StatisticKey] = frozenset(
    {
        StatisticKey.SONGS_PLAYED,
        StatisticKey.SEARCHES,
        StatisticKey.ARTIST_NAVIGATIONS,
        StatisticKey.ALBUM_NAVIGATIONS,
        StatisticKey.QUEUE_ADDED,
        StatisticKey.QUEUE_SKIPPED,
        StatisticKey.PLAYBACK_ERRORS,
        StatisticKey.RECOVERIES,
    }
)

_REPORTED_NAMES = frozenset(key.value for key in REPORTED)


class CounterSnapshot(Protocol):
    """`StatisticsSnapshot`, as far as a report needs it — a mapping of names to numbers."""

    def as_dict(self) -> dict[str, int]: ...


class StatisticsCounters(Protocol):
    """`RuntimeStatisticsRepository`, as far as this service needs it (ADR-009)."""

    def increment_both(self, key: StatisticKey, *, today: date, amount: int = 1) -> int: ...

    def snapshot(self, *, day: date | None = None) -> CounterSnapshot: ...

    def series(self, key: StatisticKey, *, days: Iterable[date]) -> Mapping[date, int]: ...


class StatisticsHistory(Protocol):
    """`PlaybackHistoryRepository`, as far as the report needs it."""

    def distinct_songs(self) -> int: ...

    def leaderboard(self, *, limit: int = 10) -> list[tuple[SongId, int]]: ...

    def counts_by_outcome(self) -> dict[str, int]: ...

    def total(self) -> int: ...


class StatisticsTransaction(Protocol):
    """One `runtime.db` transaction, as far as counting reaches it."""

    @property
    def statistics(self) -> StatisticsCounters: ...

    @property
    def history(self) -> StatisticsHistory: ...


class StatisticsStore(Protocol):
    """The transaction factory `RuntimeStore` satisfies (see `QueueStore` for the why)."""

    @contextmanager
    def unit_of_work(self) -> Iterator[StatisticsTransaction]: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class StatisticsReport:
    """Every number the dashboard prints, from one transaction.

    The two views of the same counters — `today` and `totals` — are both here rather than
    one of them, because "how busy was today" and "how busy is this box" are the two
    questions SAPRS 10.3 actually asks, and a report that could answer only one would be
    read as the other.
    """

    generated_at: datetime
    day: date
    today: Mapping[str, int]
    totals: Mapping[str, int]
    trend: tuple[tuple[date, int], ...] = ()
    distinct_songs: int = 0
    history_rows: int = 0
    outcomes: Mapping[str, int] = MappingProxyType({})
    leaderboard: tuple[tuple[SongId, int], ...] = ()

    @property
    def plays_today(self) -> int:
        return self.today.get(StatisticKey.SONGS_PLAYED.value, 0)

    @property
    def plays_total(self) -> int:
        return self.totals.get(StatisticKey.SONGS_PLAYED.value, 0)

    @property
    def errors(self) -> int:
        """Failed tracks ever. The number that turns "quiet" into "broken" (SAPRS 11.9)."""

        return self.totals.get(StatisticKey.PLAYBACK_ERRORS.value, 0)

    @property
    def busiest_day(self) -> tuple[date, int]:
        if not self.trend:
            return (self.day, 0)
        return max(self.trend, key=lambda item: item[1])

    def as_json(self) -> dict[str, object]:
        """The `/api/v1/stats` body, in the API's conventions (durations in ms elsewhere)."""

        return {
            "generated_at": self.generated_at,
            "day": self.day.isoformat(),
            "today": dict(self.today),
            "totals": dict(self.totals),
            "trend": [{"day": when.isoformat(), "plays": plays} for when, plays in self.trend],
            "distinct_songs": self.distinct_songs,
            "history_rows": self.history_rows,
            "outcomes": dict(self.outcomes),
            "leaderboard": [
                {"song_id": int(song), "plays": plays} for song, plays in self.leaderboard
            ],
        }


class StatisticsService:
    """Counts what the party did, and answers in one call.

    Args:
        store: `runtime.db`, as a transaction factory.
        events: The bus, subscribed to in `start()` and withdrawn in `close()` — the same
            lifecycle `QueueService` uses, so a stopped appliance has no handler left
            writing counters into a shutdown (SAPRS 11.8).
        clock: Time, because "today" is a decision and it must be made in one place.
        trend_days: How many days of history a report spans.
        leaderboard_size: How many most-played tracks to name.
        logger: For the one case worth reporting: a counter that could not be written,
            which is a statistics gap rather than a party failure (SAPRS 11.9).
    """

    def __init__(
        self,
        *,
        store: StatisticsStore,
        events: EventBus,
        clock: Clock | None = None,
        trend_days: int = DEFAULT_TREND_DAYS,
        leaderboard_size: int = 10,
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._events = events
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._trend_days = max(1, trend_days)
        self._leaderboard_size = max(1, leaderboard_size)
        self._logger = logger or logging.getLogger("encore.statistics")
        self._subscriptions: list[Subscription] = []

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> list[Subscription]:
        """Subscribe to the facts that mean something was done."""

        if not self._subscriptions:
            self._subscriptions = [
                self._events.subscribe(
                    event_type, self._handle, name=f"statistics.{event_type.__name__}"
                )
                for event_type in FACT_COUNTERS
            ]
        return self._subscriptions

    def close(self) -> None:
        """Withdraw the subscriptions, so a settling appliance counts nothing new."""

        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions = []

    # -- the request-side counters (SAPRS 10.3's "how often was it used") ---

    def searched(self) -> int:
        """One search happened. A method rather than a key, for the reason above."""

        return self._tally(StatisticKey.SEARCHES)

    def artist_viewed(self) -> int:
        return self._tally(StatisticKey.ARTIST_NAVIGATIONS)

    def album_viewed(self) -> int:
        return self._tally(StatisticKey.ALBUM_NAVIGATIONS)

    # -- reading -----------------------------------------------------------

    def counters(self, *, day: date | None = None) -> Mapping[str, int]:
        """The reported counters, for one day or for all time."""

        with self._store.unit_of_work() as work:
            return _reported(work.statistics.snapshot(day=day).as_dict())

    def report(self) -> StatisticsReport:
        """Everything the dashboard prints, in one transaction.

        One visit rather than six reads is not an optimisation here, it is a correctness
        claim: two snapshots taken around a song that ended mid-page would print a total
        smaller than the today figure beside it, and that is a bug report nobody can
        reproduce.
        """

        now = self._clock.now()
        today = now.date()
        days = [today - timedelta(days=ago) for ago in range(self._trend_days - 1, -1, -1)]
        with self._store.unit_of_work() as work:
            all_time = _reported(work.statistics.snapshot().as_dict())
            same_day = _reported(work.statistics.snapshot(day=today).as_dict())
            trend = tuple(
                sorted(work.statistics.series(StatisticKey.SONGS_PLAYED, days=days).items())
            )
            distinct_songs = work.history.distinct_songs()
            history_rows = work.history.total()
            outcomes = MappingProxyType(work.history.counts_by_outcome())
            leaderboard = tuple(work.history.leaderboard(limit=self._leaderboard_size))
        return StatisticsReport(
            generated_at=now,
            day=today,
            today=same_day,
            totals=all_time,
            trend=trend,
            distinct_songs=distinct_songs,
            history_rows=history_rows,
            outcomes=outcomes,
            leaderboard=leaderboard,
        )

    # -- internals ---------------------------------------------------------

    def _handle(self, event: Event) -> None:
        """One fact, as the counters it moves.

        A statistics failure is never allowed to travel back into playback — SAPRS 11.9's
        rule that a subscriber which raises must not stop the party — so the one exception
        caught here is the one this service cannot fix: a database that would not take the
        number. It is logged rather than swallowed, because a silent statistic is worse than
        a missing one.
        """

        for key in self._keys(event):
            try:
                self._tally(key)
            except Exception:
                self._logger.exception("statistics counter failed", extra={"key": key.value})

    def _keys(self, event: Event) -> tuple[StatisticKey, ...]:
        """The counters one fact moves, with `SongFinished`'s three answers spelled out."""

        if type(event) is SongFinished:
            return _finished_keys(event)
        return FACT_COUNTERS.get(type(event), ())

    def _tally(self, key: StatisticKey, *, amount: int = 1) -> int:
        """Add one to a counter and its total, in the same transaction.

        `amount` exists for a caller that counts a batch; nothing in v1 passes more than
        one, and the reason it is a parameter rather than a constant is that a statistic
        you cannot add two to is a statistic you cannot correct.
        """

        now = self._clock.now()
        with self._store.unit_of_work() as work:
            return work.statistics.increment_both(key, today=now.date(), amount=amount)


def _finished_keys(event: SongFinished) -> tuple[StatisticKey, ...]:
    """Plays, skips and faults: one fact, three counters, and no double counting.

    `counts_as_played` is the domain's rule and the only judge of SONGS_PLAYED (issue #23's
    regression test is why this reads the property rather than a local opinion). A skip is
    also a *rejection*, which is the number a host wants; a failure is neither a play nor a
    rejection but the reason there were no more plays, which is a third question again
    (SAPRS 11.9).
    """

    if event.reason is FinishedReason.FAILED:
        return (StatisticKey.PLAYBACK_ERRORS,)
    keys: list[StatisticKey] = []
    if event.counts_as_played:
        keys.append(StatisticKey.SONGS_PLAYED)
    if event.reason is FinishedReason.SKIPPED:
        keys.append(StatisticKey.QUEUE_SKIPPED)
    return tuple(keys)


def _reported(values: Mapping[str, int]) -> Mapping[str, int]:
    """Only the keys v1 counts, so a report cannot print a future feature as a zero."""

    return MappingProxyType(
        {key: value for key, value in sorted(values.items()) if key in _REPORTED_NAMES}
    )
