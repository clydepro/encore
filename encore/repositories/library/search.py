"""FTS5 reads, as a repository and nothing more (SAPRS 5.5, 11.1, 11.2).

Milestone 7 owns search as a *feature* — query compilation, ranking policy, the
100 ms budget, the service. What lives here is the storage access that feature needs:
hand a MATCH expression to SQLite, get rows back. The split is the one ADR-009 draws
for every other store too, and it is why no `encore.search` module is imported from
this one.

Ranking is expressed once, in `queries.py`, as bm25 weights. A repository that
re-ordered results in Python would both lose the index's work and make the weights a
lie.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from encore.domain import Album, Artist, Song
from encore.repositories.errors import SearchUnavailableError
from encore.repositories.library import queries
from encore.repositories.library.connection import LibraryConnection
from encore.repositories.library.mappers import to_album, to_artist, to_song

__all__ = ["AlbumHit", "ArtistHit", "LibrarySearch", "SongHit"]


@dataclass(frozen=True, slots=True, kw_only=True)
class SongHit:
    """One matched song, with the score that placed it.

    `score` is SQLite's bm25 value, lower being better, passed through unchanged. It
    is a *relative* number and nothing should treat two builds' scores as comparable;
    the field exists so a UI can show why an order is what it is, which is the only
    honest use of it.
    """

    song: Song
    score: float


@dataclass(frozen=True, slots=True, kw_only=True)
class AlbumHit:
    album: Album
    score: float


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtistHit:
    artist: Artist
    score: float


@runtime_checkable
class SearchRead(Protocol):
    """The capability milestone 7's `SearchService` needs from storage.

    Declared as a protocol so the service can be built against it and tested with a
    fake, while the real implementation remains a repository (AIG 4: services never
    touch SQLite).
    """

    def songs(self, match: str, *, limit: int) -> Sequence[SongHit]: ...

    def albums(self, match: str, *, limit: int) -> Sequence[AlbumHit]: ...

    def artists(self, match: str, *, limit: int) -> Sequence[ArtistHit]: ...


class LibrarySearch:
    """`song_search`, `album_search` and `artist_search`, read through one connection.

    Args:
        connection: The same read-only handle everything else in this package uses.
        available: Whether the FTS tables exist. `store.py` decides by listing
            `sqlite_master`, because a SQLite built without FTS5 is a supported way to
            lose this feature and the appliance should still boot (SAPRS 11.2).
    """

    def __init__(self, connection: LibraryConnection, *, available: bool = True) -> None:
        self._connection = connection
        self._available = available

    def songs(self, match: str, *, limit: int) -> list[SongHit]:
        self._require()
        return self._connection.rows(queries.SONG_SEARCH, _song_hit, (match, limit))

    def albums(self, match: str, *, limit: int) -> list[AlbumHit]:
        self._require()
        return self._connection.rows(queries.ALBUM_SEARCH, _album_hit, (match, limit))

    def artists(self, match: str, *, limit: int) -> list[ArtistHit]:
        self._require()
        return self._connection.rows(queries.ARTIST_SEARCH, _artist_hit, (match, limit))

    def _require(self) -> None:
        """Refuse a search the store cannot run, in words milestone 7 can show.

        Silently returning nothing would be indistinguishable from a library with no
        matches, which is the failure mode SAPRS 11.2 tells us to avoid.
        """

        if not self._available:
            raise SearchUnavailableError


def _song_hit(row: sqlite3.Row) -> SongHit:
    return SongHit(song=to_song(row), score=_score(row))


def _album_hit(row: sqlite3.Row) -> AlbumHit:
    return AlbumHit(album=to_album(row), score=_score(row))


def _artist_hit(row: sqlite3.Row) -> ArtistHit:
    return ArtistHit(artist=to_artist(row), score=_score(row))


def _score(row: sqlite3.Row) -> float:
    """bm25 for one row, selected alongside the columns rather than re-evaluated.

    Asking SQLite twice would let two calls disagree about an order they both claim to
    describe.
    """

    return float(row["score"])
