"""Every `SELECT` the Server runs against `library.db` (ADR-009).

ADR-009 accepts hand-written SQL and names its own mitigation in the same paragraph:
*"a column rename surfaces at runtime, not in the type checker. Mitigation: one module
naming every column, plus a test that every SELECT resolves against a real built
database."* The names live in `encore.repositories.contract`, the statements live here,
and `tests/integration/test_library_contract.py` executes every constant in this module
against a library the Builder actually built. That test is the only reason the
arrangement is safe rather than brave.

Column lists are composed from the contract rather than typed out. Sixty columns
copied from a schema somebody once read is a second definition of the schema, which is
the drift ADR-010 exists to prevent.

No `INSERT`, `UPDATE` or `DELETE` appears below, and that is not a convention: SAPRS 5.2
forbids them, `connection.py` opens the file `mode=ro`, and
`test_runtime_cannot_write_the_library` asks SQLite to prove the refusal.
"""

from __future__ import annotations

from encore.repositories.contract import AlbumColumn as AC
from encore.repositories.contract import ArtistColumn as AR
from encore.repositories.contract import ArtworkColumn as AW
from encore.repositories.contract import FileColumn as FC
from encore.repositories.contract import SongColumn as SC
from encore.repositories.contract import Table

# ruff: noqa: S608 — every interpolated value is a constant from `contract.py`, and the
# contract test resolves the assembled string against a real built database.

__all__ = [
    "ALBUMS_BY_ARTIST",
    "ALBUMS_BY_IDS",
    "ALBUMS_PAGE",
    "ALBUM_BY_ID",
    "ALBUM_SEARCH",
    "ARTISTS_BY_IDS",
    "ARTISTS_PAGE",
    "ARTIST_BY_ID",
    "ARTIST_COUNT",
    "ARTIST_SEARCH",
    "ARTWORK_BY_ID",
    "ARTWORK_FOR_SONG",
    "FILE_BY_ID",
    "LIBRARY_META",
    "PROVENANCE_BY_SONG",
    "PROVENANCE_SOURCES",
    "SONGS_BY_ALBUM",
    "SONGS_BY_ARTIST",
    "SONGS_BY_IDS",
    "SONGS_PAGE",
    "SONG_BY_ID",
    "SONG_COUNT",
    "SONG_METADATA",
    "SONG_SEARCH",
    "TABLE_NAMES",
]

#: The song read path is a join, so it is written once. The denormalized structures of
#: SAPRS 5.4 are what `song_details` is for; spelling the join here rather than in six
#: places keeps navigation inside SAPRS 1.8's 200 ms budget by construction rather
#: than by each caller getting it right.
_SONG_FROM = (
    f"FROM {Table.SONGS} AS s JOIN {Table.MUSIC_FILES} AS f ON f.{FC.ID} = s.{SC.MUSIC_FILE_ID}"
)

_SONG_SELECT = (
    f"SELECT s.{SC.ID}, s.{SC.TITLE}, s.{SC.SORT_TITLE}, s.{SC.ARTIST_ID}, s.{SC.ALBUM_ID},"
    f" s.{SC.MUSIC_FILE_ID}, s.{SC.TRACK_NUMBER}, s.{SC.DISC_NUMBER}, s.{SC.GENRE},"
    f" s.{SC.DATE}, s.{SC.MUSICBRAINZ_RECORDING_ID}, s.{SC.ARTWORK_ID},"
    f" f.{FC.PATH}, f.{FC.FORMAT}, f.{FC.DURATION_SECONDS}"
)

SONG_BY_ID = f"{_SONG_SELECT} {_SONG_FROM} WHERE s.{SC.ID} = ?"

SONGS_BY_ALBUM = (
    f"{_SONG_SELECT} {_SONG_FROM} WHERE s.{SC.ALBUM_ID} = ?"
    f" ORDER BY s.{SC.DISC_NUMBER}, s.{SC.TRACK_NUMBER}, s.{SC.ID}"
)

#: Artist browsing lists the *performing* artist, hence the predicate on
#: `songs.artist_id` rather than through the album (SAPRS 4.4, 9.5). A compilation's
#: tracks belong on the contributor's page.
SONGS_BY_ARTIST = f"{_SONG_SELECT} {_SONG_FROM} WHERE s.{SC.ARTIST_ID} = ? ORDER BY s.{SC.ID}"

SONGS_PAGE = f"{_SONG_SELECT} {_SONG_FROM} ORDER BY s.{SC.ID} LIMIT ? OFFSET ?"

