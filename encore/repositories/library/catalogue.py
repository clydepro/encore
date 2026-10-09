"""Artist and album reads (SAPRS 4.2, 4.3, 9.5).

Two aggregates in one module because they are one idea — the catalogue's identity
layer, which every song row points into — and because separating `artists.py` from
`albums.py` would produce two files whose entire content is a name and four `SELECT`s.
`songs.py` is its own module for the opposite reason: it has six access paths and the
queue depends on all of them.

The one visible decision is why browsing by name is not search. An artist page is a
lookup; a search that matched "The" against every artist beginning with it would be
correct for search and useless for navigation, which is SAPRS 6.5's distinction
between normalized values that compare and display values that show.
"""

from __future__ import annotations

from collections.abc import Sequence

from encore.domain import Album, AlbumId, Artist, ArtistId
from encore.repositories.library import queries
from encore.repositories.library.connection import LibraryConnection
from encore.repositories.library.mappers import to_album, to_artist

__all__ = ["AlbumRepository", "ArtistRepository"]


class ArtistRepository:
    """Reads `artists`."""

    def __init__(self, connection: LibraryConnection) -> None:
        self._connection = connection

    def by_id(self, artist_id: ArtistId) -> Artist | None:
        return self._connection.one(queries.ARTIST_BY_ID, to_artist, (int(artist_id),))

    def by_ids(self, ids: Sequence[ArtistId]) -> list[Artist]:
        """Batch read for the album and song lists that name their artist.

        Empty input short-circuits rather than building `IN ()`, which is a syntax
        error and would otherwise be a crash on a page with no results.
        """

        if not ids:
            return []
        values = [int(item) for item in ids]
        statement = queries.ARTISTS_BY_IDS.replace("{ids}", ", ".join("?" for _ in values))
        return self._connection.rows(statement, to_artist, values)

    def page(self, *, limit: int, offset: int = 0) -> list[Artist]:
        """Artists in browse order: sort key, then display name."""

        return self._connection.rows(queries.ARTISTS_PAGE, to_artist, (limit, offset))

    def all(self) -> list[Artist]:
        """Every artist, for the alphabetical index and for the build report.

        `-1` is SQLite's "no limit" for `LIMIT`, which is why there is no second
        statement: a `SELECT` without `ORDER BY` would read differently on a
        different SQLite build, and this list is shown to a human.
        """

        return self.page(limit=-1)

    def count(self) -> int:
        return self._connection.count(queries.ARTIST_COUNT)


class AlbumRepository:
    """Reads `albums`."""

    def __init__(self, connection: LibraryConnection) -> None:
        self._connection = connection

    def by_id(self, album_id: AlbumId) -> Album | None:
        return self._connection.one(queries.ALBUM_BY_ID, to_album, (int(album_id),))

    def by_ids(self, ids: Sequence[AlbumId]) -> list[Album]:
        if not ids:
            return []
        values = [int(item) for item in ids]
        statement = queries.ALBUMS_BY_IDS.replace("{ids}", ", ".join("?" for _ in values))
        return self._connection.rows(statement, to_album, values)

    def by_artist(self, artist_id: ArtistId) -> list[Album]:
        """An artist's discography, sort-ordered (SAPRS 9.5).

        The `ORDER BY` is on `sort_title` rather than `title` so that *The Final
        Countdown* files under F, which is the difference between a discography and a
        list that looks broken.
        """

        return self._connection.rows(queries.ALBUMS_BY_ARTIST, to_album, (int(artist_id),))

    def page(self, *, limit: int, offset: int = 0) -> list[Album]:
        return self._connection.rows(queries.ALBUMS_PAGE, to_album, (limit, offset))

    def all(self) -> list[Album]:
        return self.page(limit=-1)
