"""`queue_items` reads and writes (SAPRS 5.7, 7.2, 7.6).

The queue's rules live in `encore.domain.queue` and, from milestone 9, in
`QueueService`: strict FIFO, duplicates allowed, positions gap-free (SAPRS 7.6). What
this module owns is the storage half of those sentences — including the one nobody
writes down, which is that "position" has to be a column rather than a row number,
because the HTTP API names an item by id while its position changes under it.

There is no foreign key from `song_id` to `library.db`. The two databases are separate
files and SQLite cannot cross-file-reference without `ATTACH`, which ADR-009 keeps out
of the runtime. The consequence is stated where it costs something: a library rebuilt
between a song being queued and being played leaves an id that resolves to nothing,
and `SongRepository.by_id` returning `None` is the signal the queue service acts on.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Result
from sqlalchemy.orm import Session

from encore.domain import QueueItem, QueueItemId, QueueItemStatus, SongId
from encore.repositories.runtime.models import QueueItemRow

__all__ = ["QueueRepository"]

#: The status that means "this item is the one on the speakers".
PLAYING: Final = QueueItemStatus.PLAYING


def _rowcount(result: Result[tuple[Any, ...]]) -> int:
    """How many rows a DML statement touched, from whatever the engine handed back.

    SQLAlchemy types `Session.execute` as a `Result` and keeps `rowcount` on the
    `CursorResult` subclass, which is correct and unusable: a DML statement always
    answers with the cursor kind, and saying so here is the one cast in this package
    that has no domain meaning attached to it.
    """

    return int(getattr(result, "rowcount", 0) or 0)


class QueueRepository:
    """The whole of `queue_items`. One session, one transaction, no business rules."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def active(self) -> list[QueueItem]:
        """Everything still to play, in FIFO order.

        `pending` and `playing` only: a finished or skipped item stays in the table
        because its row is the audit trail behind "what did we play at 11:40", but it
        is not in anyone's queue any more.
        """

        rows = self._session.scalars(
            select(QueueItemRow)
            .where(QueueItemRow.status.in_((_PENDING, _PLAYING)))
            .order_by(QueueItemRow.position, QueueItemRow.id)
        )
        return [_to_item(row) for row in rows]

    def all_items(self) -> list[QueueItem]:
        rows = self._session.scalars(
            select(QueueItemRow).order_by(QueueItemRow.position, QueueItemRow.id)
        )
        return [_to_item(row) for row in rows]

    def by_id(self, item_id: QueueItemId) -> QueueItem | None:
        row = self._session.get(QueueItemRow, int(item_id))
        return None if row is None else _to_item(row)

    def song_id_for(self, item_id: QueueItemId) -> SongId | None:
        """Which song an item names, without loading anything else.

        The skip path needs exactly this and nothing more; building a domain object to
        throw it away is the kind of cost that shows up in a 50 ms budget.
        """

        value = self._session.scalar(
            select(QueueItemRow.song_id).where(QueueItemRow.id == int(item_id))
        )
        return None if value is None else SongId(int(value))

    def append(self, song_id: SongId, *, now: datetime | None = None) -> QueueItem:
        """Add one item at the end of the queue.

        Position is `MAX(position) + 1` over *active* items, computed in the database
        rather than in Python: two guests queueing at the same instant is the normal
        case at a party, and the two requests cannot agree on a count they each read
        separately.
        """

        #: Positions are 1-based (`encore.domain.QueueItem` enforces it), so the first
        #: item in an empty queue is position 1 rather than 0.
        maximum = self._session.scalar(
            select(func.max(QueueItemRow.position)).where(
                QueueItemRow.status.in_((_PENDING, _PLAYING))
            )
        )
        values: dict[str, object] = {
            "song_id": int(song_id),
            "position": 1 if maximum is None else int(maximum) + 1,
            "status": _PENDING,
        }
        if now is not None:
            values["enqueued_at"] = now
        item = QueueItemRow(**values)
        self._session.add(item)
        self._session.flush()
        return _to_item(item)

    def replace(self, items: Sequence[QueueItem]) -> None:
        """Rewrite the table to match `items` exactly.

        Present because a domain-side reordering — a drag-to-reorder in the admin
        interface, or a compaction after a removal — has to be written back as one
        set, and a per-row update would let a concurrent append land in the middle of
        it. The caller wraps this in a transaction with whatever it also does to the
        in-memory queue; the atomicity is the transaction's job (SAPRS 12.1).
        """

        self._session.execute(delete(QueueItemRow))
        for item in items:
            self._session.add(
                QueueItemRow(
                    id=int(item.id) if item.id else None,
                    song_id=int(item.song_id),
                    position=item.position,
                    status=item.status.value,
                    enqueued_at=item.enqueued_at,
                )
            )
        self._session.flush()

    def set_status(
        self,
        item_id: QueueItemId,
        status: QueueItemStatus,
        *,
        at: datetime | None = None,
    ) -> None:
        """Record a transition. `played_at` is set only by the move to `playing`."""

        values: dict[str, object] = {"status": status.value}
        if status is QueueItemStatus.PLAYING:
            values["played_at"] = at
        self._session.execute(
            update(QueueItemRow).where(QueueItemRow.id == int(item_id)).values(**values)
        )

    def remove(self, item_id: QueueItemId) -> bool:
        """Delete one item, reporting whether anything matched.

        The id rather than the position is the key, so the removal is stable under a
        queue that changed between the page rendering and the button press. Milestone 9
        decides what happens to the gap; `compact` is the tool.
        """

        return bool(
            _rowcount(
                self._session.execute(delete(QueueItemRow).where(QueueItemRow.id == int(item_id)))
            )
        )

    def clear(self, *, statuses: Sequence[QueueItemStatus] = (QueueItemStatus.PENDING,)) -> int:
        """Drop the waiting items, leaving whatever is playing alone by default.

        SAPRS 7.6's "clear queue" means "stop taking requests", not "disconnect the
        amplifier", and a repository that deleted the playing row would make the
        supervisor's next poll report a song that had already finished.
        """

        return int(
            _rowcount(
                self._session.execute(
                    delete(QueueItemRow).where(
                        QueueItemRow.status.in_(tuple(item.value for item in statuses))
                    )
                )
            )
        )

    def compact(self) -> int:
        """Renumber active items from 0, gap-free (SAPRS 7.6).

        Returns the number of rows touched. Called after a removal rather than on every
        read, because a queue that renumbered itself underneath a page being rendered
        is a queue whose buttons do the wrong thing.
        """

        touched = 0
        for position, row in enumerate(
            self._session.scalars(
                select(QueueItemRow)
                .where(QueueItemRow.status.in_((_PENDING, _PLAYING)))
                .order_by(QueueItemRow.position, QueueItemRow.id)
            ),
            start=1,
        ):
            if row.position != position:
                row.position = position
                touched += 1
        self._session.flush()
        return touched

    def finish_active(self, *, at: datetime | None = None) -> int:
        """Mark every playing item finished, and return how many there were.

        One row should match. More than one means two processes advanced the queue, and
        the count is returned rather than asserted so the caller can log it — a
        jukebox that crashes on a race is worse than one that plays the wrong next
        song.
        """

        statement = (
            update(QueueItemRow)
            .where(QueueItemRow.status == _PLAYING)
            .values(status=_FINISHED, played_at=at)
        )
        return int(_rowcount(self._session.execute(statement)))

    def waiting_since(self, *, now: datetime) -> timedelta:
        """How long the front of the queue has been waiting.

        The admin dashboard shows it and the empty queue must answer zero rather than
        raise, because "0:00" and "no data" are the same sentence to a volunteer
        running the event.
        """

        earliest = self._session.scalar(
            select(func.min(QueueItemRow.enqueued_at)).where(QueueItemRow.status == _PENDING)
        )
        return timedelta(0) if earliest is None else max(timedelta(0), now - earliest)

    def count(self) -> int:
        return int(self._session.scalar(select(func.count()).select_from(QueueItemRow)) or 0)

    def song_ids(self) -> list[SongId]:
        """Every song named by any row, active or not — the de-duplication index.

        Read into memory on start (SAPRS 12.4); the queue is thousands of rows at most
        even after a long event, and milestone 9 needs it whole anyway to rebuild its
        in-memory state.
        """

        values = self._session.scalars(select(QueueItemRow.song_id).order_by(QueueItemRow.position))
        return [SongId(int(value)) for value in values]


_PENDING: Final = QueueItemStatus.PENDING.value
_PLAYING: Final = QueueItemStatus.PLAYING.value
_FINISHED: Final = QueueItemStatus.FINISHED.value


def _to_item(row: QueueItemRow) -> QueueItem:
    return QueueItem(
        id=QueueItemId(int(row.id)),
        song_id=SongId(int(row.song_id)),
        position=int(row.position),
        status=QueueItemStatus(row.status),
        enqueued_at=row.enqueued_at,
        played_at=row.played_at,
    )
