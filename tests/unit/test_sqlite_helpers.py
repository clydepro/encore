"""Tests for the temporary SQLite helpers (PBK 16)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.support.sqlite import TempDatabase, fts5_available, table_names, temp_database


def test_temp_database_creates_and_removes_file(tmp_path: Path) -> None:
    with temp_database(tmp_path, "runtime.db") as database:
        assert database.path.exists()
        assert database.size_bytes() > 0
    assert not database.path.exists()


def test_temp_database_applies_statements_and_setup(tmp_path: Path) -> None:
    def seed(connection: sqlite3.Connection) -> None:
        connection.execute("INSERT INTO probe (value) VALUES (1)")

    with temp_database(
        tmp_path,
        "probe.db",
        statements=["CREATE TABLE probe (value INTEGER)"],
        setup=seed,
    ) as database:
        connection = database.connect()
        try:
            assert table_names(connection) == {"probe"}
            row = connection.execute("SELECT value FROM probe").fetchone()
            assert row[0] == 1
        finally:
            connection.close()


def test_readonly_connection_cannot_write(library_db: TempDatabase) -> None:
    """`library.db` is immutable at runtime (ADR-006); the helper enforces it."""

    writable = library_db.connect()
    writable.execute("CREATE TABLE songs (id INTEGER PRIMARY KEY, title TEXT)")
    writable.execute("INSERT INTO songs (title) VALUES ('Test')")
    writable.commit()
    writable.close()

    readonly = library_db.connect(readonly=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            readonly.execute("INSERT INTO songs (title) VALUES ('Nope')")
        with pytest.raises(sqlite3.OperationalError):
            readonly.execute("DROP TABLE songs")
    finally:
        readonly.close()


def test_runtime_connection_enforces_foreign_keys(runtime_db: TempDatabase) -> None:
    connection = runtime_db.connect()
    try:
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))")
        connection.execute("INSERT INTO parent (id) VALUES (1)")
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO child (parent_id) VALUES (99)")
    finally:
        connection.close()


def test_fts5_probe_reports_availability(tmp_path: Path) -> None:
    """SQLite FTS5 is a hard requirement (AIG 11); the probe must not lie."""

    connection = sqlite3.connect(tmp_path / "probe.db")
    try:
        available = fts5_available(connection)
        assert isinstance(available, bool)
        if available:
            connection.execute("CREATE VIRTUAL TABLE probe USING fts5(title)")
            assert "probe" in table_names(connection)
    finally:
        connection.close()
