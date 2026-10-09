"""The schema contract: names, and only names (ADR-009, ADR-010).

ADR-010 gives the Library Builder sole ownership of the `library.db` DDL, and
ADR-009 gives the Server only `SELECT` statements. One thing both sides need and
neither may own twice is what the tables and columns are *called*. This module is
that: strings, no SQL, no connection, no behaviour — so `apps/builder/` can import it
without importing the Server's read path.

| Concern | Home |
| ------- | ---- |
| DDL, indexes, FTS5 definitions | `apps/builder/schema.py` — the only `CREATE` statements |
| Column names | here |
| `SELECT` statements | `encore/repositories/library/queries.py` |
| Shape and version checks | `encore/repositories/library/store.py` |

Two tests keep the arrangement honest. One asserts nothing in this module is SQL — a
`CREATE` appearing here means the schema has leaked into the read side. The other
resolves every name below against a database the Builder actually built, which is what
ADR-010 calls the contract test that makes a Builder change "loud rather than
surprising".

Normalization and deduplication rules are deliberately absent: SAPRS 6.5 gives those
to the Builder, and the entities in `encore.domain` carry their results.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

__all__ = [
    "ARTWORK_KINDS",
    "AUDIO_FORMAT_NAMES",
    "COLUMNS",
    "LIBRARY_SCHEMA_VERSION",
    "LIBRARY_TABLES",
    "METADATA_FIELDS",
    "METADATA_SOURCES",
    "META_BUILDER_VERSION",
    "META_BUILT_AT",
    "META_FILE_COUNT",
    "META_KEYS",
    "META_LIBRARY_VERSION",
    "META_RULES_VERSION",
    "META_SCHEMA_VERSION",
    "META_SONG_COUNT",
    "QUEUE_STATUSES",
    "RUNTIME_TABLES",
    "SEARCH_VIEWS",
    "SONG_SEARCH_COLUMNS",
    "AlbumColumn",
    "ArtistColumn",
    "ArtworkColumn",
    "FileColumn",
    "MetadataColumn",
    "MetadataField",
    "MetadataSource",
    "RuntimeTable",
    "Table",
    "columns_for",
]

#: The schema revision stamped into every built `library.db`. The Server refuses a
#: store whose stamp differs; a new schema is a new build, never a migration
#: (ADR-006, ADR-009).
LIBRARY_SCHEMA_VERSION: Final = 1

#: `library_meta` keys. `built_at` is ISO 8601 with an offset, like every timestamp
#: Encore stores (SAPRS 5.3).
META_LIBRARY_VERSION: Final = "library_version"
META_SCHEMA_VERSION: Final = "schema_version"
META_RULES_VERSION: Final = "normalization_rules_version"
META_BUILT_AT: Final = "built_at"
META_SONG_COUNT: Final = "song_count"
META_FILE_COUNT: Final = "file_count"
META_BUILDER_VERSION: Final = "builder_version"

META_KEYS: Final[frozenset[str]] = frozenset(
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


class Table:
    """Table names, as SAPRS 5.3 and 5.4 list them."""

    ARTISTS: Final = "artists"
    ALBUMS: Final = "albums"
    SONGS: Final = "songs"
    ARTWORK: Final = "artwork"
    MUSIC_FILES: Final = "music_files"
    METADATA: Final = "metadata"
    LIBRARY_META: Final = "library_meta"
    #: Derived FTS5 structures (SAPRS 5.4, 5.5). Listed apart from `LIBRARY_TABLES`
    #: because a store can be valid on a build of SQLite without FTS5.
    SONG_SEARCH: Final = "song_search"
    ALBUM_SEARCH: Final = "album_search"
    ARTIST_SEARCH: Final = "artist_search"


class ArtistColumn:
    """`artists` — SAPRS 4.2 as columns."""

    ID: Final = "id"
    NAME: Final = "name"
    SORT_NAME: Final = "sort_name"
    NORMALIZED_NAME: Final = "normalized_name"
    MUSICBRAINZ_ID: Final = "musicbrainz_id"
    ARTWORK_ID: Final = "artwork_id"


class AlbumColumn:
    """`albums` — SAPRS 4.3 as columns."""

    ID: Final = "id"
    ARTIST_ID: Final = "artist_id"
    TITLE: Final = "title"
    SORT_TITLE: Final = "sort_title"
    NORMALIZED_TITLE: Final = "normalized_title"
    MUSICBRAINZ_RELEASE_ID: Final = "musicbrainz_release_id"
    RELEASE_DATE: Final = "release_date"
    ARTWORK_ID: Final = "artwork_id"


class SongColumn:
    """`songs` — SAPRS 4.4, plus the originals SAPRS 6.5 requires be retained."""

    ID: Final = "id"
    TITLE: Final = "title"
    SORT_TITLE: Final = "sort_title"
    NORMALIZED_TITLE: Final = "normalized_title"
    ARTIST_ID: Final = "artist_id"
    ALBUM_ID: Final = "album_id"
    MUSIC_FILE_ID: Final = "music_file_id"
    TRACK_NUMBER: Final = "track_number"
    DISC_NUMBER: Final = "disc_number"
    GENRE: Final = "genre"
    DATE: Final = "date"
    MUSICBRAINZ_RECORDING_ID: Final = "musicbrainz_recording_id"
    ARTWORK_ID: Final = "artwork_id"
    ORIGINAL_ARTIST: Final = "original_artist"
    ORIGINAL_ALBUM: Final = "original_album"
    ORIGINAL_TITLE: Final = "original_title"


class FileColumn:
    """`music_files` — SAPRS 4.1, plus ADR-010's incremental key."""

    ID: Final = "id"
    PATH: Final = "path"
    FORMAT: Final = "format"
    DURATION_SECONDS: Final = "duration_seconds"
    SIZE_BYTES: Final = "size_bytes"
    MTIME_NS: Final = "mtime_ns"
    TAG_FINGERPRINT: Final = "tag_fingerprint"
    EMBEDDED_ARTWORK_SHA: Final = "embedded_artwork_sha"
    RULES_VERSION: Final = "rules_version"
    CACHE_KEY: Final = "cache_key"


