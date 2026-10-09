"""The Search Service: search behaviour, and nothing else (SAPRS 11.6, 9.6, AIG 11).

Three things belong to this module, and none of them is SQL:

1. **Compiling the guest's words into something the index can be asked** —
   `encore.search.query`, which is why no caller ever hands raw text to a repository.
2. **Deciding what a result looks like** — the entity, plus the names that identify it,
   plus the fields that matched (SAPRS 9.6), assembled from batch catalogue reads.
3. **Saying so when search cannot happen** — `SearchUnavailableError` rather than an
   empty list (SAPRS 11.2).

Everything below that — tokenization, bm25 weights, the FTS tables — is already decided
in `encore/repositories/library/`, and ADR-009's contract test is what keeps this module
honest about it. A second query layer here would be exactly the drift that test exists
to catch, which is why the only SQL in this package is none.

Two absences, both deliberate:

* **No counting.** `StatisticKey.SEARCHES` exists and nothing increments it. That is
  `StatisticsService`'s job (AIG 7), and a service that writes someone else's counter
  is how two numbers for one action appear.
* **No paging past the cap.** `search.page_size` limits each list. An offset needs a
  stable order across queries, bm25 does not promise one, and "load more" is a
  milestone 12 decision with a screen attached to it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import Protocol, runtime_checkable

from encore.domain import Album, AlbumId, Artist, ArtistId
from encore.repositories.errors import SearchUnavailableError as NoIndexError
from encore.repositories.library import AlbumHit, ArtistHit, SearchRead, SongHit
from encore.search.errors import SearchUnavailableError
from encore.search.query import SearchQuery, parse
from encore.search.results import (
    AlbumResult,
    ArtistResult,
    SearchResults,
    SongResult,
    matched_fields,
)

__all__ = ["DEFAULT_PAGE_SIZE", "CatalogueRead", "SearchService"]

#: Results per kind when configuration has nothing to say — the value
#: `examples/config.yaml` prints, repeated here as the safe default.
DEFAULT_PAGE_SIZE = 50


@runtime_checkable
class CatalogueRead(Protocol):
    """The name-resolution half of the library, as search needs it.

    Declared by the consumer rather than the provider (SAPRS 11.5), so `encore.search`
    depends on a capability and not on `LibraryStore` — which satisfies it structurally,
    without importing this module and therefore without a cycle.
    """

    def artists_by_ids(self, ids: Sequence[ArtistId]) -> list[Artist]: ...

    def albums_by_ids(self, ids: Sequence[AlbumId]) -> list[Album]: ...


class SearchService:
    """Searches artists, albums and songs (SAPRS 5.5, 9.6).

    Args:
        index: The FTS5 read side — `LibraryStore.search` in production, a fake in
            tests. Deliberately the narrow `SearchRead` rather than the store, so a
            test of ranking and labelling policy needs no database.
        catalogue: Batch artist and album reads, for labelling results.
        page_size: Cap per result list, from `search.page_size`.
    """

    def __init__(
        self,
        *,
        index: SearchRead,
        catalogue: CatalogueRead,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        if page_size < 1:
            raise ValueError(f"page_size must be at least 1, got {page_size}")
        self._index = index
        self._catalogue = catalogue
        self._page_size = page_size

    @property
    def page_size(self) -> int:
        return self._page_size

    # -- the feature ------------------------------------------------------

    def search(self, text: str, *, limit: int | None = None) -> SearchResults:
        """One search: three lists, bm25 order within each.

        Never raises on input. A query that parses to nothing returns nothing, and a
        library it cannot be asked about raises `SearchUnavailableError`.
        """

        cap = self._cap(limit)
        query = parse(text)
        if query.is_empty:
            return SearchResults.nothing(text, limit=cap)
        return SearchResults(
            query=query,
            songs=tuple(self._songs(query, cap)),
            albums=tuple(self._albums(query, cap)),
            artists=tuple(self._artists(query, cap)),
            limit=cap,
        )

    def songs(self, text: str, *, limit: int | None = None) -> list[SongResult]:
        """Songs whose title, album or artist matches `text` (SAPRS 5.5)."""

        query = parse(text)
        return [] if query.is_empty else self._songs(query, self._cap(limit))

    def albums(self, text: str, *, limit: int | None = None) -> list[AlbumResult]:
        query = parse(text)
        return [] if query.is_empty else self._albums(query, self._cap(limit))

    def artists(self, text: str, *, limit: int | None = None) -> list[ArtistResult]:
        query = parse(text)
        return [] if query.is_empty else self._artists(query, self._cap(limit))

    # -- one list each ----------------------------------------------------

    def _songs(self, query: SearchQuery, limit: int) -> list[SongResult]:
        with _indexed():
            hits: Sequence[SongHit] = self._index.songs(query.match, limit=limit)
        artists = self._artist_map(hit.song.artist_id for hit in hits)
        albums = self._album_map(hit.song.album_id for hit in hits)
        results: list[SongResult] = []
        for hit in hits:
            artist = artists.get(hit.song.artist_id)
            album = albums.get(hit.song.album_id)
            results.append(
                SongResult(
                    song=hit.song,
                    artist=artist,
                    album=album,
                    score=hit.score,
                    matched_on=matched_fields(
                        query.tokens,
                        title=hit.song.title,
                        album="" if album is None else album.title,
                        artist="" if artist is None else artist.name,
                    ),
                )
            )
        return results

    def _albums(self, query: SearchQuery, limit: int) -> list[AlbumResult]:
        with _indexed():
            hits: Sequence[AlbumHit] = self._index.albums(query.match, limit=limit)
        artists = self._artist_map(hit.album.artist_id for hit in hits)
        results: list[AlbumResult] = []
        for hit in hits:
            artist = artists.get(hit.album.artist_id)
            results.append(
                AlbumResult(
                    album=hit.album,
                    artist=artist,
                    score=hit.score,
                    matched_on=matched_fields(
                        query.tokens,
                        album=hit.album.title,
                        artist="" if artist is None else artist.name,
                    ),
                )
            )
        return results

    def _artists(self, query: SearchQuery, limit: int) -> list[ArtistResult]:
        with _indexed():
            hits: Sequence[ArtistHit] = self._index.artists(query.match, limit=limit)
        return [
            ArtistResult(
                artist=hit.artist,
                score=hit.score,
                # `sort_name` is indexed too, but it is the same name filed
                # differently; a guest needs one answer, not two labels.
                matched_on=matched_fields(query.tokens, artist=hit.artist.name),
            )
            for hit in hits
        ]

    # -- internals --------------------------------------------------------

    def _cap(self, limit: int | None) -> int:
        """The smaller of the caller's ask and the configured page, never unlimited.

        A caller asking for more than `search.page_size` gets the configuration's
        answer: the budget is per request, and one screen of five thousand rows is how
        a 100 ms feature becomes a second.
        """

        if limit is None:
            return self._page_size
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        return min(limit, self._page_size)

    def _artist_map(self, ids: Iterable[ArtistId]) -> dict[ArtistId, Artist]:
        return {artist.id: artist for artist in self._catalogue.artists_by_ids(_unique(ids))}

    def _album_map(self, ids: Iterable[AlbumId]) -> dict[AlbumId, Album]:
        return {album.id: album for album in self._catalogue.albums_by_ids(_unique(ids))}


@contextmanager
def _indexed() -> Iterator[None]:
    """Translate "this library cannot search" into the feature's own words."""

    try:
        yield
    except NoIndexError as error:
        raise SearchUnavailableError(
            "this library has no search index; rebuild it with encore-builder (SAPRS 5.4)"
        ) from error


def _unique[T: int](ids: Iterable[T]) -> list[T]:
    """De-duplicate ids, ordered by value.

    `IN ()` is a syntax error and a repeated id is wasted work, so a search that
    returns forty songs from three artists must ask for three rows. Sorted, because an
    orderless batch read makes the answer depend on which order the hits arrived in.
    """

    seen: dict[int, T] = {}
    for identifier in ids:
        seen.setdefault(int(identifier), identifier)
    return [seen[key] for key in sorted(seen)]
