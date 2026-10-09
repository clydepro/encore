"""Repository failures an operator can act on (AEP 15, ADR-009).

There is exactly one failure mode in this package that has no other answer, and
the ADR-009 decision names it: `sqlite3.connect()` and `create_engine()` both
*create* a missing file and hand back a working, empty database. A typo in
`paths.library_db` therefore boots an appliance that reports zero songs and no
error — the only silent failure in the storage layer, and the one a party cannot
recover from. These exceptions exist so that case raises instead of shrugging.

Everything else a repository can hit is a contract violation: a store built by a
different Builder revision, a row whose shape the mappers were not written for,
or a `runtime.db` needing a downgrade nobody has written.

Messages are written for the admin log view (milestone 14) and `journalctl`, so
they name the file and say what to do, and they never quote a traceback or a
SQL statement (AEP 15).
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "ContractViolationError",
    "LibraryContractError",
    "RepositoryError",
    "RuntimeContractError",
    "SearchUnavailableError",
    "StoreError",
    "StoreNotFoundError",
]


class RepositoryError(Exception):
    """Base class for every storage failure raised outside this package."""


class StoreError(RepositoryError):
    """A database is present but is not the database Encore expected."""


class StoreNotFoundError(RepositoryError):
    """The configured path is not an Encore database at all.

    Attributes:
        path: The file that was missing. Surfaced as data because the
            administrative interface shows it next to the configuration summary
            (SAPRS 12.3) rather than parsing the message.
    """

    def __init__(self, path: Path, *, hint: str) -> None:
        super().__init__(f"no Encore database at {path}: {hint}")
        self.path = path


class LibraryContractError(StoreError):
    """`library.db` failed the shape or version check (ADR-009).

    The Builder is the only writer this file has ever had, so a mismatch means
    the artifact came from a different Builder revision, was truncated, or was
    edited by hand. All three answers are the same: rebuild and republish.
    """


class RuntimeContractError(StoreError):
    """`runtime.db` needs a migration Encore cannot or will not apply.

    Raised for a schema newer than the application (a downgrade nobody has
    written, which the installer must refuse rather than guess at) and for a
    store whose version row disagrees with its tables.
    """


class SearchUnavailableError(StoreError):
    """Search was asked of a store with no usable index.

    Distinct from "no results". A SQLite built without FTS5, or a library published by
    a Builder that could not create the tables, both answer this way, and SAPRS 11.2 is
    explicit that the difference matters to the person who has to fix it.
    """

    def __init__(self, detail: str = "this library has no search index") -> None:
        super().__init__(f"{detail}; rebuild the library with encore-builder")


class ContractViolationError(RepositoryError):
    """A row arrived that the mappers were not written for.

    Deliberately distinct from a corrupt database: the file opened fine and the
    query ran, so this is our contract breaking — a column renamed on one side of
    the Builder/Server boundary. That is a programming error and says so.
    """

    def __init__(self, table: str, column: str, *, got: object = None) -> None:
        detail = "" if got is None else f", got {got!r}"
        super().__init__(
            f"{table}.{column} did not match the type the mapper expects{detail} "
            "(SAPRS 5.3: the library schema is a contract between the Builder and the Server)"
        )
        self.table = table
        self.column = column
