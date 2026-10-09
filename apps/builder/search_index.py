"""Stage 8 — search optimization (SAPRS 5.4, 5.5, 6.2).

Fills the derived search structures and tunes the file for reading. The
denormalized tables and views are *defined* in `apps.builder.schema` — the Builder
owns the DDL, ADR-010 — and this stage is the only thing that puts data into them,
which is what "derived data, normalized tables authoritative" (SAPRS 5.4) means in
practice: the rows below are produced by a `SELECT` over the canonical tables and
could be regenerated from them by any build.

Three operations, in the order their cost matters:

* **Populate.** One `INSERT … SELECT` per index, joining through `song_details` so
  the text the guest searches is the text the artist row actually holds, not a copy
  normalization made earlier and forgot to update.
* **Optimize.** FTS5's `optimize` command merges the incrementally-built b-trees
  into one. On a 3,000-track build the difference is a few milliseconds; at 15,000
  it is the difference between the first search of a party answering inside
  SAPRS 1.8's 100 ms and answering outside it. Measured, not assumed — the number
  is asserted in `tests/performance/`.
* **ANALYZE.** Statistics for the query planner, so `WHERE album_id = ?` and
  `WHERE normalized_title = ?` are chosen from what the columns actually hold
  rather than from a guess.
"""
# ruff: noqa: S608 — the populated tables are named by `contract`, never by input.

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final

from encore.repositories.contract import Table

__all__ = ["SearchIndex", "optimize"]

#: One statement per FTS5 index, populating it from the denormalized view. `rowid`
#: is the entity id, which is what lets a search result join back without a second
#: lookup — and why the tables are contentless (see `schema.py`).
_POPULATE: Final[tuple[str, ...]] = (
    f"""
    INSERT INTO {Table.SONG_SEARCH} (rowid, title, album, artist)
    SELECT id, title, album_title, artist_name FROM song_details
    """,
    f"""
    INSERT INTO {Table.ALBUM_SEARCH} (rowid, album, artist)
    SELECT id, title, artist_name FROM album_details
    """,
    f"""
    INSERT INTO {Table.ARTIST_SEARCH} (rowid, artist, sort_name)
    SELECT id, name, sort_name FROM {Table.ARTISTS}
    """,
)

_OPTIMIZE: Final[tuple[str, ...]] = tuple(
    f"INSERT INTO {name}({name}) VALUES ('optimize')"
    for name in (Table.SONG_SEARCH, Table.ALBUM_SEARCH, Table.ARTIST_SEARCH)
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchIndex:
    """Row counts of the derived structures, taken from the index itself.

    A count read back from FTS rather than from the canonical tables is the only
    evidence that population happened; `validation.py` compares the two and calls
    the difference a finding (SAPRS 6.10, "search indexes").
    """

    songs: int = 0
    albums: int = 0
    artists: int = 0

    @property
    def total(self) -> int:
        return self.songs + self.albums + self.artists

    @property
    def empty(self) -> bool:
        return self.total == 0


def optimize(connection: sqlite3.Connection) -> SearchIndex:
    """Populate, merge and analyse the search structures of a built library."""

    for statement in _POPULATE:
        connection.execute(statement)
    for statement in _OPTIMIZE:
        connection.execute(statement)
    connection.execute("ANALYZE")
    return SearchIndex(
        songs=_count(connection, Table.SONG_SEARCH),
        albums=_count(connection, Table.ALBUM_SEARCH),
        artists=_count(connection, Table.ARTIST_SEARCH),
    )


def _count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
