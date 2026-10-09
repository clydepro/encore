"""Artwork, file and provenance reads (SAPRS 4.1, 5.6, 6.5, 6.7).

These are the reads a *page* needs after it has the entity it was asked for: an image
reference, the file behind a song, the evidence behind a value. All three are separate
queries rather than joins into the catalogue reads, and that is a latency decision:
`/api/v1/songs` with 15,000 rows stays inside SAPRS 1.8's 200 ms precisely because it
does not join in an artwork table it will not use.

Missing artwork answers with `None`, never with a placeholder. SAPRS 6.7 is explicit
that missing artwork must not affect playback, and a placeholder path would make an
absent image indistinguishable from a present one all the way to the template.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from encore.domain import Artwork, ArtworkId, Metadata, MusicFile, MusicFileId, SongId
from encore.repositories.library import queries
from encore.repositories.library.connection import LibraryConnection
from encore.repositories.library.mappers import to_artwork, to_metadata, to_music_file

__all__ = ["ArtworkRepository", "MusicFileRepository", "ProvenanceRepository"]


class ArtworkRepository:
    """Reads the `artwork` index, which points into the cache rather than into a BLOB."""

    def __init__(self, connection: LibraryConnection) -> None:
        self._connection = connection

    def by_id(self, artwork_id: ArtworkId) -> Artwork | None:
        return self._connection.one(queries.ARTWORK_BY_ID, to_artwork, (int(artwork_id),))

    def for_song(self, song_id: SongId) -> Artwork | None:
        """The song's own cover, falling back to its album's (SAPRS 6.7).

        The fallback lives here rather than in a template because it is a fact about
        the library — the Builder attaches artwork to whichever row the file named —
        and a template that grew that much logic would be AIG 22's "business logic in
        templates".
        """

        identifier = int(song_id)
        return self._connection.one(queries.ARTWORK_FOR_SONG, to_artwork, (identifier, identifier))


class MusicFileRepository:
    """Reads `music_files`: the file behind a song, as the Builder recorded it."""

    def __init__(self, connection: LibraryConnection) -> None:
        self._connection = connection

    def by_id(self, file_id: MusicFileId) -> MusicFile | None:
        return self._connection.one(queries.FILE_BY_ID, to_music_file, (int(file_id),))

    def metadata_for(self, song_id: SongId) -> Metadata | None:
        """Reassemble the tags behind a song, provenance included (SAPRS 4.1, 6.5).

        Admin-only (milestone 14). It costs two joins and a second query, which is
        why it is not part of the browse path and why nothing on a guest screen calls
        it.
        """

        row = self._connection.raw.execute(queries.SONG_METADATA, (int(song_id),)).fetchone()
        if row is None:
            return None
        provenance: list[sqlite3.Row] = self._connection.raw.execute(
            queries.PROVENANCE_BY_SONG, (int(song_id),)
        ).fetchall()
        return to_metadata(row, provenance)


class ProvenanceRepository:
    """Where each value came from — the audit trail ADR-010 makes mandatory."""

    def __init__(self, connection: LibraryConnection) -> None:
        self._connection = connection

    def for_song(self, song_id: SongId) -> list[dict[str, object]]:
        from encore.repositories.library.mappers import to_provenance  # noqa: PLC0415 - one caller

        return self._connection.rows(queries.PROVENANCE_BY_SONG, to_provenance, (int(song_id),))

    def sources(self) -> Sequence[str]:
        """Distinct origins present in this store, for the build summary."""

        return [
            str(row[0])
            for row in self._connection.raw.execute(
                "SELECT DISTINCT source FROM metadata ORDER BY source"
            )
        ]
