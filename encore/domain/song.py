"""Song (SAPRS 4.4).

A `Song` is a playable track: the normalized conclusion the Builder drew from one
or more files. It is the object every other subsystem passes around - search
returns it, the queue references it, playback loads it - which is why the file
facts live on it directly rather than behind a lookup: a queue that had to join
to `MusicFile` to find a path would be doing infrastructure work in the domain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from encore.domain.identifiers import AlbumId, ArtistId, ArtworkId, MusicFileId, SongId
from encore.domain.media import AudioFormat

__all__ = ["Song"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Song:
    """A playable track.

    Attributes:
        id: Stable identifier assigned by the Builder (SAPRS 4.4).
        title: Display title.
        artist_id: Performing artist. Stored alongside `album_id` rather than
            reached through it, because the artist on a track and the artist on
            the release legitimately disagree on compilations, and playback must
            not have to guess which one the library meant.
        album_id: Release this track sits on.
        sort_title: Folding used to order track lists; empty means "sort by
            `title`".
        track_number / disc_number: Position within the release, 1-based or
            None (SAPRS 6.4).
        duration: Decoded length. Playback start (SAPRS 1.8) and the progress
            bar both depend on it, so a song without a duration is a build
            defect, not a display nicety.
        file_path: Location on the appliance. Absolute, exactly as recorded by
            the Builder; nothing resolves it against configuration at runtime
            (SAPRS 7.7).
        file_format: Container, one of the formats SAPRS 1.2 promises.
        music_file_id: The `MusicFile` this track was derived from. Present so
            duplicate analysis (SAPRS 6.8) can be traced back from a song, and
            optional because a song may legitimately be filed from more than one
            identical file.
        artwork_id: Reference into the artwork cache (SAPRS 5.6). Optional:
            missing artwork must never make a track unplayable (SAPRS 6.7).
    """

    id: SongId
    title: str
    artist_id: ArtistId
    album_id: AlbumId
    file_path: Path
    file_format: AudioFormat
    duration: timedelta
    sort_title: str = ""
    track_number: int | None = None
    disc_number: int | None = None
    music_file_id: MusicFileId | None = None
    artwork_id: ArtworkId | None = None

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError(f"song {self.id} needs a display title")
        if not self.file_path.is_absolute():
            raise ValueError(
                f"file_path must be absolute on the appliance (SAPRS 13.3), got {self.file_path}"
            )
        if self.duration <= timedelta(0):
            raise ValueError(f"song {self.id} needs a positive decoded duration")
        _require_position(self.track_number, "track_number")
        _require_position(self.disc_number, "disc_number")

    @property
    def sort_key(self) -> str:
        """The value track browsing orders on."""

        return self.sort_title or self.title

    @property
    def duration_seconds(self) -> float:
        """Duration in seconds — the unit mpv and the progress bar use."""

        return self.duration.total_seconds()


def _require_position(value: int | None, name: str) -> None:
    if value is not None and value < 1:
        raise ValueError(f"{name} is 1-based or absent, got {value}")
