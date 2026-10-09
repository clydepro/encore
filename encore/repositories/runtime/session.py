"""Engine and session lifecycle for `runtime.db` (SAPRS 5.7, 12.1, ADR-009).

Three settings are load-bearing and all three are set here rather than in a config
file, because none of them is a preference:

* **WAL.** SAPRS 5.7 requires it. The Server writes queue mutations while the admin
  interface reads statistics, and rollback-journal SQLite blocks one of those
  completely. `PRAGMA journal_mode` is persistent, so it is set once on a fresh
  database and *verified* on every open: a store that silently came up in rollback
  mode is a store whose write-skew bug will be blamed on the queue.
* **`busy_timeout`.** From `RuntimeConfig`, and its default is not SQLite's: the
  five-second default exists so `SQLITE_BUSY` is rare, and a party where an admin
  saves configuration while a song finishes should see the retry, not the error.
* **`foreign_keys`.** SQLite leaves this OFF per connection, so it is switched on to
  match the intent rather than the default. It is a cheap insurance policy: the schema
  declares no cross-file relationships — `song_id` cannot be a foreign key, because
  `library.db` is a different file (SAPRS 5.7) — but a future table that *does*
  reference one of these would otherwise be unchecked, silently.

A connection pool, not a session, is what a FastAPI request needs; sessions come from
`session_factory()` and are short-lived. Nothing in this module knows about HTTP.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from encore.repositories.errors import StoreError, StoreNotFoundError

__all__ = [
    "RuntimeConfig",
    "RuntimeSession",
    "configure_wal",
    "create_runtime_engine",
    "open_runtime",
    "session_factory",
]

#: The journal mode we insist on. Anything else means WAL was unavailable (a
#: filesystem that cannot mmap, most often) and the application should say so rather
#: than discover write contention at a party.
WAL_MODE: str = "wal"


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeConfig:
    """How to open the write store.

    Attributes:
        path: `paths.runtime_db`. Created on first open, which is legitimate here and
            only here: `library.db` must never be created by the runtime (SAPRS 5.2).
        busy_timeout_ms: From `DatabaseConfig.runtime_busy_timeout_ms`.
        synchronous: `NORMAL` is SAPRS 5.7's choice and the right one under WAL, where
            SQLite fsyncs at checkpoints instead of at every commit. `FULL` is the
            answer for a device with no power protection and a slow boot.
        create: False turns a missing file into `StoreNotFoundError` instead of an
            empty database. A test or a read-only inspection wants False; boot does
            not, because migrations need the file to exist to version it.
    """

    path: Path
    busy_timeout_ms: int = 5_000
    synchronous: str = "NORMAL"
    create: bool = True
    echo: bool = False
    extra: dict[str, str] = field(default_factory=dict)


class RuntimeSession(Session):
    """A named session class, so a `Session` in a traceback says which store it came from."""


def create_runtime_engine(config: RuntimeConfig) -> Engine:
    """Build the engine, with the pragmas attached to the connection rather than to a caller.

    Raises:
        StoreNotFoundError: `create=False` and the file is not there.
        StoreError: The file exists but is not a database, or WAL could not be
            enabled. Both are reported with the path, because the fix is filesystem
            work and the operator needs to know which file.
    """

    path = Path(config.path).expanduser()
    if not config.create and not path.is_file():
        raise StoreNotFoundError(
            path, hint="runtime.db is created on first boot; check paths.runtime_db"
        )
    if path.exists() and path.stat().st_size and not _looks_like_sqlite(path):
        raise StoreError(f"{path} is not a SQLite database")

    engine = create_engine(
        _url(path),
        echo=config.echo,
        poolclass=StaticPool if _is_memory(path) else None,
        # The FastAPI threadpool and the event bus's publisher thread both open
        # sessions against one engine; the pool, not a lock, is what serialises their
        # writes (SAPRS 5.7). A shared in-memory database needs StaticPool on top of
        # that, or each connection would get its own empty database.
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection: object, record: object) -> None:
        del record
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute(f"PRAGMA busy_timeout = {int(config.busy_timeout_ms)}")
        cursor.execute(f"PRAGMA synchronous = {config.synchronous}")
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute(
            "PRAGMA temp_store = MEMORY" if _is_memory(path) else "PRAGMA cache_size = -8000"
        )
        for statement in config.extra.values():
            cursor.execute(statement)
        cursor.close()

    configure_wal(engine)
    return engine


def configure_wal(engine: Engine) -> str:
    """Enable WAL and prove it took.

    Returns the mode actually in force. `PRAGMA journal_mode = wal` answers with the
    resulting mode, which is how a read-only filesystem, a network share or an
    immutable container layer turns a silent downgrade into this one line.
    """

    with engine.connect() as connection:
        result = str(connection.execute(text("PRAGMA journal_mode = wal")).scalar_one())
    if result.lower() != WAL_MODE:
        raise StoreError(
            f"runtime.db could not be switched to WAL journaling (got {result!r}); "
            "WAL needs a filesystem that supports mmap, and SAPRS 5.7 requires it"
        )
    return result


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """The session factory the repositories are constructed with.

    `expire_on_commit=False` is not a performance tweak. The queue's read-modify-write
    cycles return domain objects built from a row's values after a commit; with the
    default, touching one of them would reissue a SELECT from inside a response, and
    the resulting "lazy load on a closed session" would be a 500 on a jukebox that is
    playing fine.
    """

    return sessionmaker(
        bind=engine,
        class_=RuntimeSession,
        expire_on_commit=False,
        autoflush=False,
    )


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One transaction. Commit on the way out, roll back on any exception (SAPRS 12.1).

    Repositories take a factory and not a session for this reason: the caller who
    begins a transaction should be the caller who ends it, and a repository method that
    committed on its own would make a two-step queue move impossible to make atomic.
    """

    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def open_runtime(config: RuntimeConfig) -> tuple[Engine, sessionmaker[Session]]:
    """Create the engine, apply migrations, and return the pair that opens a store.

    Kept separate from `store.py` so the dependency runs one way: the store needs
    migrations, migrations need a session, and neither needs the store.
    """

    from encore.repositories.runtime.migrations import upgrade  # noqa: PLC0415 - cycle otherwise

    engine = create_runtime_engine(config)
    upgrade(engine)
    return engine, session_factory(engine)


def _url(path: Path) -> str:
    """An absolute `sqlite:///` URL.

    A relative path would be resolved against the process working directory, which is
    where the "it worked in the test and empty in production" bug comes from.
    """

    if _is_memory(path):
        return "sqlite://"
    return f"sqlite:///{Path(path).resolve()}"


def _is_memory(path: Path) -> bool:
    return str(path) in {":memory:", "mode=memory", "file::memory:"}


def _looks_like_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def connect_failure(error: Exception, path: Path) -> StoreError:
    """Translate a driver error into one an operator can act on (AEP 15)."""

    if isinstance(error, sqlite3.OperationalError) and "locked" in str(error).lower():
        return StoreError(
            f"{path} is busy: another process is holding it open. Stop the server "
            "before rebuilding, and check for a database copied without its -wal file"
        )
    return StoreError(f"cannot open {path}: {error}")
