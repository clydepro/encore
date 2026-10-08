"""Temporary SQLite helpers (PBK 16).

Encore uses two SQLite databases: an immutable `library.db` and a mutable
`runtime.db`. Tests need fast, isolated, throwaway databases of both shapes
without importing any Encore schema — schema ownership belongs to the
repositories and the Library Builder milestones.

These helpers therefore deal in *databases and connections*, not tables.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: Statement used to prove FTS5 is compiled into the local SQLite build.
FTS5_PROBE: Final[str] = "CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)"


class UnsupportedSQLiteBuildError(RuntimeError):
    """Raised when the interpreter's SQLite lacks a feature Encore requires."""


def fts5_available(connection: sqlite3.Connection) -> bool:
    """Return True when the FTS5 extension is usable (SAPRS 5.5, AIG 11)."""

    try:
        connection.execute(FTS5_PROBE)
    except sqlite3.OperationalError:
        return False
    connection.execute("DROP TABLE IF EXISTS _fts5_probe")
    connection.commit()
    return True


@dataclass(frozen=True)
class TempDatabase:
    """A database file in a pytest `tmp_path`, plus connection helpers."""

    path: Path
    journal_mode: str = "MEMORY"

    def connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        """Open a connection configured the way Encore configures its own."""

        if readonly:
            connection = sqlite3.connect(self.path.as_uri() + "?mode=ro&immutable=1", uri=True)
        else:
            connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA journal_mode = {self.journal_mode}")
        return connection

    def size_bytes(self) -> int:
        return self.path.stat().st_size


@contextmanager
def temp_database(
    directory: Path,
    name: str = "test.db",
    *,
    setup: Callable[[sqlite3.Connection], None] | None = None,
    statements: Sequence[str] = (),
) -> Iterator[TempDatabase]:
    """Create a disposable SQLite database, then guarantee it is removed.

    Args:
        directory: Existing directory to place the file in (use `tmp_path`).
        name: Database filename, e.g. `library.db` or `runtime.db`.
        setup: Optional callable run once against a fresh connection.
        statements: Optional DDL/DML run before `setup`.

    Yields:
        A `TempDatabase` whose `path` lives for the duration of the block.
    """

    db = TempDatabase(path=directory / name)
    connection = db.connect()
    try:
        for statement in statements:
            connection.execute(statement)
        if setup is not None:
            setup(connection)
        connection.commit()
        # VACUUM materialises an empty-but-valid database file: sqlite3 creates
        # a zero-byte placeholder on connect, which is not a usable database.
        connection.execute("VACUUM")
    finally:
        connection.close()

    try:
        yield db
    finally:
        connection = db.connect()
        try:
            connection.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(f"{db.path}{suffix}")
                candidate.unlink(missing_ok=True)


def require_fts5(connection: sqlite3.Connection) -> None:
    """Skip-friendly guard for tests that depend on FTS5."""

    if not fts5_available(connection):
        raise UnsupportedSQLiteBuildError(
            "This Python/SQLite build has no FTS5; search tests cannot run."
        )


def table_names(connection: sqlite3.Connection) -> set[str]:
    """Return user table names — handy for structural assertions."""

    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
        "AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row[0]) for row in rows}
