"""Event types: the fixed vocabulary, and the three properties of a fact.

SAPRS 11.2 states the requirements (immutable, typed, timestamped, independent
of HTTP); ADR-004 fixes the vocabulary; AIG 8 lists the eight names. This module
holds those to the letter, including the trap `encore/events/base.py` warns
about: an event that cannot be constructed raises at the first publication, in
the middle of a party, unless something publishes one of everything.
"""

from __future__ import annotations

import dataclasses
import inspect
from datetime import UTC, datetime, timedelta

import pytest

from encore import events
from encore.domain import HealthStatus, PlaybackState, QueueItemId, SongId
from encore.events import EVENT_VOCABULARY, Event, SongQueued
from encore.utilities.clock import SystemClock, ensure_aware

#: Every event AIG 8 names, by string, so renaming one fails even if the
#: `EVENT_VOCABULARY` tuple above is edited in the same breath.
AIG_8_NAMES = (
    "SongQueued",
    "SongStarted",
    "SongFinished",
    "QueueAdvanced",
    "PlaybackRecovered",
    "LibraryReloaded",
    "BuildCompleted",
    "HealthChanged",
)


def sample(event_type: type[Event]) -> Event:
    """A valid instance of every event, with the required fields filled in."""

    arguments: dict[str, object] = {
        "SongQueued": {
            "song_id": SongId(1),
            "queue_item_id": QueueItemId(1),
            "position": 1,
            "queue_length": 1,
        },
        "SongStarted": {"song_id": SongId(1), "queue_item_id": QueueItemId(1)},
        "SongFinished": {
            "song_id": SongId(1),
            "queue_item_id": QueueItemId(1),
            "reason": events.FinishedReason.COMPLETED,
        },
        "QueueAdvanced": {"now_playing": SongId(2), "queue_length": 3},
        "PlaybackRecovered": {"reason": "mpv exited with signal 11", "restart_count": 1},
        "LibraryReloaded": {"song_count": 15_000, "version": "2026-10-08"},
        "BuildCompleted": {
            "files_discovered": 15_002,
            "files_processed": 15_000,
            "files_skipped": 2,
            "song_count": 15_000,
            "validated": True,
        },
        "HealthChanged": {"status": HealthStatus.DEGRADED, "component": "playback"},
    }
    return event_type(**arguments[event_type.__name__])  # type: ignore[arg-type]


# -- the vocabulary ------------------------------------------------------


def test_the_vocabulary_is_exactly_eight_and_matches_aig_8() -> None:
    assert [event_type.__name__ for event_type in EVENT_VOCABULARY] == list(AIG_8_NAMES)


def test_no_event_type_lurks_outside_the_declared_set() -> None:
    """A ninth event is an architecture change, and ADR-004 says the set is fixed."""

    defined = {
        obj
        for _, obj in inspect.getmembers(events, inspect.isclass)
        if issubclass(obj, Event)
        and obj is not Event
        and obj.__module__.startswith("encore.events")
    }
    assert defined == set(EVENT_VOCABULARY), (
        f"{sorted(c.__name__ for c in defined - set(EVENT_VOCABULARY))} are not in AIG 8's list"
    )


@pytest.mark.parametrize("event_type", EVENT_VOCABULARY, ids=lambda c: c.__name__)
def test_every_event_can_be_constructed_and_published(event_type: type[Event]) -> None:
    """The regression guard for `base.py`'s `slots` warning.

    `@dataclass(slots=True)` rebuilds the class, which breaks the zero-argument
    `super()` a subclass's `__post_init__` would otherwise use - and it breaks it
    at publication, not at import. Constructing one of every event is the only
    way to know the package is sound.
    """

    event = sample(event_type)
    assert isinstance(event, Event)
    assert event.name == event_type.__name__


@pytest.mark.parametrize("event_type", EVENT_VOCABULARY, ids=lambda c: c.__name__)
def test_every_event_is_immutable(event_type: type[Event]) -> None:
    event = sample(event_type)
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.occurred_at = datetime(2020, 1, 1, tzinfo=UTC)  # type: ignore[misc]