class ArtworkColumn:
    """`artwork` — a file plus a stable reference, never a BLOB (SAPRS 5.6)."""

    ID: Final = "id"
    KIND: Final = "kind"
    RELATIVE_PATH: Final = "relative_path"
    WIDTH: Final = "width"
    HEIGHT: Final = "height"
    SHA256: Final = "sha256"
    SOURCE_PATH: Final = "source_path"


class MetadataColumn:
    """`metadata` — per-field provenance, mandatory per ADR-010."""

    ID: Final = "id"
    SONG_ID: Final = "song_id"
    FIELD: Final = "field"
    ORIGINAL: Final = "original"
    EFFECTIVE: Final = "effective"
    SOURCE: Final = "source"
    REPAIRED: Final = "repaired"


class RuntimeTable:
    """`runtime.db` tables (SAPRS 5.7), owned by `encore/repositories/runtime/`.

    Names live here for the same reason the library names do, and ADR-010 says it
    plainly: two applications with two names for one table is a defect. The DDL does
    not — it is in `runtime/migrations.py`, which is the only thing that creates these.
    """

    SCHEMA_VERSION: Final = "schema_version"
    QUEUE_ITEMS: Final = "queue_items"
    PLAYBACK_HISTORY: Final = "playback_history"
    RUNTIME_STATISTICS: Final = "runtime_statistics"
    ADMIN_STATE: Final = "admin_state"


class MetadataField:
    """The `field` values a provenance row may name."""

    ARTIST: Final = "artist"
    ALBUM: Final = "album"
    ALBUM_ARTIST: Final = "album_artist"
    TITLE: Final = "title"
    TRACK: Final = "track"
    DISC: Final = "disc"
    DATE: Final = "date"
    GENRE: Final = "genre"


METADATA_FIELDS: Final[frozenset[str]] = frozenset(
    {
        MetadataField.ARTIST,
        MetadataField.ALBUM,
        MetadataField.ALBUM_ARTIST,
        MetadataField.TITLE,
        MetadataField.TRACK,
        MetadataField.DISC,
        MetadataField.DATE,
        MetadataField.GENRE,
    }
)


class MetadataSource:
    """Where a value came from: ADR-010's four precedence levels, plus one."""

    TAG: Final = "tag"
    MUSICBRAINZ: Final = "musicbrainz"
    PATH: Final = "path"
    FILENAME: Final = "filename"
    #: A value the Builder supplied because nothing else could, such as the
    #: `[Unknown Album]` grouping title. Separate from `PATH` on purpose: a made-up
    #: value must never look like a read one.
    CONSTANT: Final = "constant"


METADATA_SOURCES: Final[frozenset[str]] = frozenset(
    {
        MetadataSource.TAG,
        MetadataSource.MUSICBRAINZ,
        MetadataSource.PATH,
        MetadataSource.FILENAME,
        MetadataSource.CONSTANT,
    }
)

#: `ArtworkKind` values (SAPRS 4.1, 6.7), mirrored as a `CHECK` in the Builder's DDL.
ARTWORK_KINDS: Final[frozenset[str]] = frozenset({"artist", "album", "song"})

#: `AudioFormat` values (SAPRS 1.2, 6.3), likewise mirrored in the DDL.
AUDIO_FORMAT_NAMES: Final[frozenset[str]] = frozenset({"mp3", "flac", "aac", "m4a"})

