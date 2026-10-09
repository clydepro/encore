"""The `runtime.db` store: engine, migrations, session-per-operation, repositories.

`RuntimeStore` is what `encore/services/container.py` builds and what every write in
Encore goes through. Its shape follows from ADR-009's second half — SQLAlchemy 2.x for
the mutable store, WAL, numbered migrations, a session per operation — and from one
observation about a jukebox: the operations that write are short and the ones that
matter are reads, so a session that lives for the length of one method call is both
simpler to reason about and harder to leak than one threaded through a request.

Sessions are handed to repositories per call rather than held, which is what makes the
same repository object safe to use from a request handler and from the event bus's
worker thread. The repositories take a session in their constructor, so the store
constructs them inside `unit_of_work()`; that is a two-line method rather than a
`with` statement the caller has to remember to nest correctly.

Nothing here opens `library.db`. The two databases never share a transaction and never
share a connection, and keeping the code that way is what makes "no runtime state
inside `library.db`" (AIG 22) a structural fact rather than a habit.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from encore.repositories.contract import LIBRARY_TABLES, RUNTIME_TABLES, RuntimeTable
from encore.repositories.errors import (
    RepositoryError,
    RuntimeContractError,
    StoreError,
    StoreNotFoundError,
)
from encore.repositories.runtime.admin_state import AdminStateRepository, PasswordHasher
from encore.repositories.runtime.history import PlaybackHistoryRepository
from encore.repositories.runtime.migrations import CURRENT_VERSION, UpgradeReport, upgrade
from encore.repositories.runtime.models import QueueItemRow
from encore.repositories.runtime.queue import QueueRepository
from encore.repositories.runtime.session import (
    RuntimeConfig,
    create_runtime_engine,
    session_factory,
)
from encore.repositories.runtime.statistics import RuntimeStatisticsRepository

__all__ = ["RuntimeStore", "RuntimeStoreInfo", "open_runtime_store"]


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeStoreInfo:
    """What the store is, for `/health` and the admin configuration summary."""

    path: Path
    schema_version: int
    expected_version: int
    journal_mode: str
    queue_length: int
    history_rows: int
    needs_upgrade: bool

    @property
    def current(self) -> bool:
        return self.schema_version == self.expected_version and not self.needs_upgrade


class RuntimeStore:
    """Every write Encore performs, behind one object.

    Args:
        engine: Built by `open_runtime_store`. Held for the store's lifetime; the pool
            inside it is what makes "session per operation" cheap.
        report: What `upgrade()` did, kept so `info()` can say whether this boot
            changed the schema. A store that could not report that is a store whose
            first-run behaviour is invisible in the log.
        hasher: Injected into the admin repository. Defaults to PBKDF2; milestone 14
            may pass an argon2 hasher without changing anything here.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        report: UpgradeReport | None = None,
        hasher: PasswordHasher | None = None,
    ) -> None:
        self._engine = engine
        self._factory = session_factory(engine)
        self._report = report
        self._hasher = hasher
        self._lock = threading.RLock()
        self._closed = False

    @property
    def engine(self) -> Engine:
        """Not for callers outside this package. See `LibraryConnection.raw`."""

        return self._engine

    @contextmanager
    def session(self) -> Iterator[Session]:
        """One transaction: commit on success, roll back on anything else.

        The rollback is the point. A half-written queue — an item marked playing before
        the previous one was marked finished — is a queue that will not advance, and it
        is reachable exactly when an exception lands between two statements.
        """

        if self._closed:
            raise StoreError("this runtime store is closed")
        session = self._factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def unit_of_work(self) -> Iterator[UnitOfWork]:
        """A transaction with repositories in it.

        ```python
        with store.unit_of_work() as work:
            work.queue.append(song.id)
            work.statistics.increment(StatisticKey.QUEUE_ADDED)
        ```

        Both statements land in one commit, which is what SAPRS 12.1 means by
        "transactional queue mutations": the count and the queue cannot disagree.
        """

        with self.session() as session:
            yield UnitOfWork(session, hasher=self._hasher)

    @property
    def info(self) -> RuntimeStoreInfo:
        path = Path(self._engine.url.database or "")
        with self.session() as session:
            version = int(session.scalar(text("SELECT MAX(version) FROM schema_version")) or 0)
            names = _names(session)
            missing = RUNTIME_TABLES - names
            queue_length = int(session.scalar(select(func.count()).select_from(QueueItemRow)) or 0)
            history_rows = int(
                # The table name is a contract constant, never input.
                session.scalar(text(f"SELECT COUNT(*) FROM {RuntimeTable.PLAYBACK_HISTORY}"))  # noqa: S608
                or 0
            )
            journal = str(session.scalar(text("PRAGMA journal_mode")) or "").lower()
        return RuntimeStoreInfo(
            path=path,
            schema_version=version,
            expected_version=CURRENT_VERSION,
            journal_mode=journal,
            queue_length=queue_length,
            history_rows=history_rows,
            needs_upgrade=bool(missing) or version < CURRENT_VERSION,
        )

    @property
    def report(self) -> UpgradeReport | None:
        return self._report

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._engine.dispose()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"RuntimeStore({str(self._engine.url.database)!r})"