@pytest.mark.parametrize("event_type", EVENT_VOCABULARY, ids=lambda c: c.__name__)
def test_every_event_is_timestamped_in_utc(event_type: type[Event]) -> None:
    """SAPRS 11.2 requires a timestamp; comparing across the party requires UTC."""

    stamp = sample(event_type).occurred_at
    assert stamp.tzinfo is not None
    assert stamp.utcoffset() == timedelta(0)
    assert abs((datetime.now(UTC) - stamp).total_seconds()) < 5


def test_timestamps_are_injected_not_read_from_a_global() -> None:
    """`docs/Developer/Testing.md` forbids wall-clock dependence in unit tests."""

    pinned = datetime(2026, 5, 4, 21, 30, tzinfo=UTC)
    stamped: SongQueued = dataclasses.replace(sample(SongQueued), occurred_at=pinned)  # type: ignore[assignment]
    assert stamped.occurred_at == pinned


def test_naive_timestamps_are_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        SongQueued(
            song_id=SongId(1),
            queue_item_id=QueueItemId(1),
            position=1,
            queue_length=1,
            occurred_at=datetime(2026, 1, 1),
        )


@pytest.mark.parametrize("event_type", EVENT_VOCABULARY, ids=lambda c: c.__name__)
def test_events_carry_no_http_or_presentation_state(event_type: type[Event]) -> None:
    """SAPRS 11.2: independent of HTTP. A request object in a payload ends that."""

    for field in dataclasses.fields(event_type):
        assert not field.name.startswith(("request", "response", "session", "html", "template"))


# -- payload rules -------------------------------------------------------


def test_song_queued_reports_a_position_the_queue_can_have() -> None:
    with pytest.raises(ValueError, match="shorter"):
        SongQueued(song_id=SongId(1), queue_item_id=QueueItemId(1), position=6, queue_length=3)


def test_song_queued_positions_are_one_based() -> None:
    with pytest.raises(ValueError, match="1-based"):
        SongQueued(song_id=SongId(1), queue_item_id=QueueItemId(1), position=0, queue_length=1)


def test_playback_recovery_states_a_reason() -> None:
    """SAPRS 7.6 asks for diagnostics; an empty reason is a shrug in data form."""

    with pytest.raises(ValueError, match="reason"):
        events.PlaybackRecovered(reason="   ", restart_count=1)
    with pytest.raises(ValueError, match="restart_count"):
        events.PlaybackRecovered(reason="mpv died", restart_count=0)


def test_recovery_names_where_playback_landed() -> None:
    recovered = events.PlaybackRecovered(
        reason="mpv exited", restart_count=3, resumed_state=PlaybackState.PLAYING
    )
    assert recovered.resumed_state is PlaybackState.PLAYING


def test_a_health_change_that_is_not_a_change_is_refused() -> None:
    """Publishing HEALTHY -> HEALTHY would make the dashboard lie."""

    with pytest.raises(ValueError, match="reports a move"):
        events.HealthChanged(status=HealthStatus.HEALTHY, previous=HealthStatus.HEALTHY)


def test_build_completion_cannot_invent_files() -> None:
    """SAPRS 6.12's counts must add up, or the report is decoration."""

    with pytest.raises(ValueError, match="process"):
        events.BuildCompleted(files_discovered=10, files_processed=9, files_skipped=9)


def test_queue_advance_may_report_silence() -> None:
    """SAPRS 8.9 lists "queue emptied"; `now_playing=None` is that fact."""

    emptied = events.QueueAdvanced(finished_queue_item_id=QueueItemId(4), queue_length=0)
    assert emptied.now_playing is None


def test_song_finished_distinguishes_a_play_from_a_skip() -> None:
    """Statistics count a play; a skip is not one (SAPRS 8.9)."""

    played: events.SongFinished = sample(events.SongFinished)  # type: ignore[assignment]
    skipped = dataclasses.replace(played, reason=events.FinishedReason.SKIPPED)
    assert played.counts_as_played
    assert not skipped.counts_as_played


