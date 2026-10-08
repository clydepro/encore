"""Stable identifiers (SAPRS 4.2-4.5).

Every core entity carries a "stable identifier", and the normalized tables of
SAPRS 5.3 make those identifiers the integers the Library Builder assigns.

A bare `int` is legal Python and useless in a signature: `Song(artist_id=7,
album_id=7, ...)` type-checks and is wrong, and the failure surfaces as the
wrong artist's album on a guest's phone. `NewType` costs nothing at runtime and
turns that into a type error.

Repositories (milestone 5) wrap row values in these types on the way out of
SQLite; nothing else needs to know they are distinct.
"""

from __future__ import annotations

from typing import NewType

__all__ = [
    "AlbumId",
    "ArtistId",
    "ArtworkId",
    "MusicFileId",
    "QueueItemId",
    "SongId",
]

ArtistId = NewType("ArtistId", int)
AlbumId = NewType("AlbumId", int)
SongId = NewType("SongId", int)
ArtworkId = NewType("ArtworkId", int)
MusicFileId = NewType("MusicFileId", int)
QueueItemId = NewType("QueueItemId", int)
