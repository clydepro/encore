"""The read-only `library.db` store: one connection, the catalogue's repositories, and
the startup shape check ADR-009 requires (SAPRS 5.2, 5.3, 12.4).

`open()` is the only door. It refuses a missing file, a file SQLite would rather not
open, and a file that opens but is not an Encore library — which is the same
distinction ADR-009 draws about `create_engine`: both `sqlite3.connect()` and
`create_engine()` happily *create* an empty database, so a typo in `paths.library_db`
would otherwise boot an appliance that reports zero songs and no error. The shape check
is what makes that a startup failure with a sentence in it.

Everything downstream of `open()` is a read. The connection is `mode=ro` and
`query_only`, so "the runtime never modifies `library.db`" (AIG 4, SAPRS 5.2) is a
property of the file handle rather than of everyone remembering not to call `commit`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from encore.domain import Album, Artist, Artwork, Metadata, Song, SongId
from encore.repositories.contract import (
    LIBRARY_SCHEMA_VERSION,
    LIBRARY_TABLES,
    META_BUILT_AT,
    META_KEYS,
    META_LIBRARY_VERSION,
    META_RULES_VERSION,
    META_SCHEMA_VERSION,
    META_SONG_COUNT,
    Table,
)
from encore.repositories.errors import (
    ContractViolationError,
    LibraryContractError,
)
from encore.repositories.library.catalogue import AlbumRepository, ArtistRepository
from encore.repositories.library.connection import LibraryConnection
from encore.repositories.library.media import (
    ArtworkRepository,
    MusicFileRepository,
    ProvenanceRepository,
)
from encore.repositories.library.queries import LIBRARY_META, SONGS_BY_IDS
from encore.repositories.library.search import LibrarySearch
from encore.repositories.library.songs import SongRepository

__all__ = ["LibraryInfo", "LibraryStore", "open_library"]

#: Tables whose absence means "this is not an Encore library". The FTS tables are not
#: in the set: `search.py` reports their absence as a search-specific failure rather
#: than refusing to boot a jukebox that can still play everything it knows.
REQUIRED_TABLES: Final[frozenset[str]] = LIBRARY_TABLES


@dataclass(frozen=True, slots=True, kw_only=True)
class LibraryInfo:
    """What `library_meta` says, typed. Shown by the admin configuration summary.

    Attributes:
        path: The file this store opened, for the log line that explains a refusal.
        library_version: The release the library was built for (`paths.library_version`).
        schema_version: The structural revision; must equal `LIBRARY_SCHEMA_VERSION`.
        rules_version: Which normalization rules produced the values on screen, so a
            report can say *why* an artist name changed between builds.
        built_at: ISO 8601, exactly as stored.
        song_count: The Builder's own tally, which `counts()` re-derives so a
            disagreement is visible instead of silent.
    """

    path: Path
    library_version: str
    schema_version: int
    rules_version: str
    built_at: str
    song_count: int
    fts_available: bool

    @property
    def usable(self) -> bool:
        """Whether anything should be read from a store this badly shaped."""

        return self.schema_version == LIBRARY_SCHEMA_VERSION


class LibraryStore:
    """Reads `library.db` as domain entities. Construct with `open_library`."""

    def __init__(self, connection: LibraryConnection, info: LibraryInfo) -> None:
        self._connection = connection
        self._info = info
        self.songs = SongRepository(connection)
        self.artists = ArtistRepository(connection)
        self.albums = AlbumRepository(connection)
        self.artwork = ArtworkRepository(connection)
        self.files = MusicFileRepository(connection)
        self.provenance = ProvenanceRepository(connection)
        # Composed here rather than constructed by callers: `LibrarySearch` needs the
        # same connection *and* the FTS verdict from `library_meta`'s inspection, and a
        # milestone 7 service assembling the pair would be one place to get
        # `available` wrong. `SearchService` takes this object.
        self.search = LibrarySearch(connection, available=info.fts_available)

    @property
    def info(self) -> LibraryInfo:
        return self._info

    @property
    def path(self) -> Path:
        return self._connection.path

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> LibraryStore:
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()

    # -- catalogue ------------------------------------------------------

    def song(self, song_id: SongId) -> Song | None:
        return self.songs.by_id(song_id)

    def songs_by_ids(self, ids: Iterable[SongId] | None) -> list[Song]:
        return self.songs.by_ids(tuple(ids or ()))

    def statement_for_ids(self, count: int) -> str:
        """The batch song read, with `count` placeholders.

        Public because the queue needs it and building SQL in a service is the exact
        thing AIG 22 forbids; this stays inside the repository layer either way.
        """

        if count <= 0:
            raise ValueError("a batch read needs at least one id")
        return SONGS_BY_IDS.replace("{ids}", ", ".join("?" for _ in range(count)))

    def artist(self, artist_id: object) -> Artist | None:
        return self.artists.by_id(artist_id)  # type: ignore[arg-type]

    def album(self, album_id: object) -> Album | None:
        return self.albums.by_id(album_id)  # type: ignore[arg-type]

    def artwork_for_song(self, song_id: SongId) -> Artwork | None:
        return self.artwork.for_song(song_id)

    def metadata_for(self, song_id: SongId) -> Metadata | None:
        """The tags behind a song, originals and sources included (SAPRS 4.1, 6.5)."""

        return self.files.metadata_for(song_id)

    # -- facts ----------------------------------------------------------

    def counts(self) -> Mapping[str, int]:
        """Live row counts, keyed by table name.

        Computed rather than read from `library_meta` because the two disagreeing is
        the interesting case, and a store that reported only its own stamp could not
        show it (`validation.py` fails a build on the disagreement).
        """

        tables = (Table.ARTISTS, Table.ALBUMS, Table.SONGS, Table.MUSIC_FILES, Table.ARTWORK)
        return {
            table: self._connection.count(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
            for table in tables
        }

    def fts_available(self) -> bool:
        """Whether the search tables are present, checked rather than assumed.

        A Pi built against a SQLite without FTS5 is a supported way to lose this
        feature, and the answer changes what `/api/v1/search` says rather than whether
        the appliance boots (SAPRS 11.2).
        """

        return self._info.fts_available

    def introspect(self, sql: str) -> list[str]:
        """Column names a statement would return, without running it.

        Exists for one thing: `tests/integration/test_library_contract.py` prepares
        every statement in this package and compares the columns to the contract. That
        check is what ADR-009 asks for and cannot be expressed as a unit test, because
        it needs the real schema in front of it.
        """

        cursor = self._connection.raw.execute(sql)
        return [str(column[0]) for column in cursor.description or []]

    def table_names(self) -> frozenset[str]:
        return self._connection.table_names()


def open_library(path: Path, *, immutable: bool = False) -> LibraryStore:
    """Open `path`, check its shape, and return a store that cannot write to it.

    Raises:
        StoreNotFoundError: No such file — publish a library first.
        LibraryContractError: A database that is not an Encore library, or is one from
            a schema revision this Server cannot read.
    """

    connection = LibraryConnection(path, immutable=immutable)
    try:
        info = _check(connection)
    except Exception:
        connection.close()
        raise
    return LibraryStore(connection, info)


def _check(connection: LibraryConnection) -> LibraryInfo:
    missing = REQUIRED_TABLES - connection.table_names()
    if missing:
        raise LibraryContractError(
            f"{connection.path} is not an Encore library: no {' and '.join(sorted(missing))} "
            "table. Build it with encore-builder (SAPRS 6.1)."
        )
    meta = _meta(connection)
    if not meta:
        raise LibraryContractError(
            f"{connection.path} has an empty library_meta; the Builder stamps one on"
            " every published artifact (SAPRS 5.3)."
        )
    schema_version = _integer(meta.get(META_SCHEMA_VERSION), META_SCHEMA_VERSION)
    if schema_version != LIBRARY_SCHEMA_VERSION:
        raise LibraryContractError(
            f"{connection.path} was built for schema version {schema_version} and this "
            f"server reads {LIBRARY_SCHEMA_VERSION}; rebuild the library (ADR-006: the "
            "runtime does not migrate it)."
        )
    return LibraryInfo(
        path=connection.path,
        library_version=str(meta.get(META_LIBRARY_VERSION) or "unknown"),
        schema_version=schema_version,
        rules_version=str(meta.get(META_RULES_VERSION) or "unknown"),
        built_at=str(meta.get(META_BUILT_AT) or ""),
        song_count=_integer(meta.get(META_SONG_COUNT), META_SONG_COUNT),
        fts_available=_has_fts(connection),
    )


def _meta(connection: LibraryConnection) -> Mapping[str, str]:
    rows = connection.raw.execute(LIBRARY_META).fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows if str(row["key"]) in META_KEYS}


def _integer(value: object, name: str) -> int:
    if value is None:
        return 0
    try:
        return int(str(value))
    except ValueError as error:
        raise ContractViolationError("library_meta", name, got=value) from error


def _has_fts(connection: LibraryConnection) -> bool:
    names = connection.table_names()
    return (
        Table.SONG_SEARCH in names and Table.ALBUM_SEARCH in names and Table.ARTIST_SEARCH in names
    )