#: `{ids}` is expanded to the right number of `?` by the repository, never by string
#: interpolation of the ids themselves.
SONGS_BY_IDS = f"{_SONG_SELECT} {_SONG_FROM} WHERE s.{SC.ID} IN ({{ids}}) ORDER BY s.{SC.ID}"

ARTIST_BY_ID = (
    f"SELECT {AR.ID}, {AR.NAME}, {AR.SORT_NAME}, {AR.NORMALIZED_NAME}, {AR.MUSICBRAINZ_ID},"
    f" {AR.ARTWORK_ID} FROM {Table.ARTISTS} WHERE {AR.ID} = ?"
)

ARTISTS_PAGE = (
    f"SELECT {AR.ID}, {AR.NAME}, {AR.SORT_NAME}, {AR.NORMALIZED_NAME}, {AR.MUSICBRAINZ_ID},"
    f" {AR.ARTWORK_ID} FROM {Table.ARTISTS} ORDER BY {AR.SORT_NAME}, {AR.NAME} LIMIT ? OFFSET ?"
)

ARTISTS_BY_IDS = (
    f"SELECT {AR.ID}, {AR.NAME}, {AR.SORT_NAME}, {AR.NORMALIZED_NAME}, {AR.MUSICBRAINZ_ID},"
    f" {AR.ARTWORK_ID} FROM {Table.ARTISTS} WHERE {AR.ID} IN ({{ids}})"
)

ARTIST_COUNT = f"SELECT COUNT(*) FROM {Table.ARTISTS}"

SONG_COUNT = f"SELECT COUNT(*) FROM {Table.SONGS}"

_ALBUM_SELECT = (
    f"SELECT {AC.ID}, {AC.ARTIST_ID}, {AC.TITLE}, {AC.SORT_TITLE}, {AC.NORMALIZED_TITLE},"
    f" {AC.MUSICBRAINZ_RELEASE_ID}, {AC.RELEASE_DATE}, {AC.ARTWORK_ID}"
)

ALBUM_BY_ID = f"{_ALBUM_SELECT} FROM {Table.ALBUMS} WHERE {AC.ID} = ?"

ALBUMS_BY_ARTIST = (
    f"{_ALBUM_SELECT} FROM {Table.ALBUMS} WHERE {AC.ARTIST_ID} = ?"
    f" ORDER BY {AC.SORT_TITLE}, {AC.TITLE}"
)

ALBUMS_PAGE = f"{_ALBUM_SELECT} FROM {Table.ALBUMS} ORDER BY {AC.ID} LIMIT ? OFFSET ?"

ALBUMS_BY_IDS = f"{_ALBUM_SELECT} FROM {Table.ALBUMS} WHERE {AC.ID} IN ({{ids}})"

ARTWORK_BY_ID = (
    f"SELECT {AW.ID}, {AW.KIND}, {AW.RELATIVE_PATH}, {AW.WIDTH}, {AW.HEIGHT}"
    f" FROM {Table.ARTWORK} WHERE {AW.ID} = ?"
)

#: The song's own cover, else its album's (SAPRS 6.7). The fallback belongs in SQL
#: rather than in a template or a service because it is a fact about how the Builder
#: attaches artwork, and it takes the song id twice: once for each branch.
ARTWORK_FOR_SONG = (
    f"SELECT {AW.ID}, {AW.KIND}, {AW.RELATIVE_PATH}, {AW.WIDTH}, {AW.HEIGHT} FROM {Table.ARTWORK}"
    f" WHERE {AW.ID} = COALESCE("
    f"(SELECT s.{SC.ARTWORK_ID} FROM {Table.SONGS} AS s WHERE s.{SC.ID} = ?),"
    f"(SELECT al.{AC.ARTWORK_ID} FROM {Table.SONGS} AS s"
    f" JOIN {Table.ALBUMS} AS al ON al.{AC.ID} = s.{SC.ALBUM_ID} WHERE s.{SC.ID} = ?))"
)

PROVENANCE_SOURCES = f"SELECT DISTINCT source FROM {Table.METADATA} ORDER BY source"

FILE_BY_ID = (
    f"SELECT {FC.ID}, {FC.PATH}, {FC.FORMAT}, {FC.DURATION_SECONDS}, {FC.SIZE_BYTES}"
    f" FROM {Table.MUSIC_FILES} WHERE {FC.ID} = ?"
)

#: Provenance for one song (SAPRS 6.5, ADR-010). Read alongside the song rather than
#: joined into it: the relation is one-to-many and a join would multiply the row.
PROVENANCE_BY_SONG = (
    "SELECT field, original, effective, source, repaired"
    f" FROM {Table.METADATA} WHERE song_id = ? ORDER BY field"
)

