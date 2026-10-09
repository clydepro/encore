"""SQLAlchemy ORM definitions for `runtime.db` (SAPRS 5.7, ADR-009).

These are the only ORM classes in Encore. SAPRS 3.3's split is the reason they stop at
the door: SQLAlchemy's type system and `create_all` are worth having for the
read/write side, where the queries are dynamic, the transactions matter and
concurrent writers exist. `library.db` is written by one program, once, with SQL
deliberately in front of a reviewer.

`created_at` and `updated_at` are `TIMESTAMP` rather than SQLite `TEXT` because
`server_default=func.now()` and `onupdate=func.now()` are what make the "did this row
change since I last looked" question one `SELECT` instead of a hash. AIP-2 is the
convention being followed; SAPRS 5.3 is the rule it violates, and the resolution is
`types.py` normalizing what comes back on the way into the domain.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, Index, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from encore.repositories.contract import RuntimeTable as RT
from encore.repositories.runtime.types import UTCDateTime

__all__ = [
    "AdminState",
    "Base",
    "PlaybackHistory",
    "QueueItemRow",
    "RuntimeStatistics",
    "SchemaVersion",
]

#: `sqlite_autoindex` on a `PRIMARY KEY` is the only index the runtime needs for its
#: hottest query (`WHERE status = 'playing'`), so the status column is indexed by
#: hand: SAPRS 12.1 expects a queue read inside 50 ms on a queue of hundreds.


class Base(DeclarativeBase):
    """The declarative base for the runtime models."""


class SchemaVersion(Base):
    """One row per applied migration (SAPRS 5.7).

    Kept in a table rather than in `PRAGMA user_version` so the history is queryable
    from the admin log view and survives a `VACUUM` without anyone remembering to
    re-read a pragma.
    """

    __tablename__ = RT.SCHEMA_VERSION

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default=func.now()
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)


class QueueItemRow(Base):
    """`queue_items` — SAPRS 4.5's queue entry as storage.

    `position` is the ordering, not `id`: the queue re-compacts positions after every
    removal (SAPRS 7.6), while ids stay stable so an HTTP endpoint can name an item
    that has already moved.
    """

    __tablename__ = RT.QUEUE_ITEMS

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    song_id: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    enqueued_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default=func.now()
    )
    #: When playback started with this item. Nullable because a pending item has not
    #: played, and because a skipped-then-removed item never will.
    played_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (Index("ix_queue_items_position", "position"),)


class PlaybackHistory(Base):
    """`playback_history` — one row per finished track.

    There is no foreign key to `songs`. SAPRS 5.7 states it outright: history
    references song ids, deliberately not foreign keys, because the library changes
    between builds and history must not disappear when it does.
    """

    __tablename__ = RT.PLAYBACK_HISTORY

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    song_id: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    completion: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: How the track ended. `PlaybackOutcome` values, stored as text for the same
    #: reason the queue stores its status: an operator reads this table.
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)


class RuntimeStatistics(Base):
    """`runtime_statistics` — counters by key and day (SAPRS 5.7, 10.3).

    `day` is `YYYY-MM-DD` in the appliance's local date, and `NULL` for a total that
    is not per-day. Statistics are the one place where a date rather than a timestamp
    is the right grain, because every question asked of them is "how many today".
    """

    __tablename__ = RT.RUNTIME_STATISTICS

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Empty string means "the all-time total", not NULL. SQLite's `UNIQUE` treats
    #: NULLs as distinct from one another, so a nullable `day` would let the upsert in
    #: `statistics.py` insert a second total row instead of updating the first.
    day: Mapped[str] = mapped_column(String(10), nullable=False, default="")

    __table_args__ = (Index("ix_runtime_statistics_key_day", "key", "day", unique=True),)


class AdminState(Base):
    """`admin_state` — the single administrative account (SAPRS 5.7, 12.3).

    No usernames. The table is keyed so that exactly one row can exist, which is the
    schema enforcing AIG 22's "do not implement guest accounts" and SAPRS 12.3's
    statement that there is one admin rather than a user table with a role column.
    """

    __tablename__ = RT.ADMIN_STATE

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    #: Sessions live in memory, so nothing here outlives a restart; the column exists
    #: to record that a login was accepted, which is the audit trail milestone 14 shows.
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
