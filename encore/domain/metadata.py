"""Tag values read from a music file (SAPRS 4.1, 6.4, 6.5).

`Metadata` is the evidence; `Artist`, `Album` and `Song` are the conclusions.
Keeping them apart matters because SAPRS 6.5 requires the original values to
remain available after normalization, and SAPRS 6.6 lets the Builder repair them
from MusicBrainz - neither of which can be audited afterwards if only the final
answer is stored.

The *rules* for normalizing and repairing are milestone 6 work. This module
carries values only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from types import MappingProxyType
from typing import Final

__all__ = ["CANONICAL_TAGS", "Metadata"]

#: Tag names Encore reads into named fields. Anything else a file carries lands
#: in `Metadata.extra` and survives into the library untouched (SAPRS 6.5).
CANONICAL_TAGS: Final[frozenset[str]] = frozenset(
    {
        "artist",
        "album",
        "album_artist",
        "title",
        "track",
        "disc",
        "genre",
        "date",
        "musicbrainz_artist_id",
        "musicbrainz_release_id",
        "musicbrainz_recording_id",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class Metadata:
    """The tags one file reported, exactly as it reported them (SAPRS 6.4).

    Attributes:
        artist: Primary tag artist.
        title: Track title.
        album: Album name, or None when the file said nothing. Absence is
            preserved rather than substituted: "no album tag" and
            ``[Unknown Album]`` are different facts, and choosing what to file
            a track under is the Builder's decision, not the model's.
        album_artist: Album artist where the file distinguishes one - the usual
            signal for a various-artists release (SAPRS 6.4).
        track_number / disc_number: 1-based positions, or None when absent.
        genre: Free-text genre tag.
        date: Release date as tagged. Deliberately text: tag dates are routinely
            partial ("1979", "1979-05"), and parsing them invents precision the
            source does not have.
        duration: Decoded audio length, from the container rather than a tag.
        musicbrainz_artist_id / musicbrainz_release_id / musicbrainz_recording_id:
            Identifiers already in the file, or supplied by enrichment
            (SAPRS 6.6).
        original: Every tag as the file carried it, keyed by tag name
            (SAPRS 6.5). Read-only.
        extra: Tags outside `CANONICAL_TAGS`, preserved untouched. Read-only.
    """

    artist: str
    title: str
    album: str | None = None
    album_artist: str | None = None
    track_number: int | None = None
    disc_number: int | None = None
    genre: str | None = None
    date: str | None = None
    duration: timedelta = timedelta(0)
    musicbrainz_artist_id: str | None = None
    musicbrainz_release_id: str | None = None
    musicbrainz_recording_id: str | None = None
    original: Mapping[str, str] = field(default_factory=dict)
    extra: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.artist.strip():
            raise ValueError(
                "artist is required: a file with no artist cannot be catalogued (SAPRS 6.5)"
            )
        if not self.title.strip():
            raise ValueError(
                "title is required: a file with no title cannot be catalogued (SAPRS 6.5)"
            )
        if self.duration < timedelta(0):
            raise ValueError(f"duration must not be negative, got {self.duration}")
        _require_position(self.track_number, "track_number")
        _require_position(self.disc_number, "disc_number")
        # A frozen dataclass can still hand out a mutable mapping, so the two tag
        # views are replaced with genuinely read-only ones rather than documented
        # as read-only.
        object.__setattr__(self, "original", MappingProxyType(dict(self.original)))
        object.__setattr__(self, "extra", MappingProxyType(dict(self.extra)))

    @property
    def has_identifiers(self) -> bool:
        """True when a MusicBrainz identifier is already known (SAPRS 6.6).

        Enrichment is rate-limited, so the Builder uses this to decide which
        files are worth a network round trip.
        """

        return bool(
            self.musicbrainz_artist_id
            or self.musicbrainz_release_id
            or self.musicbrainz_recording_id
        )

    def tag(self, name: str) -> str | None:
        """Return an original tag value, falling back to `extra`."""

        value = self.original.get(name)
        return value if value is not None else self.extra.get(name)


def _require_position(value: int | None, name: str) -> None:
    if value is not None and value < 1:
        raise ValueError(f"{name} is 1-based or absent, got {value}")
