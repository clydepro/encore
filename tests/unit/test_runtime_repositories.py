"""The four runtime repositories, as storage and nothing else (SAPRS 5.7, 7.x, 10.3).

The rule these test is ADR-009's: a repository contains no business logic. So the
assertions are all about *durability and shape* — what a write leaves behind, what a read
returns, what survives a reopen — and never about whether a queue should have advanced.
The moment one of these tests asks a question about playback, it has moved a rule out of
the domain and into the wrong layer.

Everything runs through `store.unit_of_work()`, because that is the only supported way
to reach a repository, and because the atomicity it provides is the property SAPRS 12.1
calls "transactional queue mutations".
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from encore.domain import PlaybackOutcome, QueueItemId, QueueItemStatus, SongId
from encore.repositories.contract import RuntimeTable as RT
from encore.repositories.runtime import (
    HistoryEntry,
    Pbkdf2Hasher,
    RuntimeStore,
    StatisticKey,
    open_runtime_store,
)
from encore.repositories.runtime.admin_state import current_scheme_is_current
from encore.utilities.clock import utc_now

DAY = date(2026, 7, 14)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[RuntimeStore]:
    opened = open_runtime_store(tmp_path / "runtime.db")
    try:
        yield opened
    finally:
        opened.close()


# -- queue ----------------------------------------------------------------


def test_append_is_fifo_and_numbered(store: RuntimeStore) -> None:
    with store.unit_of_work() as work:
        first = work.queue.append(SongId(1))
        second = work.queue.append(SongId(2))
    assert first.position < second.position
    assert [int(item.song_id) for item in work.queue.active()] == [1, 2]


def test_positions_are_one_based(store: RuntimeStore) -> None:
    """`QueueItem` enforces it, so a repository that wrote 0 would fail on read-back."""

    with store.unit_of_work() as work:
        assert work.queue.append(SongId(1)).position == 1


def test_duplicates_are_allowed_and_distinct_rows(store: RuntimeStore) -> None:
    """SAPRS 7.2: the same song twice is two items, and the queue must know that."""

    with store.unit_of_work() as work:
        one = work.queue.append(SongId(9))
        two = work.queue.append(SongId(9))
        assert one.id != two.id
        assert work.queue.count() == 2
        assert [int(song) for song in work.queue.song_ids()] == [9, 9]


def test_status_transition_records_played_at(store: RuntimeStore) -> None:
    moment = utc_now()
    with store.unit_of_work() as work:
        item = work.queue.append(SongId(1))
        pending = work.queue.by_id(item.id)
        assert pending is not None
        assert pending.played_at is None
        work.queue.set_status(item.id, QueueItemStatus.PLAYING, at=moment)
        started = work.queue.by_id(item.id)
        assert started is not None
        assert started.played_at == moment
        row = work.session.execute(
            text(f"SELECT played_at, status FROM {RT.QUEUE_ITEMS} WHERE id = :id"),
            {"id": int(item.id)},
        ).first()
    assert row is not None
    assert str(row[0]).startswith(moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"))
    assert row[1] == "playing"


def test_removed_items_leave_the_history_alone(store: RuntimeStore) -> None:
    with store.unit_of_work() as work:
        item = work.queue.append(SongId(1))
        assert work.queue.remove(item.id) is True
        assert work.queue.remove(item.id) is False
        assert work.queue.active() == []


def test_compaction_closes_the_gap(store: RuntimeStore) -> None:
    with store.unit_of_work() as work:
        items = [work.queue.append(SongId(index)) for index in range(1, 5)]
        work.queue.remove(items[1].id)
        assert [item.position for item in work.queue.active()] == [1, 3, 4]
        assert work.queue.compact() == 2
        assert [item.position for item in work.queue.active()] == [1, 2, 3]


def test_clear_leaves_the_playing_item(store: RuntimeStore) -> None:
    """SAPRS 7.6's "clear queue" stops intake; it does not disconnect the amplifier."""

    with store.unit_of_work() as work:
        playing = work.queue.append(SongId(1))
        work.queue.append(SongId(2))
        work.queue.set_status(playing.id, QueueItemStatus.PLAYING, at=utc_now())
        assert work.queue.clear() == 1
        assert [int(item.song_id) for item in work.queue.active()] == [1]


