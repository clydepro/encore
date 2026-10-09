"""Pure domain model: entities, value objects and rules (SAPRS Chapter 4).

This package is the innermost layer of the architecture (SAPRS 15.2). It must
not import FastAPI, Jinja, SQLite bindings, mpv bindings or any web framework.
It uses standard-library frozen dataclasses and `enum` only; Pydantic belongs to
the configuration boundary (`encore/config/`), where external input arrives.

SAPRS Chapter 4 defines the model; this package states it once, so the
repositories, the services and the Library Builder all use the same words. Every
type is immutable: a library row that changed while the appliance was playing
from it would be a bug nobody could reproduce, and ADR-006 makes the same point
about `library.db`.

Two boundaries worth knowing before adding a type here:

* **Normalization rules are not in this package.** SAPRS 6.5 gives them to the
  Library Builder (milestone 6). Entities carry the *results* - `sort_name`,
  `normalized_name` - because Chapter 4 lists them as properties.
* **Nothing here touches a file, a socket or a process.** Playback state and
  progress are values; the state machine that produces them lives in
  `encore/playback/` (milestone 8).

Import from `encore.domain`, not from a module inside it, so the package stays
free to reorganize.
"""

from __future__ import annotations

from encore.domain.album import Album
from encore.domain.artist import Artist
from encore.domain.health import HEALTHY, ComponentHealth, HealthStatus
from encore.domain.identifiers import (
    AlbumId,
    ArtistId,
    ArtworkId,
    MusicFileId,
    QueueItemId,
    SongId,
)
from encore.domain.media import Artwork, ArtworkKind, AudioFormat, MusicFile
from encore.domain.metadata import CANONICAL_TAGS, Metadata
from encore.domain.playback import (
    ALLOWED_TRANSITIONS,
    PlaybackOutcome,
    PlaybackProgress,
    PlaybackState,
    can_transition,
)
from encore.domain.queue import ACTIVE_STATUSES, QueueItem, QueueItemStatus
from encore.domain.song import Song

__all__ = [
    "ACTIVE_STATUSES",
    "ALLOWED_TRANSITIONS",
    "CANONICAL_TAGS",
    "HEALTHY",
    "Album",
    "AlbumId",
    "Artist",
    "ArtistId",
    "Artwork",
    "ArtworkId",
    "ArtworkKind",
    "AudioFormat",
    "ComponentHealth",
    "HealthStatus",
    "Metadata",
    "MusicFile",
    "MusicFileId",
    "PlaybackOutcome",
    "PlaybackProgress",
    "PlaybackState",
    "QueueItem",
    "QueueItemId",
    "QueueItemStatus",
    "Song",
    "SongId",
    "can_transition",
]
