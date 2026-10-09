"""`playback_history` writes and reads (SAPRS 5.7, 10.3).

One row per track the appliance played, appended and never updated — which is the
opposite of the queue's lifecycle, and is the reason the statistics are derivable.
SAPRS 5.7 also settles the relationship question: history references song ids
*deliberately not as foreign keys*, because the library changes between builds and a
party's play log must not vanish when it does. The consequence is visible in
`recent()`: a song id that no longer resolves is reported as a gap in the names rather
than as a missing row.

Completion is stored rather than recomputed from mpv's position at stop time, because
the supervisor's crash path (`PlaybackRecovered`, milestone 10) knows how far a track
got and nothing else does.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final

from sqlalchemy import (
    CursorResult,  # noqa: F401 - typed at the cast site
    func,
    select,
    update,
)
from sqlalchemy.orm import Session

from encore.domain import PlaybackOutcome, SongId
from encore.repositories.runtime.models import PlaybackHistory

__all__ = ["HistoryEntry", "PlaybackHistoryRepository"]

#: The completion ratio that means "this finished on its own". SAPRS 7.2's threshold
#: for a normal advance and the one the statistics count on are the same number.
COMPLETED_RATIO: Final = 0.95


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoryEntry:
    """One played track, as stored.

    Deliberately not `encore.domain.PlaybackHistory`: that entity describes a playback
    *event* with a progress snapshot, and the row describes what happened to a song
    between two timestamps. Forcing one into the other would lose the completion ratio,
    which is the number the admin statistics are built from.
    """

    id: int
    song_id: SongId
    started_at: datetime
    finished_at: datetime | None
    completion: float
    outcome: PlaybackOutcome | None

    @property
    def duration_played(self) -> timedelta:
        if self.finished_at is None:
            return timedelta(0)
        return self.finished_at - self.started_at

    @property
    def completed(self) -> bool:
        return self.completion >= COMPLETED_RATIO


class PlaybackHistoryRepository:
    """Appends and reads the play log."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        *,
        song_id: SongId,
        started_at: datetime,
        finished_at: datetime | None = None,
        completion: float = 0.0,
        outcome: PlaybackOutcome | None = None,
    ) -> HistoryEntry:
        row = PlaybackHistory(
            song_id=int(song_id),
            started_at=started_at,
            finished_at=finished_at,
            completion=float(completion),
            outcome=None if outcome is None else outcome.value,
        )
        self._session.add(row)
        self._session.flush()
        return _to_entry(row)

    def close(
        self,
        *,
        song_id: SongId,
        finished_at: datetime,
        completion: float,
        outcome: PlaybackOutcome | None = None,
    ) -> int:
        """Finish the still-open row for `song_id`, if there is one.

        Matched by song rather than by id because the caller is the supervisor's
        recovery path, which knows what was playing and not what the primary key was.
        Returns the number of rows closed, so a caller can tell "nothing was open" from
        "two were", which is a bug in whoever opened them.
        """

        statement = (
            update(PlaybackHistory)
            .where(PlaybackHistory.finished_at.is_(None), PlaybackHistory.song_id == int(song_id))
            .values(
                finished_at=finished_at,
                completion=float(completion),
                outcome=None if outcome is None else outcome.value,
            )
        )
        return int(getattr(self._session.execute(statement), "rowcount", 0) or 0)

    def open_row(self) -> HistoryEntry | None:
        """The track that started and never finished.

        Read at startup: a kill between mpv opening a file and the row being closed
        leaves exactly this, and the recovery milestone needs to be able to say which
        song it was rather than guess from the queue.
        """

        row = self._session.scalars(
            select(PlaybackHistory)
            .where(PlaybackHistory.finished_at.is_(None))
            .order_by(PlaybackHistory.started_at.desc())
            .limit(1)
        ).first()
        return None if row is None else _to_entry(row)

    def recent(self, *, limit: int = 50) -> list[HistoryEntry]:
        rows = self._session.scalars(
            select(PlaybackHistory).order_by(PlaybackHistory.started_at.desc()).limit(limit)
        )
        return [_to_entry(row) for row in rows]

    def for_song(self, song_id: SongId, *, limit: int = 100) -> list[HistoryEntry]:
        rows = self._session.scalars(
            select(PlaybackHistory)
            .where(PlaybackHistory.song_id == int(song_id))
            .order_by(PlaybackHistory.started_at.desc())
            .limit(limit)
        )
        return [_to_entry(row) for row in rows]

    def played_on(self, day: date) -> int:
        """Tracks started on a given local day.

        The comparison is made against the day's boundaries rather than `func.date()`
        so the index on `started_at` is usable; a function around the column turns this
        into a scan of the whole log, which is the table most likely to grow.
        """

        start = datetime(day.year, day.month, day.day)
        return int(
            self._session.scalar(
                select(func.count())
                .select_from(PlaybackHistory)
                .where(
                    PlaybackHistory.started_at >= start,
                    PlaybackHistory.started_at < start + timedelta(days=1),
                )
            )
            or 0
        )

    def distinct_songs(self) -> int:
        return int(
            self._session.scalar(select(func.count(func.distinct(PlaybackHistory.song_id)))) or 0
        )

    def leaderboard(self, *, limit: int = 10) -> list[tuple[SongId, int]]:
        """Most-played songs, descending. Ties break by song id, which is stable.

        SAPRS 10.3 lists this as a statistic. It is one grouped query rather than a
        Counter over `recent()` because a Counter over a window is a different question
        — "most played lately" — and answering it with this name would be a lie that
        looked like a number.
        """

        rows = self._session.execute(
            select(PlaybackHistory.song_id, func.count())
            .group_by(PlaybackHistory.song_id)
            .order_by(func.count().desc(), PlaybackHistory.song_id)
            .limit(limit)
        )
        return [(SongId(int(song)), int(count)) for song, count in rows]

    def counts_by_outcome(self) -> dict[str, int]:
        rows = self._session.execute(
            select(PlaybackHistory.outcome, func.count()).group_by(PlaybackHistory.outcome)
        )
        return {str(outcome): int(count) for outcome, count in rows if outcome is not None}

    def total(self) -> int:
        return int(self._session.scalar(select(func.count()).select_from(PlaybackHistory)) or 0)

    def since(self, moment: datetime) -> Sequence[HistoryEntry]:
        rows = self._session.scalars(
            select(PlaybackHistory)
            .where(PlaybackHistory.started_at >= moment)
            .order_by(PlaybackHistory.started_at)
        )
        return [_to_entry(row) for row in rows]

    def busiest_hour(self, *, entries: Sequence[HistoryEntry] | None = None) -> int:
        """The hour of day with the most plays, for the party-simulation report.

        Computed in Python over an already-loaded window rather than in SQL: the
        simulation loads those rows anyway, and a second grouped query against the
        same table on a Pi 4 is the kind of "optimization before measuring" AIG 22
        names.
        """

        source = self.recent(limit=10_000) if entries is None else entries
        counter = Counter(entry.started_at.astimezone().hour for entry in source)
        return counter.most_common(1)[0][0] if counter else 0


def _to_entry(row: PlaybackHistory) -> HistoryEntry:
    return HistoryEntry(
        id=int(row.id),
        song_id=SongId(int(row.song_id)),
        started_at=row.started_at,
        finished_at=row.finished_at,
        completion=float(row.completion),
        outcome=None if row.outcome is None else PlaybackOutcome(row.outcome),
    )