#: `QueueItemStatus` values (SAPRS 4.5), mirrored in `runtime.db`'s DDL.
QUEUE_STATUSES: Final[frozenset[str]] = frozenset(
    {"pending", "playing", "finished", "skipped", "removed"}
)

#: The canonical tables whose presence means "this is an Encore library" to the
#: startup shape check ADR-009 requires.
LIBRARY_TABLES: Final[frozenset[str]] = frozenset(
    {
        Table.ARTISTS,
        Table.ALBUMS,
        Table.SONGS,
        Table.ARTWORK,
        Table.MUSIC_FILES,
        Table.METADATA,
        Table.LIBRARY_META,
    }
)

#: The denormalized read structures (SAPRS 5.4). Views, so they cannot drift from the
#: authoritative tables underneath them.
SEARCH_VIEWS: Final[frozenset[str]] = frozenset({"song_details", "album_details"})

#: The FTS5 columns, in declaration order. Not part of `COLUMNS`: a virtual table
#: declares no types and `PRAGMA table_info` answers differently for it.
SONG_SEARCH_COLUMNS: Final[tuple[str, ...]] = ("title", "album", "artist")

_COLUMNS: dict[str, tuple[str, ...]] = {
    Table.ARTISTS: (
        ArtistColumn.ID,
        ArtistColumn.NAME,
        ArtistColumn.SORT_NAME,
        ArtistColumn.NORMALIZED_NAME,
        ArtistColumn.MUSICBRAINZ_ID,
        ArtistColumn.ARTWORK_ID,
    ),
    Table.ALBUMS: (
        AlbumColumn.ID,
        AlbumColumn.ARTIST_ID,
        AlbumColumn.TITLE,
        AlbumColumn.SORT_TITLE,
        AlbumColumn.NORMALIZED_TITLE,
        AlbumColumn.MUSICBRAINZ_RELEASE_ID,
        AlbumColumn.RELEASE_DATE,
        AlbumColumn.ARTWORK_ID,
    ),
    Table.SONGS: (
        SongColumn.ID,
        SongColumn.TITLE,
        SongColumn.SORT_TITLE,
        SongColumn.NORMALIZED_TITLE,
        SongColumn.ARTIST_ID,
        SongColumn.ALBUM_ID,
        SongColumn.MUSIC_FILE_ID,
        SongColumn.TRACK_NUMBER,
        SongColumn.DISC_NUMBER,
        SongColumn.GENRE,
        SongColumn.DATE,
        SongColumn.MUSICBRAINZ_RECORDING_ID,
        SongColumn.ARTWORK_ID,
        SongColumn.ORIGINAL_ARTIST,
        SongColumn.ORIGINAL_ALBUM,
        SongColumn.ORIGINAL_TITLE,
    ),
    Table.MUSIC_FILES: (
        FileColumn.ID,
        FileColumn.PATH,
        FileColumn.FORMAT,
        FileColumn.DURATION_SECONDS,
        FileColumn.SIZE_BYTES,
        FileColumn.MTIME_NS,
        FileColumn.TAG_FINGERPRINT,
        FileColumn.EMBEDDED_ARTWORK_SHA,
        FileColumn.RULES_VERSION,
        FileColumn.CACHE_KEY,
    ),
    Table.ARTWORK: (
        ArtworkColumn.ID,
        ArtworkColumn.KIND,
        ArtworkColumn.RELATIVE_PATH,
        ArtworkColumn.WIDTH,
        ArtworkColumn.HEIGHT,
        ArtworkColumn.SHA256,
        ArtworkColumn.SOURCE_PATH,
    ),
    Table.METADATA: (
        MetadataColumn.ID,
        MetadataColumn.SONG_ID,
        MetadataColumn.FIELD,
        MetadataColumn.ORIGINAL,
        MetadataColumn.EFFECTIVE,
        MetadataColumn.SOURCE,
        MetadataColumn.REPAIRED,
    ),
    Table.LIBRARY_META: ("key", "value"),
}

COLUMNS: Mapping[str, tuple[str, ...]] = MappingProxyType(_COLUMNS)


def columns_for(table: str) -> tuple[str, ...]:
    """Every column of `table`, in declaration order."""

    return COLUMNS[table]


#: Table names of `runtime.db`, owned by `encore/repositories/runtime/` and created by
#: its migrations rather than by the Builder (SAPRS 5.7, ADR-009).
RUNTIME_TABLES: Final[frozenset[str]] = frozenset(
    {
        RuntimeTable.SCHEMA_VERSION,
        RuntimeTable.QUEUE_ITEMS,
        RuntimeTable.PLAYBACK_HISTORY,
        RuntimeTable.RUNTIME_STATISTICS,
        RuntimeTable.ADMIN_STATE,
    }
)
