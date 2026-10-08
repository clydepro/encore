"""Album (SAPRS 4.3).

One release, belonging to one artist. Like `Artist`, an album is a normalized
identity produced by the Builder, not a tag copy.
"""

from __future__ import annotations

from dataclasses import dataclass

from encore.domain.identifiers import AlbumId, ArtistId, ArtworkId

__all__ = ["Album"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Album:
    """A normalized release.

    Attributes:
        id: Stable identifier assigned by the Builder (SAPRS 4.3).
        artist_id: Owning artist. One album has one artist: various-artists
            releases are a distinct artist of their own, which is what keeps the
            "select an artist, list their albums" flow (SAPRS 9.5) a single
            lookup.
        title: Album title as the library should show it.
        sort_title: Folding used to order album lists; empty means "sort by
            `title`".
        normalized_title: Case- and whitespace-folded form used for comparison
            and search (SAPRS 6.5).
        musicbrainz_release_id: Optional identifier from enrichment
            (SAPRS 6.6).
        artwork_id: Reference into the artwork cache (SAPRS 5.6, 6.7).
    """

    id: AlbumId
    artist_id: ArtistId
    title: str
    sort_title: str = ""
    normalized_title: str = ""
    musicbrainz_release_id: str | None = None
    artwork_id: ArtworkId | None = None

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError(f"album {self.id} needs a display title")

    @property
    def sort_key(self) -> str:
        """The value album browsing orders on."""

        return self.sort_title or self.title
