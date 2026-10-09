"""One read-only SQLite connection, opened the way ADR-009 says to open it.

`LibraryConnection` owns the only handle to `library.db` in a running Server and
exposes it through one narrow door: `rows()`, `one()` and `scalar()` take a SQL
statement, run it, and apply a caller-supplied mapper to each row *inside this
package*. That shape is not ceremony. The boundary rule — "no `Session`, `Engine`,
`Row` or `Result` reaches a service, controller or template" (ADR-009) — is
impossible to enforce once a row object is returnable, and free to enforce when
the only way out is a function that turns it into a domain entity.

Read-only comes from the URI, not from anyone's good intentions. `mode=ro` makes
SQLite refuse to create the file and refuse to write it; `PRAGMA query_only`
covers a database that was attached some other way. Both are asserted by a test
that attempts an `INSERT` and expects SQLite, not a repository, to say no.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from encore.repositories.errors import ContractViolationError, StoreNotFoundError

__all__ = ["BUSY_TIMEOUT_MILLISECONDS", "LibraryConnection"]

#: How long a read waits on a writer before giving up. The Server never writes
#: here, so anything but an instant means another process holds the file open for
#: writing — which is the Builder publishing, and it is brief.
BUSY_TIMEOUT_MILLISECONDS: Final = 5_000


class LibraryConnection:
    """A `library.db` handle that cannot write.

    Args:
        path: The published library database.
        immutable: Add `immutable=1` to the URI. Off by default, and only correct
            where the file genuinely cannot change while open: it lets SQLite skip
            locking, which ADR-009 permits "where the platform supports it and the
            file is confirmed static", and it silently reads stale bytes if a
            republished `library.db` replaces the file under a live handle. A
            Server that answers `LibraryReloaded` by reopening may set it; one
            that keeps its handle must not.

    Raises:
        StoreNotFoundError: `path` is not a file. Checked before connecting,
            because `mode=ro` reports "unable to open database file", which names
            the symptom and not the typo the operator made. ADR-009 requires a
            shape check rather than relying on the open to fail "by accident".
    """

    def __init__(self, path: Path, *, immutable: bool = False) -> None:
        resolved = Path(path).expanduser()
        if not resolved.is_file():
            raise StoreNotFoundError(
                resolved,
                hint=(
                    "publish a library with the Builder first; the server never "
                    "creates library.db (SAPRS 5.2, ADR-006)"
                ),
            )
        self._path = resolved
        self._lock = threading.Lock()
        self._closed = False
        self._connection = sqlite3.connect(
            _read_only_uri(resolved, immutable=immutable),
            uri=True,
            check_same_thread=False,
            timeout=BUSY_TIMEOUT_MILLISECONDS / 1000,
        )
        # `query_only` is a no-op if the URI already refused, and the URI is a
        # no-op if something attached this file by path. Setting both costs one
        # statement each and removes the assumption that only one worked.
        self._connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MILLISECONDS}")
        self._connection.execute("PRAGMA query_only = ON")
        self._connection.row_factory = sqlite3.Row

    @property
    def path(self) -> Path:
        """The file this handle reads."""

        return self._path

    @property
    def raw(self) -> sqlite3.Connection:
        """The connection, for this package's shape checks only.

        Not part of the intended surface: anything holding this can call
        `execute`, and only `query_only` stands between that and `library.db`.
        """

        return self._connection

    def close(self) -> None:
        """Release the file handle. Idempotent, so a failed startup cannot leak it."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    def __enter__(self) -> LibraryConnection:
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()

    def rows[T](
        self,
        sql: str,
        mapper: Callable[[sqlite3.Row], T],
        params: Sequence[object] = (),
    ) -> list[T]:
        """Run a read and map every row on the way out."""

        return [self._row_to(mapper, row) for row in self._execute(sql, params)]

    def one[T](
        self,
        sql: str,
        mapper: Callable[[sqlite3.Row], T],
        params: Sequence[object] = (),
    ) -> T | None:
        """Run a read that must return at most one row, and map it.

        Raises:
            ContractViolationError: More than one row came back, which means the
                statement was not the one its caller thought it was.
        """

        rows = self._execute(sql, params)
        if not rows:
            return None
        if len(rows) > 1:
            raise ContractViolationError("library", "row count", got=f"{len(rows)} rows for one")
        return self._row_to(mapper, rows[0])

    def scalar(self, sql: str, params: Sequence[object] = ()) -> object | None:
        """Run a read and return the first column of the first row, unmapped.

        For counts and existence checks, where constructing a domain object just
        to discard it would cost more than it saves.
        """

        rows = self._execute(sql, params)
        return None if not rows else rows[0][0]

    def count(self, sql: str, params: Sequence[object] = ()) -> int:
        """A `SELECT COUNT(*)` as the number it stands for.

        `scalar()` hands back whatever SQLite bound into that column, and every call site
        that converts it says `int(value or 0)`. This is that line once, typed, so a
        repository cannot get the conversion wrong and mypy does not have to be told a
        count is a number four times over.
        """

        rows = self._execute(sql, params)
        if not rows:
            return 0
        value = rows[0][0]
        return int(value) if isinstance(value, (int, str, float)) else 0

    def table_names(self) -> frozenset[str]:
        """Every table and view in the store, for the startup shape check."""

        rows = self._execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
            "AND name NOT LIKE 'sqlite_%'"
        )
        return frozenset(str(row[0]) for row in rows)

    def _execute(self, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        with self._lock:
            cursor = self._connection.execute(sql, tuple(params))
            try:
                return list(cursor.fetchall())
            finally:
                cursor.close()

    def _row_to[T](self, mapper: Callable[[sqlite3.Row], T], row: sqlite3.Row) -> T:
        try:
            return mapper(row)
        except KeyError as error:
            # A missing key is not a query problem, it is the column-name
            # contract: the Builder renamed something the SELECT list still uses
            # (or the reverse), which ADR-009 lists as the cost of hand-written
            # SQL and which must not surface as a bare KeyError.
            raise ContractViolationError("library", str(error.args[0]).strip("'")) from error


def _read_only_uri(path: Path, *, immutable: bool) -> str:
    """The `file:` URI ADR-009 specifies for this store.

    `as_uri()` does the percent-encoding a music path needs — apostrophes, spaces,
    `#` — that a hand-concatenated URI would silently truncate at.
    """

    uri = path.absolute().as_uri()
    return f"{uri}?mode=ro&immutable=1" if immutable else f"{uri}?mode=ro"
