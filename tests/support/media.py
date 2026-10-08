"""Synthetic media generator hooks (PBK 16, SAPRS 14.9).

Playback, builder, performance and Party Simulation tests all need real audio
files on disk: a two-second silent MP3 behaves like the library Encore will
index, without shipping 15,000 tracks in Git.

Generating them requires an encoder (`ffmpeg`/`lame`), which the bootstrap phase
does not assume is present. These functions are deliberate placeholders: they
fail loudly so that no test silently passes on an empty fixture directory.

Owners:

- milestone 6 (Library Builder) — file creation and metadata tagging.
- milestone 16 (Party Simulation) — bulk generation against load profiles.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

#: Formats promised by SAPRS 1.2.
SUPPORTED_FORMATS: Final[tuple[str, ...]] = ("mp3", "flac", "aac", "m4a")


class SyntheticMediaUnavailableError(RuntimeError):
    """Raised when a test asks for audio files that cannot yet be generated."""


@dataclass(frozen=True)
class MediaSpec:
    """One synthetic track.

    Attributes:
        title: Track title written into metadata.
        artist: Artist name.
        album: Album name.
        track_number: Position on the album.
        duration_seconds: Target length of the silent audio.
        fmt: Container/codec, one of `SUPPORTED_FORMATS`.
        silence: Whether to emit silence rather than a tone.
    """

    title: str
    artist: str
    album: str
    track_number: int = 1
    duration_seconds: float = 1.0
    fmt: str = "mp3"
    silence: bool = True

    @property
    def filename(self) -> str:
        safe = _slug(self.title)
        return f"{self.track_number:02d}-{safe}.{self.fmt}"


@dataclass(frozen=True)
class AlbumSpec:
    """A group of tracks sharing artist/album metadata."""

    artist: str
    album: str
    tracks: Sequence[MediaSpec] = field(default_factory=tuple)


def generate_track(spec: MediaSpec, destination: Path) -> Path:
    """Create one synthetic track and return its path.

    Placeholder for milestone 6. Implementations must not depend on network
    access and must leave the file readable by mpv on the target platform.
    """

    raise SyntheticMediaUnavailableError(
        "Synthetic media generation is not implemented yet (PBK 16 placeholder). "
        f"Would have written {destination / spec.filename!r}."
    )


def generate_albums(specs: Iterable[AlbumSpec], destination: Path) -> list[Path]:
    """Create a directory tree of `artist/album/track` files.

    Placeholder for milestone 6; used by builder and performance suites.
    """

    raise SyntheticMediaUnavailableError(
        f"Synthetic library generation is not implemented yet (PBK 16 placeholder); "
        f"target tree would be {destination!r}."
    )


def _slug(value: str) -> str:
    slug = "".join(character if character.isalnum() else "-" for character in value)
    return slug.lower().strip("-")
