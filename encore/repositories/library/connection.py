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
        self._immutable = immutable
        self._lock = threading.Lock()
        self._closed = False
        self._connection = self._connect(resolved)

    @property
    def path(self) -> Path:
        """The file this handle reads."""

        return self._path

    @property
    def immutable(self) -> bool:
        """Whether this handle was opened with `immutable=1` (see the class docstring).

        Read by `LibraryStore.reload`, which must not turn a reload into a silently
        different kind of handle: a connection that gains `immutable` would start trusting
        a file that can still change under it, and one that loses it costs a little locking
        nobody asked to pay for.
        """

        return self._immutable

    def reopen[T](self, verify: Callable[[LibraryConnection], T]) -> T:
        """Open this handle's own path again, and switch only if `verify` allows it.

        This is the whole of a library reload at the storage layer, and the *in place* part
        is the reason it is a method rather than a new object. Every repository in this
        package holds this connection, and `LibraryStore.search` — the object
        `SearchService` was handed at composition — holds it too. Replacing the store's
        attribute with a second connection would therefore leave songs reading the rebuilt
        library and search reading a handle nobody closed, which reports itself as "the
        library changed, and now search is dead". One handle, one door; the door changes
        what it points at.

        `verify` runs against the new handle while the old one is still live, which is what
        stops a reload making a broken file current. The Builder publishes atomically
        (SAPRS 5.8), so a shape check is how to tell "a new library" from "something else is
        now at that path", and the answer arrives before anything is swapped.

        Args:
            verify: Reads the new handle and returns what the caller wants back — in
                practice `store._check`, whose `LibraryInfo` becomes the store's own.

        Raises:
            StoreNotFoundError: There is no file at this path any more.
            Anything `verify` raises. The previous handle stays live in that case: a failed
                reload is a no-op with an exception, not a jukebox gone deaf (SAPRS 11.9).
        """

        if not self._path.is_file():
            raise StoreNotFoundError(
                self._path,
                hint="the library the reload was asked for is gone; the running library is "
                "still readable until it is published again (ADR-006)",
            )
        fresh = LibraryConnection(self._path, immutable=self._immutable)
        try:
            verdict = verify(fresh)
        except Exception:
            fresh.close()
            raise
        with self._lock:
            old, self._connection = self._connection, fresh._connection
            fresh._closed = True
            self._closed = False
        old.close()
        return verdict

    def _connect(self, path: Path) -> sqlite3.Connection:
        """Open the read-only handle ADR-009 specifies, with both refusals set.

        `query_only` is a no-op if the URI already refused, and the URI is a no-op if
        something attached this file by path. Setting both costs one statement each and
        removes the assumption that only one worked.
        """

        connection = sqlite3.connect(
            _read_only_uri(path, immutable=self._immutable),
            uri=True,
            check_same_thread=False,
            timeout=BUSY_TIMEOUT_MILLISECONDS / 1000,
        )
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MILLISECONDS}")
        connection.execute("PRAGMA query_only = ON")
        connection.row_factory = sqlite3.Row
        return connection

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
