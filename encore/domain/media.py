"""Physical media and artwork (SAPRS 4.1, 4.4, 5.3, 5.6).

`MusicFile` is what exists on disk; `Artwork` is what exists in the artwork
cache. Both are referenced by `encore.domain.song.Song` rather than embedded in
it, because SAPRS 5.6 requires artwork to be addressed by a stable reference and
SAPRS 6.8 needs the file record on its own for duplicate analysis.

The Builder (milestone 6) discovers these; nothing here opens a file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final

from encore.domain.identifiers import ArtworkId, MusicFileId

__all__ = ["Artwork", "ArtworkKind", "AudioFormat", "MusicFile"]


class AudioFormat(StrEnum):
    """The containers Encore promises to play (SAPRS 1.2, 6.3).

    Anything else on disk is ignored or reported by the Builder; it never
    reaches this type.
    """

    MP3 = "mp3"
    FLAC = "flac"
    AAC = "aac"
    M4A = "m4a"

    @property
    def suffixes(self) -> tuple[str, ...]:
        """Filename suffixes that identify this format, lowercase and dotted."""

        return _SUFFIXES[self]

    @classmethod
    def for_path(cls, path: Path) -> AudioFormat | None:
        """Return the format of `path`, or None when Encore cannot play it.

        A None result is not an error: SAPRS 6.3 says unsupported files are
        ignored or reported, and choosing between those is the Builder's call.
        """

        suffix = path.suffix.lower()
        for candidate in cls:
            if suffix in candidate.suffixes:
                return candidate
        return None


_SUFFIXES: Final[dict[AudioFormat, tuple[str, ...]]] = {
    AudioFormat.MP3: (".mp3",),
    AudioFormat.FLAC: (".flac",),
    AudioFormat.AAC: (".aac",),
    # `.mp4` is a legal AAC container. SAPRS 1.2 names both "AAC" and "M4A"
    # without distinguishing them, so the Builder decides which label a given
    # file earns; both are discoverable here.
    AudioFormat.M4A: (".m4a", ".mp4"),
}


class ArtworkKind(StrEnum):
    """What an artwork file depicts (SAPRS 6.7).

    The Builder associates artwork with artists, albums and songs "as
    appropriate"; the kind records which of those associations a file serves.
    """

    ARTIST = "artist"
    ALBUM = "album"
    SONG = "song"


@dataclass(frozen=True, slots=True, kw_only=True)
class MusicFile:
    """One playable file in the library (SAPRS 4.1, 5.3 `music_files`).

    `path` is recorded exactly as it will exist on the appliance. The Builder
    rewrites source paths into the layout it was given (SAPRS 13.3); Playback
    then consumes the value verbatim, so no layer resolves paths against
    configuration at runtime (SAPRS 7.7).
    """

    id: MusicFileId
    path: Path
    format: AudioFormat
    duration: timedelta
    size_bytes: int = 0

    def __post_init__(self) -> None:
        if self.duration < timedelta(0):
            raise ValueError(f"duration must not be negative, got {self.duration}")
        if self.size_bytes < 0:
            raise ValueError(f"size_bytes must not be negative, got {self.size_bytes}")
        if not self.path.is_absolute():
            raise ValueError(f"path must be absolute on the appliance, got {self.path}")

    @property
    def duration_seconds(self) -> float:
        """Duration in seconds — the unit mpv and `runtime.db` both use."""

        return self.duration.total_seconds()


@dataclass(frozen=True, slots=True, kw_only=True)
class Artwork:
    """A cached image addressed by a stable reference (SAPRS 4.1, 5.6, 6.7).

    SAPRS 5.6 rules out embedding binaries in rows, so this is a pointer plus
    the dimensions the Builder generated. Missing artwork must never make a
    track unplayable (SAPRS 6.7), which is why every association into this
    entity is optional.
    """

    id: ArtworkId
    kind: ArtworkKind
    relative_path: Path
    width: int = 0
    height: int = 0

    def __post_init__(self) -> None:
        if self.width < 0 or self.height < 0:
            raise ValueError(f"dimensions must not be negative, got {self.width}x{self.height}")
        if self.relative_path.is_absolute():
            raise ValueError(
                "artwork is referenced relative to the configured artwork directory "
                f"(SAPRS 5.6), got absolute {self.relative_path}"
            )
