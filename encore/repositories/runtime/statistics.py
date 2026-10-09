"""`runtime_statistics` counters (SAPRS 5.7, 10.3).

A counter is `(key, day, value)`, and the day is part of the identity rather than an
afterthought: SAPRS 10.3's questions are all "how many today", and an appliance that
only keeps a total cannot answer them. The totals are not stored separately — the
`day IS NULL` row is the all-time figure — because two ways to compute one number is
one way too many to disagree.

The interesting property is `increment`. A naive read-modify-write is correct on a
single-threaded queue and wrong the moment the admin interface and a request handler
touch the same counter, which is the normal case; SQLite's `ON CONFLICT DO UPDATE` is
one statement and one write lock, so the increment happens where the value lives.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from enum import StrEnum, auto
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from encore.repositories.errors import RepositoryError
from encore.repositories.runtime.models import RuntimeStatistics

__all__ = ["RuntimeStatisticsRepository", "StatisticKey", "StatisticsSnapshot"]


class StatisticKey(StrEnum):
    """The counters Encore keeps. SAPRS 10.3, named as data.

    Extending this enum is how a new statistic is added; there is deliberately no
    `OTHER`, and a free-form key is how two subsystems end up counting the same thing
    under different spellings.
    """

    SONGS_PLAYED = auto()
    SEARCHES = auto()
    ARTIST_NAVIGATIONS = auto()
    ALBUM_NAVIGATIONS = auto()
    QUEUE_ADDED = auto()
    QUEUE_SKIPPED = auto()
    PLAYLIST_PLAYS = auto()
    PLAYLIST_BUILDS = auto()
    PLAYBACK_ERRORS = auto()
    RECOVERIES = auto()
    PEER_QUERIES = auto()
    PEER_JOINS = auto()


#: The statistic shown as "total plays" on the dashboard, which is the one that has to
#: be right for the party simulation's assertions to mean anything.
PLAYED: Final = StatisticKey.SONGS_PLAYED


class RuntimeStatisticsRepository:
    """Atomic counters, keyed and optionally dated."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def increment(
        self,
        key: StatisticKey,
        amount: int = 1,
        *,
        day: date | datetime | None = None,
    ) -> int:
        """Add `amount` and return the new value.

        `day=None` updates the all-time total, and a caller that passes a day usually
        wants the total too. That pair is `increment_both`, not two calls: two calls is
        two statements, and a crash between them is a total that does not match the sum
        of its days.
        """

        value = int(amount)
        statement = (
            sqlite_insert(RuntimeStatistics)
            .values(key=key.value, value=value, day=_day(day))
            .on_conflict_do_update(
                index_elements=("key", "day"), set_={"value": RuntimeStatistics.value + value}
            )
            .returning(RuntimeStatistics.value)
        )
        result = self._session.execute(statement).scalar()
        if result is None:  # pragma: no cover - `RETURNING` always answers on SQLite 3.35+
            raise RepositoryError(f"statistics increment for {key.value} returned no value")
        return int(result)

    def increment_both(self, key: StatisticKey, *, today: date, amount: int = 1) -> int:
        """Update today's counter and the total in one transaction."""

        self.increment(key, amount, day=today)
        return self.increment(key, amount)

    def set_value(self, key: StatisticKey, value: int, *, day: date | None = None) -> None:
        """Overwrite a counter. Only for a value that is a measurement, not a tally."""

        self._session.execute(
            sqlite_insert(RuntimeStatistics)
            .values(key=key.value, value=int(value), day=_day(day))
            .on_conflict_do_update(index_elements=("key", "day"), set_={"value": int(value)})
        )

    def value(self, key: StatisticKey, *, day: date | None = None) -> int:
        found = self._session.scalar(
            select(RuntimeStatistics.value).where(
                RuntimeStatistics.key == key.value,
                RuntimeStatistics.day == _day(day),
            )
        )
        return 0 if found is None else int(found)

    def total(self, key: StatisticKey) -> int:
        return self.value(key)

    def series(self, key: StatisticKey, *, days: Iterable[date]) -> dict[date, int]:
        """The named counters over a set of days, missing days as zero.

        Zeros are filled in here rather than left absent, because every consumer is a
        chart and a chart that skips a day draws a straight line across the gap and
        says "nothing happened here" in the wrong font.
        """

        wanted = [item.isoformat() for item in days]
        rows = self._session.execute(
            select(RuntimeStatistics.day, RuntimeStatistics.value).where(
                RuntimeStatistics.key == key.value, RuntimeStatistics.day.in_(wanted)
            )
        )
        found = {str(day): int(value) for day, value in rows if day is not None}
        return {item: found.get(item.isoformat(), 0) for item in days}

    def snapshot(self, *, day: date | None = None) -> StatisticsSnapshot:
        """Every counter as of one call, for the dashboard and the JSON API."""

        rows = self._session.execute(
            select(RuntimeStatistics.key, func.sum(RuntimeStatistics.value))
            .where(
                RuntimeStatistics.day == _ALL_TIME
                if day is None
                else RuntimeStatistics.day == day.isoformat()
            )
            .group_by(RuntimeStatistics.key)
        )
        totals = {str(key): int(total or 0) for key, total in rows}
        for member in StatisticKey:
            totals.setdefault(member.value, 0)
        return StatisticsSnapshot(day=day, values=totals)

    def keys_in_use(self) -> list[str]:
        rows = self._session.scalars(
            select(RuntimeStatistics.key).distinct().order_by(RuntimeStatistics.key)
        )
        return [str(row) for row in rows]


class StatisticsSnapshot:
    """An immutable view of the counters. Attribute access, not dict access, so a
    typo in a template is a missing attribute rather than a silent zero."""

    __slots__ = ("_values", "day")

    def __init__(self, *, day: date | None, values: dict[str, int]) -> None:
        self.day = day
        self._values = values

    def __getitem__(self, key: StatisticKey | str) -> int:
        return self._values[key.value if isinstance(key, StatisticKey) else key]

    def __contains__(self, key: object) -> bool:
        return str(key) in self._values

    def as_dict(self) -> dict[str, int]:
        return dict(self._values)

    @property
    def total_plays(self) -> int:
        return self._values.get(PLAYED.value, 0)

    def __repr__(self) -> str:
        return f"StatisticsSnapshot(day={self.day!r}, {len(self._values)} keys)"


#: The `day` value that means "the all-time total". An empty string rather than NULL,
#: because SQLite treats NULLs as distinct inside a `UNIQUE`, which would let a second
#: total row for the same key exist and make the upsert an insert.
_ALL_TIME: Final = ""


def _day(value: date | datetime | None) -> str:
    """The day key.

    A `datetime` is accepted and truncated to its date, because callers hold a
    timestamp and converting at the boundary is cheaper than explaining why the
    counter for 23:59 landed on the wrong day.
    """

    return (
        _ALL_TIME
        if value is None
        else (value.date().isoformat() if isinstance(value, datetime) else value.isoformat())
    )
