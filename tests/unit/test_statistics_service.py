"""Counting a party without being asked (SAPRS 10.3, 11.6, AIG 7's `StatisticsService`).

The service has one job — turn facts into numbers — and the interesting part of that job is
everything that can go wrong while nobody is looking: a song that counts twice because two
events describe it, a counter written from a subscriber that raises and stops playback, a
"today" that means two things either side of midnight, a dashboard that reads six numbers from
six transactions and prints a total smaller than the day beside it.

Everything runs against a real `runtime.db` and the real Event Bus, because the failure modes
above all live in the seam between a subscription and a transaction, and a dictionary fake
would let every one of them pass.

Three claims get tests rather than comments:

* **`SongFinished` is the only fact that counts a play** (`FACT_COUNTERS`). `SongStarted`,
  `QueueAdvanced` and `SongQueued` all describe the same music moving, and issue #23's
  regression — four plays recorded per song — is what an over-eager subscriber looks like.
* **A statistics failure cannot reach playback.** SAPRS 11.9's isolation rule, in the one
  place this milestone adds a subscriber to the bus.
* **v1.1's keys are absent from a v1.0 report.** `PLAYLIST_PLAYS` and `PEER_QUERIES` exist in
  the enum because the schema is forward-compatible (SAPRS 16.3), and a dashboard that printed
  them as zeroes would be a dashboard advertising features that do not exist.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from encore.domain import PlaybackOutcome, PlaybackState, QueueItemId, SongId
from encore.events import SongStarted
from encore.events.bus import EventBus
from encore.events.playback import FinishedReason, PlaybackRecovered, SongFinished
from encore.events.queue import SongQueued
from encore.repositories.runtime import RuntimeStore, StatisticKey, open_runtime_store
from encore.services.statistics_service import (
    DEFAULT_TREND_DAYS,
    REPORTED,
    StatisticsService,
)
from encore.utilities.clock import Clock

pytestmark = pytest.mark.unit

START = datetime(2026, 7, 14, 22, 30, tzinfo=UTC)


class AdvancingClock(Clock):
    def __init__(self, at: datetime = START) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at

    def advance(self, by: timedelta) -> None:
        self.at += by


class BrokenStore:
    """A `runtime.db` that raises the moment anyone opens a transaction.

    The statistics service's only interesting failure: it is subscribed to facts that playback
    publishes, and a counter that cannot be written must not become a song that does not play
    (SAPRS 11.9).
    """

    def unit_of_work(self) -> object:
        raise RuntimeError("disk full")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[RuntimeStore]:
    opened = open_runtime_store(tmp_path / "runtime.db")
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def bus() -> EventBus:
    return EventBus(logger=logging.getLogger("encore.test.statistics"))


@pytest.fixture
def clock() -> AdvancingClock:
    return AdvancingClock()


def service(
    store: object, bus: EventBus, *, clock: AdvancingClock | None = None, **options: object
) -> StatisticsService:
    made = StatisticsService(
        store=store,  # type: ignore[arg-type]
        events=bus,
        clock=clock or AdvancingClock(),
        **options,  # type: ignore[arg-type]
    )
    made.start()
    return made


def queued(song: int = 1, item: int = 1) -> SongQueued:
    return SongQueued(
        song_id=SongId(song),
        queue_item_id=QueueItemId(item),
        position=item,
        queue_length=item,
    )


def finished(
    song: int = 1, item: int = 1, reason: FinishedReason = FinishedReason.COMPLETED
) -> SongFinished:
    return SongFinished(
        song_id=SongId(song), queue_item_id=QueueItemId(item), reason=reason, completion=1.0
    )


# -- what a fact is worth ---------------------------------------------------


def test_a_queued_song_is_one_add_and_nothing_else(store: RuntimeStore, bus: EventBus) -> None:
    stats = service(store, bus)
    bus.publish(queued())

    totals = stats.report().totals

    assert totals[StatisticKey.QUEUE_ADDED.value] == 1
    assert totals[StatisticKey.SONGS_PLAYED.value] == 0, "queueing is not playing"


def test_one_song_makes_one_play_however_many_facts_describe_it(
    store: RuntimeStore, bus: EventBus
) -> None:
    """The shape of issue #23's bug, asserted directly.

    `SongQueued`, `SongStarted` and `QueueAdvanced` all accompany one play, and a subscriber
    that counted each of them would report four plays for one song — a number an operator
    cannot disprove, which is what makes this class of bug expensive.
    """

    stats = service(store, bus)
    bus.publish(queued())
    bus.publish(SongStarted(song_id=SongId(1), queue_item_id=QueueItemId(1)))
    bus.publish(finished())

    assert stats.report().totals[StatisticKey.SONGS_PLAYED.value] == 1


def test_the_reason_a_song_ended_selects_the_counters_it_moves(
    store: RuntimeStore, bus: EventBus
) -> None:
    """`FinishedReason` is why the play counter and the error counter are not one number.

    A skipped track is still a track that played; a failed one is a fault, and SAPRS 10.3 names
    both separately because an operator debugging a box needs the difference.
    """

    stats = service(store, bus)
    bus.publish(finished(1, 1, FinishedReason.COMPLETED))
    bus.publish(finished(2, 2, FinishedReason.SKIPPED))
    bus.publish(finished(3, 3, FinishedReason.FAILED))

    totals = stats.report().totals

    # One play out of three endings, because `PlaybackOutcome.counts_as_played` is the domain's
    # rule and says a skip, a stop and a failure are each a different question from "was this
    # track heard". The service reads that property rather than holding its own opinion, which
    # is what AIG 4's "one number, one writer" means for a counter.
    assert totals[StatisticKey.SONGS_PLAYED.value] == 1
    assert totals[StatisticKey.QUEUE_SKIPPED.value] == 1
    assert totals[StatisticKey.PLAYBACK_ERRORS.value] == 1


def test_a_recovery_is_counted_because_a_flapping_box_is_the_thing_to_notice(
    store: RuntimeStore, bus: EventBus
) -> None:
    stats = service(store, bus)
    bus.publish(
        PlaybackRecovered(reason="mpv exited", restart_count=1, resumed_state=PlaybackState.IDLE)
    )

    assert stats.report().totals[StatisticKey.RECOVERIES.value] == 1


def test_starting_twice_counts_each_fact_once(
    store: RuntimeStore, bus: EventBus, clock: AdvancingClock
) -> None:
    """The composition root calls `start()`; a test that restarts an appliance calls it twice.

    A second subscription to the same fact doubles every number on the dashboard, which is the
    statistics bug with the least visible cause and the most convincing output.
    """

    stats = StatisticsService(store=store, events=bus, clock=clock)
    assert len(stats.start()) == len(stats.start())
    bus.publish(queued())

    assert stats.report().totals[StatisticKey.QUEUE_ADDED.value] == 1


def test_closing_withdraws_the_subscriptions(store: RuntimeStore, bus: EventBus) -> None:
    """A stopped appliance must not be counting into a shutdown (SAPRS 11.8's ordering)."""

    stats = service(store, bus)
    bus.publish(queued())
    stats.close()
    bus.publish(queued(song=2, item=2))

    assert stats.report().totals[StatisticKey.QUEUE_ADDED.value] == 1


def test_a_counter_that_cannot_be_written_does_not_stop_the_party(
    bus: EventBus, clock: AdvancingClock, caplog: pytest.LogCaptureFixture
) -> None:
    """SAPRS 11.9's isolation rule, applied to the newest subscriber on the bus.

    The assertion is that `publish` survives and says so: a handler that raised would travel
    back into `QueueService`'s transaction — the bus delivers to subscribers inside the
    publisher's call — and turn a full disk into a failed song request, which is one fault
    becoming two and the wrong one being the visible one.
    """

    made = StatisticsService(store=BrokenStore(), events=bus, clock=clock)  # type: ignore[arg-type]
    made.start()

    with caplog.at_level(logging.WARNING, logger="encore.statistics"):
        bus.publish(queued())

    assert any("statistic" in record.getMessage().lower() for record in caplog.records), (
        "a counter that could not be written must be reported somewhere"
    )


# -- the counters only a request knows about -------------------------------


def test_a_browsed_page_counts_itself_without_touching_the_library(
    store: RuntimeStore, bus: EventBus
) -> None:
    stats = service(store, bus)

    stats.searched()
    stats.artist_viewed()
    stats.artist_viewed()
    stats.album_viewed()

    totals = stats.report().totals
    assert totals[StatisticKey.SEARCHES.value] == 1
    assert totals[StatisticKey.ARTIST_NAVIGATIONS.value] == 2
    assert totals[StatisticKey.ALBUM_NAVIGATIONS.value] == 1


# -- what a report is -------------------------------------------------------


def test_a_report_is_one_snapshot_of_one_instant(store: RuntimeStore, bus: EventBus) -> None:
    stats = service(store, bus)
    bus.publish(queued())
    bus.publish(finished())

    report = stats.report()

    assert report.generated_at == START
    assert report.day == START.date()
    assert report.plays_today == 1
    assert report.plays_total == 1
    assert report.errors == 0


def test_today_resolves_to_the_day_the_appliance_thinks_it_is(
    store: RuntimeStore, bus: EventBus, clock: AdvancingClock
) -> None:
    """The boundary that a "today" figure either respects or silently breaks.

    A counter written before midnight must not be in the figure after it, and the totals must
    still contain both: this is the one arithmetic in the service that no screen can check.
    """

    stats = service(store, bus, clock=clock)
    bus.publish(queued())
    clock.advance(timedelta(hours=3))
    bus.publish(queued(song=2, item=2))

    report = stats.report()

    assert report.day == date(2026, 7, 15)
    assert report.totals[StatisticKey.QUEUE_ADDED.value] == 2
    assert report.today[StatisticKey.QUEUE_ADDED.value] == 1


def test_the_trend_spans_the_days_the_service_was_built_with(
    store: RuntimeStore, bus: EventBus, clock: AdvancingClock
) -> None:
    stats = service(store, bus, clock=clock)
    clock.advance(timedelta(days=2))
    bus.publish(finished())

    trend = stats.report().trend

    assert len(trend) == DEFAULT_TREND_DAYS
    assert trend[-1] == (START.date() + timedelta(days=2), 1)
    assert trend[0] == (START.date() - timedelta(days=4), 0)
    assert stats.report().busiest_day == (START.date() + timedelta(days=2), 1)


def test_the_play_log_is_reported_as_a_shape_and_not_only_a_count(
    store: RuntimeStore, bus: EventBus
) -> None:
    """SAPRS 10.3's "top tracks" is a leaderboard, and a leaderboard needs the log.

    The rows are written through the repository the way playback writes them, rather than by
    publishing events, because what is being checked is the report's reading of the log: how
    many different songs, how many finished, and how each one ended.
    """

    with store.unit_of_work() as work:
        for song, outcome in (
            (1, PlaybackOutcome.COMPLETED),
            (1, PlaybackOutcome.COMPLETED),
            (2, PlaybackOutcome.SKIPPED),
        ):
            work.history.record(
                song_id=SongId(song),
                started_at=START,
                finished_at=START + timedelta(minutes=3),
                completion=1.0,
                outcome=outcome,
            )

    stats = service(store, bus)
    report = stats.report()

    assert report.history_rows == 3
    assert report.distinct_songs == 2
    assert report.outcomes == {"completed": 2, "skipped": 1}
    assert report.leaderboard[0] == (SongId(1), 2)
    # The counters are untouched, because rows were written to the log rather than played:
    # the two views in one report are deliberately two sources, and a dashboard that could
    # not tell them apart would credit a restored `playback_history` to tonight's party.
    assert report.plays_total == 0


def test_a_v1_1_counter_is_absent_rather_than_zero(store: RuntimeStore, bus: EventBus) -> None:
    """The schema has keys for features v1 does not (SAPRS 16.3, 16.5).

    Printing them as zeroes would advertise a playlist feature on a screen an operator trusts,
    and a zero is indistinguishable from "nobody used it" — which is a lie about a thing that
    was never built.
    """

    with store.unit_of_work() as work:
        work.statistics.increment_both(StatisticKey.PEER_QUERIES, today=START.date())
        work.statistics.increment_both(StatisticKey.PLAYLIST_PLAYS, today=START.date())

    totals = service(store, bus).report().totals

    assert StatisticKey.PEER_QUERIES.value not in totals
    assert StatisticKey.PLAYLIST_PLAYS.value not in totals


def test_the_json_report_is_the_pages_report(store: RuntimeStore, bus: EventBus) -> None:
    """`/api/v1/stats` and `/admin/statistics` may not describe different parties.

    One call to `report()` is the whole of the guarantee, and this is the assertion that keeps
    a future "add a second, cheaper endpoint" from making the two drift.
    """

    stats = service(store, bus)
    bus.publish(queued())
    bus.publish(finished())

    as_json = stats.report().as_json()

    assert as_json["totals"] == dict(stats.report().totals)
    assert as_json["day"] == START.date().isoformat()
    trend = cast("list[dict[str, object]]", as_json["trend"])
    assert trend[-1]["plays"] == 1
    assert set(as_json) >= {"generated_at", "today", "outcomes", "leaderboard"}


def test_an_empty_appliance_reports_zeroes_rather_than_nothing(
    store: RuntimeStore, bus: EventBus
) -> None:
    """A page that renders nothing looks broken, which is worse than a page showing nought.

    The counters have no rows at all until something happens, so this asserts the service's
    reading of an absent row — the difference between an appliance that has never played a song
    and one whose statistics table is missing.
    """

    report = service(store, bus).report()

    # Every v1 key, printed as zero rather than omitted: a page with a row missing is a page
    # that looks like it broke, and the repository's snapshot is zero-filled for exactly this
    # reason (`RuntimeStatisticsRepository.snapshot`).
    assert report.totals == {key.value: 0 for key in REPORTED}
    assert report.today == {key.value: 0 for key in REPORTED}
    assert report.plays_total == 0
    assert report.plays_today == 0
    assert report.errors == 0
    assert report.leaderboard == ()
    assert report.busiest_day[1] == 0
