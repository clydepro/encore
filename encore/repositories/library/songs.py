"""Song reads (SAPRS 4.4, 5.3, 9.5, 9.6).

No rules, no `Session`, no row objects escaping: every method returns
`encore.domain.Song` or nothing (ADR-009's boundary). The only judgement visible here
is ordering, and it is the ordering the catalogue pages show — disc, then track, then
id, so a two-disc release reads as 1-9, 10-18 rather than interleaved.
"""

from __future__ import annotations

from collections.abc import Sequence

from encore.domain import AlbumId, ArtistId, Song, SongId
from encore.repositories.library import queries
from encore.repositories.library.connection import LibraryConnection
from encore.repositories.library.mappers import to_song

__all__ = ["SongRepository"]


class SongRepository:
    """Reads `songs`, joined with the file each one plays."""

    def __init__(self, connection: LibraryConnection) -> None:
        self._connection = connection

    def by_id(self, song_id: SongId) -> Song | None:
        return self._connection.one(queries.SONG_BY_ID, to_song, (int(song_id),))

    def by_ids(self, ids: Sequence[SongId]) -> list[Song]:
        """Batch read for the queue, which names several songs on one screen.

        Returns catalogue order rather than the caller's order. The queue carries its
        own position, and a repository that permuted results to match an input list
        would be doing presentation work.
        """

        if not ids:
            return []
        values = [int(item) for item in ids]
        placeholders = ", ".join("?" for _ in values)
        statement = queries.SONGS_BY_IDS.replace("{ids}", placeholders)
        return self._connection.rows(statement, to_song, values)

    def by_album(self, album_id: AlbumId) -> list[Song]:
        return self._connection.rows(queries.SONGS_BY_ALBUM, to_song, (int(album_id),))

    def by_artist(self, artist_id: ArtistId) -> list[Song]:
        return self._connection.rows(queries.SONGS_BY_ARTIST, to_song, (int(artist_id),))

    def page(self, *, limit: int, offset: int = 0) -> list[Song]:
        """Every song, in the order the All Songs list uses.

        Ordered by id because the Builder assigns ids in catalogue order, which keeps
        this a plain index scan on a 15,000-row table instead of a sort. `search.py`
        covers the other case: a guest who wants a list they can type into.
        """

        return self._connection.rows(queries.SONGS_PAGE, to_song, (limit, offset))

    def count(self) -> int:
        return self._connection.count(queries.SONG_COUNT)