def test_events_are_equatable_by_value() -> None:
    """Two publications of the same fact at the same instant are one fact.

    Equality compares `occurred_at` too, which is the honest behaviour: an event
    published a microsecond later is a different occurrence.
    """

    instant = datetime(2026, 5, 4, 21, 30, tzinfo=UTC)
    first: SongQueued = dataclasses.replace(sample(SongQueued), occurred_at=instant)  # type: ignore[assignment]
    assert first == dataclasses.replace(sample(SongQueued), occurred_at=instant)
    assert first != dataclasses.replace(first, song_id=SongId(2))
    assert first != dataclasses.replace(first, occurred_at=instant + timedelta(seconds=1))


# -- payload rules the happy path never reaches --------------------------


def test_a_build_report_cannot_lose_files() -> None:
    """SAPRS 6.12: the counts become the build report. A build that accounts for
    fewer files than it found would print a number nobody could act on."""

    with pytest.raises(ValueError, match="cannot process"):
        events.BuildCompleted(files_discovered=10, files_processed=8, files_skipped=8)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"files_discovered": -1},
        {"files_processed": -2},
        {"files_skipped": -1},
        {"song_count": -5},
    ],
)
def test_build_counts_cannot_be_negative(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="negative"):
        events.BuildCompleted(**kwargs)  # type: ignore[arg-type]


def test_a_reload_reports_a_real_song_count() -> None:
    with pytest.raises(ValueError, match="negative"):
        events.LibraryReloaded(song_count=-1)


def test_a_health_change_from_nothing_is_a_first_report_not_a_lie() -> None:
    """The distinction an operator needs: "mpv is broken" and "mpv has never
    told us anything" are different failures (SAPRS 11.6)."""

    first = events.HealthChanged(status=HealthStatus.UNAVAILABLE, component="mpv")
    assert first.previous is None, "the absence of a previous report is itself information"
    assert first.status is HealthStatus.UNAVAILABLE


def test_a_health_change_to_healthy_is_not_reported_as_a_change_forever() -> None:
    recovered = events.HealthChanged(
        status=HealthStatus.HEALTHY, previous=HealthStatus.DEGRADED, component="mpv"
    )
    assert recovered.status.is_healthy
    assert recovered.previous is not HealthStatus.HEALTHY


def test_a_queue_length_of_zero_is_a_fact_not_an_absence() -> None:
    """SAPRS 8.9: "queue emptied" must be expressible; only negatives are junk."""

    assert events.QueueAdvanced(queue_length=0).now_playing is None
    with pytest.raises(ValueError, match="negative"):
        events.QueueAdvanced(queue_length=-1)


def test_a_finished_song_names_a_reason_the_statistics_can_count() -> None:
    """A reason that is not one of the five would make `counts_as_played` lie, and
    that is a statistics discrepancy weeks later rather than an error today."""

    with pytest.raises(ValueError, match="FinishedReason"):
        events.SongFinished(
            song_id=SongId(1),
            queue_item_id=QueueItemId(1),
            reason="dropped",  # type: ignore[arg-type]
        )

    coerced = events.SongFinished(
        song_id=SongId(1),
        queue_item_id=QueueItemId(1),
        reason="skipped",  # type: ignore[arg-type]
    )
    assert coerced.reason is events.FinishedReason.SKIPPED
    assert not coerced.counts_as_played


def test_the_clock_is_injectable_but_defaults_to_the_real_one() -> None:
    """SAPRS 11.2 requires a timestamp; `docs/Developer/Testing.md` forbids
    depending on wall-clock time in a unit test. Both are satisfied by one
    defaulted parameter, so `occurred_at` is the seam."""

    pinned = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    event = events.SongStarted(song_id=SongId(1), queue_item_id=QueueItemId(1), occurred_at=pinned)
    assert event.occurred_at == pinned
    assert SystemClock().now().tzinfo is UTC
    assert ensure_aware(pinned, field_name="occurred_at") == pinned
    with pytest.raises(ValueError, match="occurred_at"):
        ensure_aware(pinned.replace(tzinfo=None), field_name="occurred_at")