def test_finish_active_is_counted_not_asserted(store: RuntimeStore) -> None:
    with store.unit_of_work() as work:
        for index in (1, 2):
            item = work.queue.append(SongId(index))
            work.queue.set_status(item.id, QueueItemStatus.PLAYING, at=utc_now())
        assert work.queue.finish_active(at=utc_now()) == 2
        assert work.queue.active() == []


def test_waiting_since_is_zero_for_an_empty_queue(store: RuntimeStore) -> None:
    """A dashboard that reads "no data" as an error is a dashboard that gets ignored."""

    with store.unit_of_work() as work:
        assert work.queue.waiting_since(now=utc_now()) == timedelta(0)
        work.queue.append(SongId(1))
        assert work.queue.waiting_since(now=utc_now()) >= timedelta(0)


def test_replace_writes_the_callers_order(store: RuntimeStore) -> None:
    """A reordered queue is written back as one set, positions included."""

    with store.unit_of_work() as work:
        items = [work.queue.append(SongId(index)) for index in range(1, 4)]
    moved = [replace(item, position=index + 1) for index, item in enumerate(reversed(items))]
    with store.unit_of_work() as work:
        work.queue.replace(moved)
        assert [int(item.song_id) for item in work.queue.active()] == [3, 2, 1]


def test_the_queue_survives_a_reopen(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    store = open_runtime_store(path)
    with store.unit_of_work() as work:
        work.queue.append(SongId(11))
        work.queue.append(SongId(12))
    store.close()

    again = open_runtime_store(path)
    with again.unit_of_work() as work:
        assert [int(item.song_id) for item in work.queue.active()] == [11, 12]
        assert work.queue.append(SongId(13)).position == 3
    again.close()


def test_a_rolled_back_append_leaves_nothing(store: RuntimeStore) -> None:
    """SAPRS 12.1's "transactional queue mutations", in the failure direction."""

    def fail_mid_transaction(work: object) -> None:
        work.queue.append(SongId(1))  # type: ignore[attr-defined]
        raise ZeroDivisionError

    with pytest.raises(ZeroDivisionError), store.unit_of_work() as work:
        fail_mid_transaction(work)
    with store.unit_of_work() as work:
        assert work.queue.count() == 0


def test_song_id_lookup_skips_the_full_read(store: RuntimeStore) -> None:
    with store.unit_of_work() as work:
        item = work.queue.append(SongId(5))
        assert work.queue.song_id_for(item.id) == SongId(5)
        assert work.queue.song_id_for(QueueItemId(999)) is None


# -- history --------------------------------------------------------------


def test_history_records_a_finished_track(store: RuntimeStore) -> None:
    started = utc_now()
    with store.unit_of_work() as work:
        entry = work.history.record(
            song_id=SongId(1),
            started_at=started,
            finished_at=started + timedelta(seconds=30),
            completion=1.0,
            outcome=PlaybackOutcome.COMPLETED,
        )
    assert isinstance(entry, HistoryEntry)
    assert entry.completed
    assert entry.duration_played == timedelta(seconds=30)
    assert entry.outcome is PlaybackOutcome.COMPLETED


def test_history_is_append_only(store: RuntimeStore) -> None:
    with store.unit_of_work() as work:
        work.history.record(song_id=SongId(1), started_at=utc_now(), completion=0.5)
        work.history.record(song_id=SongId(1), started_at=utc_now(), completion=1.0)
        assert work.history.total() == 2
        assert [entry.completion for entry in work.history.recent(limit=5)] == [1.0, 0.5]


def test_an_open_row_is_findable_and_closable(store: RuntimeStore) -> None:
    """The crash path: a kill between "started" and "finished" leaves exactly this."""

    started = utc_now()
    with store.unit_of_work() as work:
        work.history.record(song_id=SongId(4), started_at=started, completion=0.0)
        open_row = work.history.open_row()
        assert open_row is not None
        assert open_row.finished_at is None
        assert (
            work.history.close(
                song_id=SongId(4), finished_at=started + timedelta(1), completion=0.2
            )
            == 1
        )
        assert work.history.open_row() is None


def test_closing_nothing_is_not_an_error(store: RuntimeStore) -> None:
    with store.unit_of_work() as work:
        assert work.history.close(song_id=SongId(1), finished_at=utc_now(), completion=1.0) == 0


def test_history_survives_a_missing_song(store: RuntimeStore) -> None:
    """SAPRS 5.7: no foreign key, deliberately — a rebuilt library must not erase the log."""

    with store.unit_of_work() as work:
        work.history.record(song_id=SongId(999_999), started_at=utc_now(), completion=1.0)
        assert work.history.total() == 1


def test_leaderboard_counts_plays(store: RuntimeStore) -> None:
    with store.unit_of_work() as work:
        for song in (1, 1, 1, 2):
            work.history.record(song_id=SongId(song), started_at=utc_now(), completion=1.0)
        assert work.history.leaderboard(limit=2) == [(SongId(1), 3), (SongId(2), 1)]


def test_played_on_a_day_uses_the_index(store: RuntimeStore) -> None:
    start = datetime(2026, 7, 14, 12, tzinfo=UTC)
    with store.unit_of_work() as work:
        work.history.record(song_id=SongId(1), started_at=start, completion=1.0)
        work.history.record(song_id=SongId(2), started_at=start + timedelta(days=1), completion=1.0)
        assert work.history.played_on(DAY) == 1
        assert work.history.played_on(DAY + timedelta(days=1)) == 1
        assert work.history.played_on(DAY + timedelta(days=7)) == 0


def test_distinct_songs_and_outcomes(store: RuntimeStore) -> None:
    with store.unit_of_work() as work:
        work.history.record(
            song_id=SongId(1),
            started_at=utc_now(),
            completion=1.0,
            outcome=PlaybackOutcome.COMPLETED,
        )
        work.history.record(
            song_id=SongId(1), started_at=utc_now(), completion=0.1, outcome=PlaybackOutcome.SKIPPED
        )
        assert work.history.distinct_songs() == 1
        assert work.history.counts_by_outcome() == {"completed": 1, "skipped": 1}


def test_busiest_hour_is_a_count_and_not_a_crash(store: RuntimeStore) -> None:
    with store.unit_of_work() as work:
        assert work.history.busiest_hour(entries=[]) == 0
        evening = datetime(2026, 7, 14, 22, tzinfo=UTC)
        work.history.record(song_id=SongId(1), started_at=evening)
    # Local hour, because "when was the party busiest" is a question about a room.
    with store.unit_of_work():
        assert work.history.busiest_hour() == evening.astimezone().hour


# -- statistics -----------------------------------------------------------


def test_counters_are_atomic_and_cumulative(store: RuntimeStore) -> None:
    with store.unit_of_work() as work:
        assert work.statistics.increment(StatisticKey.SEARCHES) == 1
        assert work.statistics.increment(StatisticKey.SEARCHES) == 2
        assert work.statistics.total(StatisticKey.SEARCHES) == 2


def test_a_day_and_the_total_move_together(store: RuntimeStore) -> None:
    """`increment_both`, because two calls is a crash between two statements."""

    with store.unit_of_work() as work:
        assert work.statistics.increment_both(StatisticKey.SONGS_PLAYED, today=DAY) == 1
        assert work.statistics.value(StatisticKey.SONGS_PLAYED, day=DAY) == 1
        assert work.statistics.total(StatisticKey.SONGS_PLAYED) == 1


def test_the_all_time_counter_is_one_row(store: RuntimeStore) -> None:
    """An empty `day` rather than NULL, because SQLite treats NULLs as distinct inside a
    `UNIQUE` and a second increment would have inserted a second total."""

    with store.unit_of_work() as work:
        for _ in range(3):
            work.statistics.increment(StatisticKey.QUEUE_ADDED)
    with store.session() as session:
        rows = session.execute(
            text(f"SELECT day, value FROM {RT.RUNTIME_STATISTICS} WHERE key = 'queue_added'")
        ).all()
    assert len(rows) == 1, f"the all-time counter split into {len(rows)} rows"
    assert str(rows[0][0]) == ""


def test_a_series_fills_missing_days_with_zero(store: RuntimeStore) -> None:
    with store.unit_of_work() as work:
        work.statistics.increment(StatisticKey.SEARCHES, day=DAY)
        series = work.statistics.series(StatisticKey.SEARCHES, days=[DAY, DAY + timedelta(days=1)])
    assert series == {DAY: 1, DAY + timedelta(days=1): 0}


def test_a_snapshot_names_every_key(store: RuntimeStore) -> None:
    """A missing key reads 0 rather than absent, so a template never prints a blank."""

    with store.unit_of_work() as work:
        work.statistics.increment(StatisticKey.RECOVERIES)
        snapshot = work.statistics.snapshot()
    assert snapshot[StatisticKey.RECOVERIES] == 1
    assert snapshot[StatisticKey.PEER_JOINS] == 0
    assert set(snapshot.as_dict()) == {key.value for key in StatisticKey}
    assert snapshot.total_plays == 0


def test_a_set_value_overwrites(store: RuntimeStore) -> None:
    """For a measurement, not a tally: a gauge the collector rewrites each poll."""

    with store.unit_of_work() as work:
        work.statistics.set_value(StatisticKey.PEER_QUERIES, 40)
        work.statistics.set_value(StatisticKey.PEER_QUERIES, 41)
        assert work.statistics.total(StatisticKey.PEER_QUERIES) == 41
        assert work.statistics.increment(StatisticKey.PEER_QUERIES, -1) == 40


def test_keys_in_use_excludes_never_touched_counters(store: RuntimeStore) -> None:
    with store.unit_of_work() as work:
        work.statistics.increment(StatisticKey.QUEUE_SKIPPED)
        assert work.statistics.keys_in_use() == ["queue_skipped"]


# -- admin state ----------------------------------------------------------


def test_a_password_is_hashed_and_verifiable(store: RuntimeStore) -> None:
    with store.unit_of_work() as work:
        assert work.admin_state.is_configured() is False
        work.admin_state.set_password("correct horse battery staple")
        assert work.admin_state.verify("correct horse battery staple") is True
        assert work.admin_state.verify("wrong") is False


def test_the_stored_value_is_not_the_password(store: RuntimeStore) -> None:
    secret = "a password nobody should see twice"  # pragma: allowlist secret
    with store.unit_of_work() as work:
        work.admin_state.set_password(secret)
    with store.session() as session:
        stored = session.execute(text(f"SELECT password_hash FROM {RT.ADMIN_STATE}")).scalar_one()
    assert secret not in str(stored)
    assert str(stored).startswith("pbkdf2-sha256$")


def test_an_empty_password_is_refused(store: RuntimeStore) -> None:
    """SAPRS 12.3 assumes a password exists; writing none must not be possible."""

    with store.unit_of_work() as work, pytest.raises(ValueError, match="cannot be empty"):
        work.admin_state.set_password("")


def test_a_hash_records_its_own_parameters(store: RuntimeStore) -> None:
    """Rotation without lockout: the format says how it was made (see `admin_state.py`)."""

    with store.unit_of_work() as work:
        work.admin_state.set_password("first")
        assert work.admin_state.stored_scheme() == "pbkdf2-sha256"
        assert work.admin_state.scheme() == "pbkdf2-sha256"


def test_a_hash_from_a_weaker_build_still_verifies(tmp_path: Path) -> None:
    """The repository must not be the thing that locks someone out after an upgrade."""

    hasher = Pbkdf2Hasher(rounds=1_000)
    path = tmp_path / "runtime.db"
    opened = open_runtime_store(path, hasher=hasher)
    with opened.unit_of_work() as work:
        work.admin_state.set_password("old")
    opened.close()

    stronger = open_runtime_store(path, hasher=Pbkdf2Hasher())
    with stronger.unit_of_work() as work:
        assert work.admin_state.verify("old") is True
    with stronger.session() as session:
        stored = session.execute(text(f"SELECT password_hash FROM {RT.ADMIN_STATE}")).scalar_one()
    assert current_scheme_is_current(Pbkdf2Hasher(), str(stored)) is False
    stronger.close()


def test_the_credential_can_be_forgotten(store: RuntimeStore) -> None:
    with store.unit_of_work() as work:
        work.admin_state.set_password("x")
        work.admin_state.delete()
        assert work.admin_state.is_configured() is False
