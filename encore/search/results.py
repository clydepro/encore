"""What a search returns, and why it matched (SAPRS 9.6, 5.5).

The repository answers with `SongHit`/`AlbumHit`/`ArtistHit`: one entity and a bm25
number. That is right for storage and wrong for a screen. SAPRS 9.6 asks that "results
should clearly identify what matched", and a song title on its own identifies nothing —
on a 15,000-track library *Cut* exists eleven times, and the guest is looking at a list
of homonyms with no way to tell them apart.

So the result types here carry the entity, the names that place it, and the set of
fields that the query's tokens actually hit. All three come from reads the repository
already supports; nothing here runs SQL, which is the point of the split ADR-009 draws.

`score` is passed through unchanged and deliberately unused for ordering: the index
ordered the hits, in SQL, with the bm25 weights in `queries.py`. A second ordering in
Python would silently overrule that policy, and the weights are the documented
behaviour (title above album above artist).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Self

from encore.domain import Album, Artist, Song
from encore.search.query import SearchQuery, parse

__all__ = [
    "AlbumResult",
    "ArtistResult",
    "MatchedField",
    "SearchResults",
    "SongResult",
]


class MatchedField(StrEnum):
    """Which indexed text a token hit (SAPRS 5.4's columns, named for display)."""

    TITLE = "title"
    ALBUM = "album"
    ARTIST = "artist"


def _starts_with_any(token: str, text: str) -> bool:
    """Whether `token` begins any word of `text`, case- and accent-insensitively enough.

    Mirrors the prefix semantics of the index rather than re-running the tokenizer:
    the words are split on the same non-alphanumerics `unicode61` splits on, and a
    token matches a word when it is a prefix of it. A substring test would claim a
    match the search could not have produced, which is the one way this function could
    be wrong and still look right.
    """

    return any(word.startswith(token) for word in text.casefold().split())


def matched_fields(
    tokens: tuple[str, ...],
    *,
    title: str = "",
    album: str = "",
    artist: str = "",
) -> frozenset[MatchedField]:
    """The fields of one hit that the query's tokens reached."""

    candidates = (
        (MatchedField.TITLE, title),
        (MatchedField.ALBUM, album),
        (MatchedField.ARTIST, artist),
    )
    return frozenset(
        name
        for name, text in candidates
        if text and any(_starts_with_any(token, text) for token in tokens)
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class SongResult:
    """One song, with the names that tell it apart from its eleven namesakes."""

    song: Song
    artist: Artist | None = None
    album: Album | None = None
    score: float = 0.0
    matched_on: frozenset[MatchedField] = field(default_factory=frozenset)

    @property
    def line(self) -> str:
        """The two-line label a guest reads: artist, then title."""

        return f"{self.artist.name} — {self.song.title}" if self.artist else self.song.title


@dataclass(frozen=True, slots=True, kw_only=True)
class AlbumResult:
    album: Album
    artist: Artist | None = None
    score: float = 0.0
    matched_on: frozenset[MatchedField] = field(default_factory=frozenset)

    @property
    def line(self) -> str:
        return self.album.title


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtistResult:
    artist: Artist
    score: float = 0.0
    matched_on: frozenset[MatchedField] = field(default_factory=frozenset)

    @property
    def line(self) -> str:
        return self.artist.name


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchResults:
    """A whole search: the parse it came from and the three lists it produced.

    Attributes:
        query: Kept so a fragment can say "3 results for *queen*" without the caller
            re-parsing the input and risking a different answer.
        songs / albums / artists: bm25 order within each list, never merged. The three
            kinds are different things to queue, and one flat list would force the UI
            to sort them back apart (SAPRS 9.6).
        limit: Per-list cap that was applied, so `truncated` can be stated honestly
            rather than guessed from `len(...) == limit`.
    """

    query: SearchQuery
    songs: tuple[SongResult, ...] = ()
    albums: tuple[AlbumResult, ...] = ()
    artists: tuple[ArtistResult, ...] = ()
    limit: int = 0

    @classmethod
    def nothing(cls, text: str, *, limit: int = 0) -> Self:
        """The answer to an empty search: no query, no results, no error.

        A blank search box is a guest who has not typed yet, not a bad request, and
        SQLite is never asked.
        """

        return cls(query=parse(text), limit=limit)

    @property
    def total(self) -> int:
        return len(self.songs) + len(self.albums) + len(self.artists)

    @property
    def empty(self) -> bool:
        return self.total == 0

    @property
    def truncated(self) -> bool:
        """Whether any list hit the cap, i.e. whether "show more" belongs on screen."""

        return any(
            self.limit and len(items) >= self.limit
            for items in (self.songs, self.albums, self.artists)
        )