#: The wide song read behind `Metadata` (SAPRS 4.1). Two joins' worth of work, which is
#: why it is not part of the browse path: nothing in a guest request needs the
#: album artist's MusicBrainz id, and the admin page that does can wait 1 ms.
SONG_METADATA = (
    f"SELECT s.{SC.ID}, s.{SC.TITLE}, s.{SC.ARTIST_ID}, s.{SC.ALBUM_ID}, s.{SC.TRACK_NUMBER},"
    f" s.{SC.DISC_NUMBER}, s.{SC.GENRE}, s.{SC.DATE}, s.{SC.MUSICBRAINZ_RECORDING_ID},"
    f" s.{SC.ORIGINAL_ARTIST}, s.{SC.ORIGINAL_ALBUM}, s.{SC.ORIGINAL_TITLE},"
    f" f.{FC.DURATION_SECONDS}, al.{AC.MUSICBRAINZ_RELEASE_ID}, al.{AC.RELEASE_DATE},"
    f" ar.{AR.MUSICBRAINZ_ID}"
    f" {_SONG_FROM}"
    f" JOIN {Table.ALBUMS} AS al ON al.{AC.ID} = s.{SC.ALBUM_ID}"
    f" JOIN {Table.ARTISTS} AS ar ON ar.{AR.ID} = al.{AC.ARTIST_ID}"
    " WHERE s.id = ?"
)

LIBRARY_META = f"SELECT key, value FROM {Table.LIBRARY_META} ORDER BY key"

TABLE_NAMES = (
    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'"
)

# -- search structures (SAPRS 5.4, 5.5) ------------------------------------

#: Contentless FTS5 stores no text, so a match resolves to a `rowid` that joins back to
#: the authoritative row. The `rowid` is qualified because an unqualified one is
#: ambiguous in a join, and the table is deliberately *not* aliased: SQLite resolves the
#: left side of `MATCH` as a table name, so `WHERE x MATCH ?` on an aliased FTS table
#: reads "column x" and fails with "no such column". That is ADR-009's measured trap.
#:
#: The bm25 weights put the title above the album above the artist: a guest typing
#: "hell" wants *Highway to Hell* on the first line, not every artist with "Hell" in
#: a name.
SONG_SEARCH = (
    f"{_SONG_SELECT}, bm25({Table.SONG_SEARCH}) AS score {_SONG_FROM}"
    f" JOIN {Table.SONG_SEARCH} ON {Table.SONG_SEARCH}.rowid = s.{SC.ID}"
    f" WHERE {Table.SONG_SEARCH} MATCH ?"
    f" ORDER BY bm25({Table.SONG_SEARCH}, 10.0, 1.0, 1.0), s.title LIMIT ?"
)

ALBUM_SEARCH = (
    f"SELECT al.{AC.ID}, al.{AC.ARTIST_ID}, al.{AC.TITLE}, al.{AC.SORT_TITLE},"
    f" al.{AC.NORMALIZED_TITLE}, al.{AC.MUSICBRAINZ_RELEASE_ID}, al.{AC.RELEASE_DATE},"
    f" al.{AC.ARTWORK_ID}, bm25({Table.ALBUM_SEARCH}) AS score"
    f" FROM {Table.ALBUMS} AS al"
    f" JOIN {Table.ALBUM_SEARCH} ON {Table.ALBUM_SEARCH}.rowid = al.{AC.ID}"
    f" WHERE {Table.ALBUM_SEARCH} MATCH ?"
    f" ORDER BY bm25({Table.ALBUM_SEARCH}, 5.0, 1.0), al.{AC.TITLE} LIMIT ?"
)

ARTIST_SEARCH = (
    f"SELECT a.{AR.ID}, a.{AR.NAME}, a.{AR.SORT_NAME}, a.{AR.NORMALIZED_NAME},"
    f" a.{AR.MUSICBRAINZ_ID}, a.{AR.ARTWORK_ID}, bm25({Table.ARTIST_SEARCH}) AS score"
    f" FROM {Table.ARTISTS} AS a"
    f" JOIN {Table.ARTIST_SEARCH} ON {Table.ARTIST_SEARCH}.rowid = a.{AR.ID}"
    f" WHERE {Table.ARTIST_SEARCH} MATCH ?"
    f" ORDER BY bm25({Table.ARTIST_SEARCH}, 5.0, 1.0), a.{AR.NAME} LIMIT ?"
)
