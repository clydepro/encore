"""Cross-database smoke test for the test infrastructure (PBK 16, SAPRS 5.1).

Exercises the separation the appliance depends on: one immutable library file
and one mutable runtime file, living side by side.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.support.sqlite import TempDatabase


def test_library_and_runtime_databases_stay_separate(
    library_db: TempDatabase,
    runtime_db: TempDatabase,
) -> None:
    assert library_db.path != runtime_db.path
    assert library_db.path.parent == runtime_db.path.parent

    library_write = library_db.connect()
    library_write.execute("CREATE TABLE songs (id INTEGER PRIMARY KEY, title TEXT)")
    library_write.execute("INSERT INTO songs (title) VALUES ('Published')")
    library_write.commit()
    library_write.close()

    runtime_write = runtime_db.connect()
    runtime_write.execute("CREATE TABLE queue (id INTEGER PRIMARY KEY, song_id INTEGER)")
    runtime_write.execute("INSERT INTO queue (song_id) VALUES (1)")
    runtime_write.commit()

    library_read = library_db.connect(readonly=True)
    assert library_read.execute("SELECT COUNT(*) FROM songs").fetchone()[0] == 1

    try:
        with pytest.raises(sqlite3.OperationalError):
            library_read.execute("INSERT INTO songs (title) VALUES ('Runtime drift')")
    finally:
        library_read.close()
        runtime_write.close()
