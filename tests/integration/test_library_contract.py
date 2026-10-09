"""Every `SELECT` the Server can run, resolved against a database the Builder built.

This is ADR-009's mitigation, stated as a test. The ADR accepts hand-written SQL on the
read side and names the cost in the same paragraph — *"a column rename surfaces at
runtime, not in the type checker"* — and then prescribes the remedy: *one module naming
every column, plus a test that every SELECT resolves against a real built database*.
The first half is `encore/repositories/contract.py`. This file is the second half.

What makes it more than a syntax check is where the database comes from: the
`built_library` fixture runs the real pipeline over generated files, so the schema under
test is the one that ships, not a copy maintained here. A rename in
`apps/builder/schema.py` fails this suite in CI rather than failing a jukebox at a party.

The mechanism is SQLite's own name resolution. Preparing a statement resolves every
table and column it mentions; supplying no bindings then raises `ProgrammingError`, which
is the signal that parsing succeeded. Anything that comes back as `OperationalError` is a
name that does not exist, which is exactly the defect class this file exists to catch.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from apps.builder.pipeline import BuildOptions
from encore.repositories import contract
from encore.repositories.library import queries
from tests.conftest import BuiltLibrary

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _statements() -> Iterator[tuple[str, str]]:
    """Every complete statement `queries` publishes, by qualified name.

    Driven off `__all__` rather than `dir()`, because the module also holds fragments —
    the `SELECT` lists the search statements are built from — and a fragment is not a
    statement the Server can run. A constant missing from `__all__` is caught by
    `test_every_public_statement_is_listed`.
    """

    for name in sorted(queries.__all__):
        value = getattr(queries, name)
        if isinstance(value, str) and value.lstrip()[:6].upper() in {"SELECT", "WITH"}:
            yield name, value


@contextmanager
def _library(built_library: BuiltLibrary) -> Iterator[sqlite3.Connection]:
    """The published file, opened the way the Server opens it."""

    path = built_library.options.library_db
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        yield connection
    finally:
        connection.close()


def _prepare(connection: sqlite3.Connection, sql: str) -> None:
    """Ask SQLite to resolve the names in `sql` without needing its bindings."""

    statement = sql.replace("{ids}", "?")
    try:
        connection.execute(statement)
    except sqlite3.ProgrammingError:
        # "Incorrect number of bindings" arrives only after the statement has been
        # parsed and every name in it resolved, which is the answer we wanted.
        return


@pytest.mark.parametrize(("name", "sql"), sorted(_statements()))
def test_statement_resolves_against_the_published_library(
    built_library: BuiltLibrary, name: str, sql: str
) -> None:
    """Every name in every statement exists in the artifact the Builder shipped.

    `name` is in the parametrisation and so in the failure message, which is the whole
    reporting design: the test says which constant and which file it disagreeing with.
    """

    del name
    with _library(built_library) as connection:
        _prepare(connection, sql)


def test_every_public_statement_is_listed() -> None:
    """A statement defined and forgotten is a statement no one tests.

    `queries.py` is the module ADR-009 asks to be the single place SQL is written, so
    the check is that its public list is its whole content.
    """

    hidden = [
        name
        for name in dir(queries)
        if not name.startswith("_")
        and isinstance(getattr(queries, name), str)
        and getattr(queries, name).lstrip()[:6].upper() in {"SELECT", "WITH"}
        and name not in queries.__all__
    ]
    assert not hidden, f"add to queries.__all__: {hidden}"


def test_the_fixture_covers_every_canonical_table(built_library: BuiltLibrary) -> None:
    """A table nobody queries is a table nobody tests.

    Checked against `contract.COLUMNS`, so a new table fails here until a statement
    mentions it rather than waiting for someone to notice nothing reads it.
    """

    del built_library
    covered = {
        table
        for _, sql in _statements()
        for table in contract.COLUMNS
        if f"{table} " in sql or f"{table}\n" in sql or f".{table}" in sql or f"FROM {table}" in sql
    }
    assert covered | {contract.Table.LIBRARY_META} >= contract.LIBRARY_TABLES, (
        f"no SELECT mentions {sorted(contract.LIBRARY_TABLES - covered - {contract.Table.LIBRARY_META})}"
    )


def test_the_contract_names_every_column_the_builder_creates(built_library: BuiltLibrary) -> None:
    """`PRAGMA table_info` is the schema; the contract is a claim about it.

    Column *order* is compared too. It should not matter — every statement here names
    its columns — and it does matter for one thing: `SELECT *` in a diagnostic, where a
    reordered column silently changes what a value means.
    """

    path = built_library.options.library_db
    with sqlite3.connect(path) as connection:
        for table, expected in contract.COLUMNS.items():
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
            assert rows, f"{table} is missing from the built library"
            assert tuple(str(row[1]) for row in rows) == expected, table


def test_the_search_structures_exist_and_are_populated(
    built_library: BuiltLibrary, sqlite_fts5: None
) -> None:
    del sqlite_fts5
    path = built_library.options.library_db
    options: BuildOptions = built_library.options
    del options
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
        names = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            contract.Table.SONG_SEARCH,
            contract.Table.ALBUM_SEARCH,
            contract.Table.ARTIST_SEARCH,
        } <= names
        for table in (
            contract.Table.SONG_SEARCH,
            contract.Table.ALBUM_SEARCH,
            contract.Table.ARTIST_SEARCH,
        ):
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count > 0, f"{table} was built empty"


def test_runtime_cannot_write_the_library(
    built_library: BuiltLibrary, runtime_store: object
) -> None:
    """SAPRS 5.2 and AIG 22, enforced by SQLite rather than by a review comment.

    Two attempts, because there are two ways to get it wrong: through the store's own
    connection, and through a connection opened by path. `query_only` covers the first
    and `mode=ro` the second, and ADR-009's argument for using both is that neither
    alone covers a database reached the other way.
    """

    del runtime_store
    from encore.repositories.library import open_library

    store = open_library(built_library.options.library_db)
    try:
        with pytest.raises(sqlite3.OperationalError, match=r"readonly|attempt to write"):
            store._connection.raw.execute(
                f"INSERT INTO {contract.Table.ARTISTS} (id, name) VALUES (9999, 'nope')"
            )
    finally:
        store.close()

    with (
        sqlite3.connect(
            f"{built_library.options.library_db.as_uri()}?mode=ro", uri=True
        ) as connection,
        pytest.raises(sqlite3.OperationalError),
    ):
        connection.execute(f"INSERT INTO {contract.Table.ARTISTS} (id, name) VALUES (9998, 'nope')")


def test_the_views_are_views(built_library: BuiltLibrary) -> None:
    """SAPRS 5.4's denormalized structures are derived, so they cannot drift."""

    path = built_library.options.library_db
    with sqlite3.connect(path) as connection:
        kinds = {
            str(row[0]): str(row[1])
            for row in connection.execute("SELECT name, type FROM sqlite_master")
        }
    for view in contract.SEARCH_VIEWS:
        assert kinds.get(view) == "view", f"{view} should be a view, got {kinds.get(view)!r}"