class UnitOfWork:
    """The repositories bound to one transaction.

    Attributes are assigned rather than created on demand because a caller inside a
    `with` block that touched `work.queue` twice should get the same object: two
    `QueueRepository` instances over one session would each hold pending state and the
    second would see the first's flushed rows and be confused about which was truth.
    """

    def __init__(self, session: Session, *, hasher: PasswordHasher | None = None) -> None:
        self.session = session
        self.queue = QueueRepository(session)
        self.history = PlaybackHistoryRepository(session)
        self.statistics = RuntimeStatisticsRepository(session)
        self.admin_state = AdminStateRepository(session, hasher=hasher)


def open_runtime_store(
    path: Path | RuntimeConfig,
    *,
    create: bool = True,
    hasher: PasswordHasher | None = None,
) -> RuntimeStore:
    """Open `path`, migrate it forward, and return a store.

    Creating the file is legitimate here and only here: SAPRS 5.2 lets the runtime own
    `runtime.db` and forbids it touching `library.db`. A brand-new store is empty, and
    an empty queue at boot is correct behaviour rather than a symptom.

    Raises:
        StoreNotFoundError: `create=False` and there is no file.
        StoreError: The path is not a database, or WAL could not be enabled.
        RuntimeContractError: The schema is newer than this build (SAPRS 12.1).
    """

    config = (
        path if isinstance(path, RuntimeConfig) else RuntimeConfig(path=Path(path), create=create)
    )
    engine = create_runtime_engine(config)
    try:
        report = upgrade(engine)
        _assert_library_free(engine)
    except Exception:
        engine.dispose()
        raise
    return RuntimeStore(engine, report=report, hasher=hasher)


def _assert_library_free(engine: Engine) -> None:
    """Fail if this file also contains the library.

    The one way to check that two databases are two databases, and it costs one query
    at boot. A deployment that pointed `paths.library_db` and `paths.runtime_db` at the
    same file would otherwise surface as queue writes failing with `attempt to write a
    readonly database`, in a handler, at a party — and the message would say nothing
    about the configuration line that caused it.
    """

    with engine.connect() as connection:
        names = {
            str(row[0])
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            )
        }
    shared = LIBRARY_TABLES & names
    if shared:
        raise StoreError(
            "paths.runtime_db points at a file that contains "
            f"{', '.join(sorted(shared))}; the runtime database and the library are "
            "different files (SAPRS 5.7, ADR-009)"
        )


def _names(session: Session) -> frozenset[str]:
    rows = session.execute(text("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"))
    return frozenset(str(row[0]) for row in rows)


def wrap_failure(error: Exception, path: Path) -> RepositoryError:
    """Turn a driver exception into an operator-facing one (AEP 15).

    Used by the repositories' shared error handling. `SQLAlchemyError` is deliberately
    not unwrapped into a message containing SQL: a statement in a log is a statement in
    a support ticket, and the useful half of the message is the path.
    """

    if isinstance(error, SQLAlchemyError):
        text_message = str(error).lower()
        if "database is locked" in text_message:
            return StoreError(
                f"{path} is busy; another process is holding it open. Stop encore before "
                "rebuilding, and check for a copied database missing its -wal file"
            )
        if "readonly" in text_message:
            return StoreError(f"{path} is not writable; check the filesystem and permissions")
        if "no such table" in text_message:
            return RuntimeContractError(
                f"{path} is missing a table Encore expects; run encore-builder or restore "
                "runtime.db from the installer's backup"
            )
        return StoreError(f"cannot use {path}: {type(error).__name__}")
    if isinstance(error, OSError):
        return StoreNotFoundError(path, hint=str(error))
    return RepositoryError(str(error))
