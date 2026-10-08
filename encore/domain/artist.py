"""Artist (SAPRS 4.2).

An artist is a normalized identity the library files tracks under, not a copy of
a tag: the Builder decides which spellings collapse into one artist
(SAPRS 6.5). At runtime artists are read-only data.
"""

from __future__ import annotations

from dataclasses import dataclass

from encore.domain.identifiers import ArtistId, ArtworkId

__all__ = ["Artist"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Artist:
    """A normalized music artist.

    Attributes:
        id: Stable identifier assigned by the Builder (SAPRS 4.2).
        name: Display name, as the library should show it.
        sort_name: Folding used to order artist lists. Empty means "sort by
            `name`"; `sort_key` resolves that, so no caller has to remember the
            difference and browsing (milestone 7) orders on `sort_key` alone.
        normalized_name: Case- and whitespace-folded form used for comparison
            and search (SAPRS 6.5).
        musicbrainz_id: Optional identifier from enrichment (SAPRS 6.6).
        artwork_id: Reference into the artwork cache (SAPRS 5.6). Optional
            because missing artwork must never affect playback (SAPRS 6.7).
    """

    id: ArtistId
    name: str
    sort_name: str = ""
    normalized_name: str = ""
    musicbrainz_id: str | None = None
    artwork_id: ArtworkId | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError(f"artist {self.id} needs a display name")

    @property
    def sort_key(self) -> str:
        """The value artist browsing orders on."""

        return self.sort_name or self.name
