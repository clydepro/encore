"""The canonical `library.db` schema — the only place it is written (ADR-010).

Every `CREATE` statement for the library lives in this file. The Server holds
`SELECT` statements and no `Table` metadata, and `encore.repositories.contract`
holds the names as data, so there is exactly one definition to disagree with.
ADR-010's reasoning is worth keeping in mind when editing: a schema duplicated
into the read side is a schema that can drift there unnoticed, and the Builder is
the only component in the project that can break the Server's queries.

Three choices in here are load-bearing beyond storage:

* **`NOT NULL` and `CHECK` instead of Python validation.** `ai/HANDOFF.md`
  states the rule for this layer: prefer a DDL constraint to a Python check that
  a direct `INSERT` can bypass. The enum-like sets mirror
  `encore.repositories.contract`, which is the only way the domain enums and the
  constraints can be kept in step by test rather than by memory.
* **Contentless FTS5.** `content=''` means the index stores no copy of the text;
  searches resolve a `rowid` and join back to `songs`. That shrinks the artifact
  the Server opens on every start (5.6's argument about BLOBs, applied to
  inverted indexes) and is safe precisely because nothing here is ever updated.
  It also means selecting an FTS column returns NULL instead of the text, so
  every read has to join — which is enforced by a test rather than by memory,
  because a silent NULL in a search result is the kind of bug that reaches a
  guest's phone.
* **Two artist references per song.** `songs.artist_id` is the performing artist
  and `albums.artist_id` the grouping artist, because SAPRS 4.4 says the two
  legitimately disagree on compilations and a query that has to guess which one
  was meant is a query with a bug in it.
"""
# ruff: noqa: S608 — the only interpolated values are `contract.Table` and
# column constants, and a schema module that could not name a table in a string would
# have to build its own DDL out of fragments (ADR-009, ADR-010).

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from typing import Final

from encore.repositories.contract import (
    ARTWORK_KINDS,
    AUDIO_FORMAT_NAMES,
    LIBRARY_SCHEMA_VERSION,
    METADATA_FIELDS,
    METADATA_SOURCES,
    Table,
)

__all__ = [
    "META_BUILDER_VERSION",
    "META_BUILT_AT",
    "META_FILE_COUNT",
    "META_LIBRARY_VERSION",
    "META_RULES_VERSION",
    "META_SCHEMA_VERSION",
    "META_SONG_COUNT",
    "SCHEMA_STATEMENTS",
    "create_schema",
    "meta_keys",
    "meta_values",
    "stamp_meta",
    "write_meta",
]

#: `library_meta` keys. `built_at` is ISO 8601 with an offset, like every other
#: timestamp Encore stores (SAPRS 5.3, ADR-009).
META_LIBRARY_VERSION: Final = "library_version"
META_SCHEMA_VERSION: Final = "schema_version"
META_RULES_VERSION: Final = "normalization_rules_version"
META_BUILT_AT: Final = "built_at"
META_SONG_COUNT: Final = "song_count"
META_FILE_COUNT: Final = "file_count"
META_BUILDER_VERSION: Final = "builder_version"


def _in(values: Iterable[str]) -> str:
    """Render a name set as an SQL `IN` list, sorted so the DDL is stable.

    Everything interpolated here comes from `encore.repositories.contract`, so it
    is a project-controlled identifier rather than a value from a music file.
    Sorting keeps one definition of the schema byte-identical between runs, which
    is what lets the contract test diff the Builder's DDL against the Server's
    expectations instead of merely running both.
    """

    return "(" + ",".join(f"'{value}'" for value in sorted(values)) + ")"


SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE library_meta (
        key   TEXT NOT NULL PRIMARY KEY,
        value TEXT NOT NULL
    ) WITHOUT ROWID
    """,
    # SAPRS 5.6: artwork is a file plus a reference. The BLOB alternative is
    # rejected in ADR-010 because it inflates every copy of the library and
    # defeats content-addressed caching, so `relative_path` is the artifact.
    f"""
    CREATE TABLE artwork (
        id            INTEGER PRIMARY KEY,
        kind          TEXT    NOT NULL CHECK (kind IN {_in(ARTWORK_KINDS)}),
        relative_path TEXT    NOT NULL UNIQUE
                      CHECK (length(relative_path) > 0 AND instr(relative_path, '/') != 1),
        width         INTEGER NOT NULL DEFAULT 0 CHECK (width >= 0),
        height        INTEGER NOT NULL DEFAULT 0 CHECK (height >= 0),
        sha256        TEXT,
        source_path   TEXT
    )
    """,
    """
    CREATE TABLE artists (
        id              INTEGER PRIMARY KEY,
        name            TEXT    NOT NULL CHECK (length(trim(name)) > 0),
        sort_name       TEXT    NOT NULL DEFAULT '',
        normalized_name TEXT    NOT NULL,
        musicbrainz_id  TEXT,
        artwork_id      INTEGER REFERENCES artwork(id) ON DELETE SET NULL,
        UNIQUE (normalized_name)
    )
    """,
    """
    CREATE TABLE albums (
        id                    INTEGER PRIMARY KEY,
        artist_id             INTEGER NOT NULL REFERENCES artists(id) ON DELETE RESTRICT,
        title                 TEXT    NOT NULL CHECK (length(trim(title)) > 0),
        sort_title            TEXT    NOT NULL DEFAULT '',
        normalized_title      TEXT    NOT NULL,
        musicbrainz_release_id TEXT,
        release_date          TEXT,
        artwork_id            INTEGER REFERENCES artwork(id) ON DELETE SET NULL,
        UNIQUE (artist_id, normalized_title)
    )
    """,
    # The incremental key ADR-010 fixes: (path, size, mtime_ns, tag hash) plus
    # the normalization-rule version, so a rule change invalidates rather than
    # silently reuses. `cache_key` is those four written as text, kept because
    # the next build compares it and cannot re-derive it from the file.
    f"""
    CREATE TABLE music_files (
        id                     INTEGER PRIMARY KEY,
        path                   TEXT    NOT NULL UNIQUE,
        format                 TEXT    NOT NULL CHECK (format IN {_in(AUDIO_FORMAT_NAMES)}),
        duration_seconds       REAL    NOT NULL CHECK (duration_seconds > 0),
        size_bytes             INTEGER NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
        mtime_ns               INTEGER NOT NULL DEFAULT 0,
        tag_fingerprint        TEXT    NOT NULL DEFAULT '',
        embedded_artwork_sha   TEXT,
        rules_version          INTEGER NOT NULL DEFAULT 0,
        cache_key              TEXT    NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE songs (
        id                    INTEGER PRIMARY KEY,
        title                 TEXT    NOT NULL CHECK (length(trim(title)) > 0),
        sort_title            TEXT    NOT NULL DEFAULT '',
        normalized_title      TEXT    NOT NULL,
        artist_id             INTEGER NOT NULL REFERENCES artists(id) ON DELETE RESTRICT,
        album_id              INTEGER NOT NULL REFERENCES albums(id) ON DELETE RESTRICT,
        music_file_id         INTEGER NOT NULL UNIQUE REFERENCES music_files(id) ON DELETE RESTRICT,
        track_number          INTEGER CHECK (track_number IS NULL OR track_number >= 1),
        disc_number           INTEGER CHECK (disc_number IS NULL OR disc_number >= 1),
        genre                 TEXT,
        date                  TEXT,
        musicbrainz_recording_id TEXT,
        artwork_id            INTEGER REFERENCES artwork(id) ON DELETE SET NULL,
        original_artist       TEXT,
        original_album        TEXT,
        original_title        TEXT
    )
    """,
    # ADR-010 makes the per-field provenance record mandatory: it is the only way
    # to answer "why is this song under this artist?" after a four-level
    # precedence rule has decided. That question is asked at 11pm by someone
    # whose artist list looks wrong, so it is stored rather than logged.
    f"""
    CREATE TABLE metadata (
        id        INTEGER PRIMARY KEY,
        song_id   INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
        field     TEXT    NOT NULL CHECK (field IN {_in(METADATA_FIELDS)}),
        original  TEXT,
        effective TEXT,
        source    TEXT    NOT NULL CHECK (source IN {_in(METADATA_SOURCES)}),
        repaired  INTEGER NOT NULL DEFAULT 0 CHECK (repaired IN (0, 1)),
        UNIQUE (song_id, field)
    )
    """,
    # -- indexes for the navigation shapes SAPRS 9.5 and 9.7 need, sized for the
    # 15,000-song budget (SAPRS 1.8) rather than the 3,049-file corpus.
    "CREATE INDEX songs_by_album ON songs (album_id, disc_number, track_number, id)",
    "CREATE INDEX songs_by_artist ON songs (artist_id, normalized_title, id)",
    "CREATE INDEX songs_by_title ON songs (normalized_title, id)",
    "CREATE INDEX albums_by_artist ON albums (artist_id, sort_title, title)",
    "CREATE INDEX artists_by_sort ON artists (sort_name, name)",
    "CREATE INDEX metadata_by_song ON metadata (song_id)",
    # -- denormalized read structures (SAPRS 5.4). Views, so they cannot drift
    # from the authoritative tables above; the tables remain the source of truth.
    """
    CREATE VIEW song_details AS
    SELECT
        s.id                  AS id,
        s.title               AS title,
        s.sort_title          AS sort_title,
        s.normalized_title    AS normalized_title,
        s.artist_id           AS artist_id,
        s.album_id            AS album_id,
        s.music_file_id       AS music_file_id,
        s.track_number        AS track_number,
        s.disc_number         AS disc_number,
        s.genre               AS genre,
        s.date                AS date,
        s.musicbrainz_recording_id AS musicbrainz_recording_id,
        s.artwork_id          AS artwork_id,
        s.original_artist     AS original_artist,
        s.original_album      AS original_album,
        s.original_title      AS original_title,
        f.path                AS file_path,
        f.format              AS file_format,
        f.duration_seconds    AS duration_seconds,
        a.title               AS album_title,
        a.sort_title          AS album_sort_title,
        a.normalized_title    AS album_normalized_title,
        ar.name               AS artist_name,
        ar.sort_name          AS artist_sort_name,
        ar.normalized_name    AS artist_normalized_name
    FROM songs AS s
    JOIN music_files AS f ON f.id = s.music_file_id
    JOIN albums AS a ON a.id = s.album_id
    JOIN artists AS ar ON ar.id = s.artist_id
    """,
    """
    CREATE VIEW album_details AS
    SELECT
        a.id                  AS id,
        a.artist_id           AS artist_id,
        a.title               AS title,
        a.sort_title          AS sort_title,
        a.normalized_title    AS normalized_title,
        a.musicbrainz_release_id AS musicbrainz_release_id,
        a.release_date        AS release_date,
        a.artwork_id          AS artwork_id,
        ar.name               AS artist_name,
        ar.sort_name          AS artist_sort_name,
        COUNT(s.id)           AS song_count,
        COALESCE(SUM(f.duration_seconds), 0.0) AS duration_seconds
    FROM albums AS a
    JOIN artists AS ar ON ar.id = a.artist_id
    LEFT JOIN songs AS s ON s.album_id = a.id
    LEFT JOIN music_files AS f ON f.id = s.music_file_id
    GROUP BY a.id
    """,
    # -- FTS5 (SAPRS 5.5). Contentless: the index holds postings, and a match
    # resolves to a rowid that joins back to the authoritative row. `rowid` is
    # the entity id, assigned at insert, which is why there is no id column.
    # `prefix` serves token starts ("highwa" finds "Highway to Hell") and
    # `unicode61` splits on the punctuation artist names carry ("AC/DC").
    """
    CREATE VIRTUAL TABLE song_search USING fts5(
        title,
        album,
        artist,
        content='',
        tokenize='unicode61 remove_diacritics 2',
        prefix='2 3'
    )
    """,
    """
    CREATE VIRTUAL TABLE album_search USING fts5(
        album,
        artist,
        content='',
        tokenize='unicode61 remove_diacritics 2',
        prefix='2 3'
    )
    """,
    """
    CREATE VIRTUAL TABLE artist_search USING fts5(
        artist,
        sort_name,
        content='',
        tokenize='unicode61 remove_diacritics 2',
        prefix='2 3'
    )
    """,
)


def create_schema(connection: sqlite3.Connection) -> None:
    """Create the whole library in `connection`, foreign keys enforced.

    Called once, on an empty file in `paths.temp_dir` (ADR-010: never in place).
    `PRAGMA user_version` is stamped to the schema contract so a shape check does
    not have to read a table to reject a store from another Builder revision;
    `library_meta` carries the slower-moving facts a human asks about.
    """

    connection.execute("PRAGMA foreign_keys = ON")
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)
    connection.execute(f"PRAGMA user_version = {LIBRARY_SCHEMA_VERSION}")


def write_meta(connection: sqlite3.Connection, values: Mapping[str, str]) -> None:
    """Store the version stamp and build facts (ADR-006's publication contract).

    `INSERT OR REPLACE` rather than `INSERT`, because `stamp_meta` is also how a
    resumed run corrects its own record. This is the one write the Builder makes
    after the schema exists, and it happens before validation, never after.
    """

    connection.executemany(
        f"INSERT OR REPLACE INTO {Table.LIBRARY_META} (key, value) VALUES (?, ?)",
        sorted(values.items()),
    )


def meta_values(connection: sqlite3.Connection) -> dict[str, str]:
    """Read the stamp back, as `library_meta` holds it."""

    rows = connection.execute(
        f"SELECT key, value FROM {Table.LIBRARY_META} ORDER BY key"
    ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def stamp_meta(
    connection: sqlite3.Connection,
    *,
    library_version: str,
    rules_version: int,
    built_at: str,
    song_count: int,
    file_count: int,
    builder_version: str,
) -> None:
    """Write the stamp in the one shape the Server's check expects."""

    write_meta(
        connection,
        {
            META_LIBRARY_VERSION: library_version,
            META_SCHEMA_VERSION: str(LIBRARY_SCHEMA_VERSION),
            META_RULES_VERSION: str(rules_version),
            META_BUILT_AT: built_at,
            META_SONG_COUNT: str(song_count),
            META_FILE_COUNT: str(file_count),
            META_BUILDER_VERSION: builder_version,
        },
    )


def meta_keys() -> frozenset[str]:
    """Every key this module writes, for the contract test."""

    return frozenset(
        {
            META_LIBRARY_VERSION,
            META_SCHEMA_VERSION,
            META_RULES_VERSION,
            META_BUILT_AT,
            META_SONG_COUNT,
            META_FILE_COUNT,
            META_BUILDER_VERSION,
        }
    )
