"""The Library Service: one façade over the immutable library (SAPRS 11.6, AIG 7).

AIG 7 has listed this service since the bootstrap phase, and issue #25's predecessor
explained why it was not written then: nothing needed a second name for a repository
that already returns domain objects. The web tier is the second reader — a browse
endpoint, a fragment, the queue and an artwork response all want the same rows — so
the condition that decision attached to writing it has arrived.

What it adds over `LibraryStore` is the three things a request needs and a repository
should not know about:

* **Page policy.** `artists(offset, limit)` clamps rather than accepting an unbounded
  `all()`: SAPRS 1.8's navigation budget is a per-request promise, and a 15,000-row
  artist list cannot meet it.
* **Artwork as a file, resolved safely.** The library stores a *reference* (SAPRS 5.6
  forbids embedding binaries in rows) and `paths.artwork_dir` says where the bytes
  live. A reference is data another application wrote (ADR-010), so it is resolved and
  then checked to be inside the cache before anything is opened.
* **One honest answer when the library is wrong.** `counts`, `song_count` and
  `search_available` are what an endpoint reports instead of a stack trace, and SAPRS
  11.2 makes "search cannot happen" a different sentence from "nothing matches".

The service never writes. `library.db` is opened read-only by the repository (ADR-006,
ADR-009), and there is deliberately no mutation method here for a server to find.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from encore.domain import (
    Album,
    AlbumId,
    Artist,
    ArtistId,
    Artwork,
    ArtworkId,
    Song,
    SongId,
)

__all__ = ["DEFAULT_PAGE_LIMIT", "MAX_PAGE_LIMIT", "ArtworkFile", "LibraryService", "Page"]

#: Rows a browse page returns when the caller did not ask: about one screen of touch
#: targets on a phone (SAPRS 9.2), which is a more useful number than a round one.
DEFAULT_PAGE_LIMIT = 60

#: The widest page Encore will serve. A caller asking for more gets this, because an
#: unbounded `?limit=` turns a browse screen into a memory test on a 4 GB board.
MAX_PAGE_LIMIT = 500


@dataclass(frozen=True, slots=True, kw_only=True)
class Page[T]:
    """One page of a collection, with enough arithmetic to say "and more".

    `total` is the whole collection, so a screen can read "312 artists" without a
    second request. `has_more` is derived rather than stored, because the two
    disagreeing is the bug this dataclass can still have.
    """

    items: Sequence[T]
    offset: int
    limit: int
    total: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total

    @property
    def next_offset(self) -> int:
        return self.offset + len(self.items)


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtworkFile:
    """A cached image, addressed by the file the Builder wrote (SAPRS 5.6, 6.7).

    `width` and `height` are 0 when the cache row carries none, which a template
    renders as no aspect-ratio hint rather than as a made-up one.
    """

    path: Path
    kind: str
    width: int = 0
    height: int = 0

    @property
    def etag(self) -> str:
        """A cache validator from the file's own size and mtime.

        An artwork cache is immutable until the next build (ADR-006), so a URL cannot
        change underneath a client that has it cached — that is what makes SAPRS
        10.10's "artwork is cacheable" true rather than merely intended. The `ETag` is
        how a reload proves it in a few bytes, and it is derived from the file rather
        than stored because a stored validator is one more thing that can be wrong.
        """

        stat = _stat_of(self.path)
        return f'"{self.path.name}-{stat[0]:x}-{stat[1]:x}"' if stat else f'"{self.path.name}"'

    @property
    def exists(self) -> bool:
        return self.path.is_file()


class SongRead(Protocol):
    """The song lookups `LibraryService` performs, named as `SongRepository` names them."""

    def by_id(self, song_id: SongId) -> Song | None: ...

    def by_ids(self, ids: Sequence[SongId]) -> list[Song]: ...

    def by_album(self, album_id: AlbumId) -> list[Song]: ...

    def by_artist(self, artist_id: ArtistId) -> list[Song]: ...


class ArtistRead(Protocol):
    def by_id(self, artist_id: ArtistId) -> Artist | None: ...

    def by_ids(self, ids: Sequence[ArtistId]) -> list[Artist]: ...

    def page(self, *, limit: int, offset: int = 0) -> list[Artist]: ...

    def count(self) -> int: ...


class AlbumRead(Protocol):
    def by_id(self, album_id: AlbumId) -> Album | None: ...

    def by_ids(self, ids: Sequence[AlbumId]) -> list[Album]: ...

    def by_artist(self, artist_id: ArtistId) -> list[Album]: ...


class ArtworkRead(Protocol):
    def by_id(self, artwork_id: ArtworkId) -> Artwork | None: ...

    def for_song(self, song_id: SongId) -> Artwork | None: ...


class LibraryRead(Protocol):
    """`LibraryStore`'s public surface, as capabilities rather than as a class.

    Declared structurally so a test of page clamping and artwork-path safety needs no
    SQLite file — the same reasoning that made `QueueService` name `SongLookup` rather
    than import the store. `LibraryStore` satisfies it; the assignment that proves it
    is annotated in `tests/integration/test_server_app.py` rather than asserted at
    runtime.
    """

    @property
    def songs(self) -> SongRead: ...

    @property
    def artists(self) -> ArtistRead: ...

    @property
    def albums(self) -> AlbumRead: ...

    @property
    def artwork(self) -> ArtworkRead: ...

    def counts(self) -> Mapping[str, int]: ...

    def fts_available(self) -> bool: ...


class LibraryService:
    """Reads the immutable library the way a request needs it read.

    Args:
        store: The read side of `library.db` (ADR-009). Never written to (AIG 4).
        artwork_dir: `paths.artwork_dir` — the cache the Builder wrote. Every path
            returned by an `artwork_for_*` method is checked against it.
        logger: For the one case worth reporting: a reference whose file is not
            where the library says it is.
    """

    def __init__(
        self,
        store: LibraryRead,
        *,
        artwork_dir: Path,
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._artwork_dir = Path(artwork_dir)
        self._logger = logger or logging.getLogger("encore.library")

    # -- songs (SAPRS 9.7) ------------------------------------------------

    def song(self, song_id: SongId) -> Song | None:
        return self._store.songs.by_id(song_id)

    def songs_by_ids(self, ids: Iterable[SongId]) -> list[Song]:
        """Batch read. This is also `queue_service.SongLookup`, which is how the
        queue and the HTTP layer came to agree about what a queued song is."""

        return self._store.songs.by_ids(tuple(ids))

    def songs_for_album(self, album_id: AlbumId) -> list[Song]:
        return self._store.songs.by_album(album_id)

    def songs_for_artist(self, artist_id: ArtistId) -> list[Song]:
        return self._store.songs.by_artist(artist_id)

    # -- artists and albums (SAPRS 9.4, 9.5) ------------------------------

    def artist(self, artist_id: ArtistId) -> Artist | None:
        return self._store.artists.by_id(artist_id)

    def album(self, album_id: AlbumId) -> Album | None:
        return self._store.albums.by_id(album_id)

    def artists(self, *, offset: int = 0, limit: int = DEFAULT_PAGE_LIMIT) -> Page[Artist]:
        """The artist list, in the library's own sort order (SAPRS 9.4)."""

        size = _clamp(limit)
        start = max(0, offset)
        return Page(
            items=self._store.artists.page(limit=size, offset=start),
            offset=start,
            limit=size,
            total=self._store.artists.count(),
        )

    def albums_for_artist(self, artist_id: ArtistId) -> list[Album]:
        return self._store.albums.by_artist(artist_id)

    def artists_by_ids(self, ids: Sequence[ArtistId]) -> list[Artist]:
        return self._store.artists.by_ids(ids)

    def albums_by_ids(self, ids: Sequence[AlbumId]) -> list[Album]:
        return self._store.albums.by_ids(ids)

    # -- artwork (SAPRS 5.6, 9.8, 10.10) ------------------------------------

    def artwork_for_song(self, song_id: SongId) -> ArtworkFile | None:
        return self._file(self._store.artwork.for_song(song_id))

    def artwork_for_album(self, album_id: AlbumId) -> ArtworkFile | None:
        album = self._store.albums.by_id(album_id)
        return None if album is None else self._artwork_of(album.artwork_id)

    def artwork_for_artist(self, artist_id: ArtistId) -> ArtworkFile | None:
        artist = self._store.artists.by_id(artist_id)
        return None if artist is None else self._artwork_of(artist.artwork_id)

    def _artwork_of(self, artwork_id: ArtworkId | None) -> ArtworkFile | None:
        return None if artwork_id is None else self._file(self._store.artwork.by_id(artwork_id))

    def _file(self, artwork: Artwork | None) -> ArtworkFile | None:
        """Resolve a reference to a file, or to nothing.

        A missing picture is not an error worth raising: SAPRS 6.7 is explicit that
        bad artwork is a missing picture and never a missing track, so the screen
        shows a placeholder rather than an exception page.
        """

        if artwork is None:
            return None
        candidate = (self._artwork_dir / artwork.relative_path).resolve()
        if not candidate.is_relative_to(self._artwork_dir.resolve()):
            # A reference that escapes the cache is a corrupt row, and the only honest
            # answer is to behave as though the picture did not exist. Checked after
            # `resolve` rather than by matching text, because `..` has more spellings
            # than a filter has branches.
            self._logger.warning(
                "artwork reference escaped the cache",
                extra={
                    "artwork_id": int(artwork.id),
                    "relative_path": str(artwork.relative_path),
                },
            )
            return None
        # A reference whose file is gone stays a reference. "Nothing is depicted" and "the
        # cache has lost what the library names" are different facts, and collapsing them here
        # would make the second invisible: an image directory deleted from under a database
        # would render placeholders everywhere and report nothing anywhere. `ArtworkFile.exists`
        # carries the distinction to the one caller that can act on it (SAPRS 6.7, 10.9).
        return ArtworkFile(
            path=candidate,
            kind=str(artwork.kind.value),
            width=artwork.width,
            height=artwork.height,
        )

    # -- facts about the library itself (SAPRS 10.3) -----------------------

    def counts(self) -> Mapping[str, int]:
        """Artists, albums, songs, files — as the store counted them, in four statements."""
        return self._store.counts()

    @property
    def song_count(self) -> int:
        return self.counts().get("songs", 0)

    @property
    def search_available(self) -> bool:
        """False means search cannot be asked, not that nothing matched (SAPRS 11.2)."""

        return self._store.fts_available()


def _clamp(limit: int) -> int:
    """The caller's page size, or the appliance's, whichever is smaller."""

    if limit < 1:
        raise ValueError(f"a page needs at least one row, got {limit}")
    return min(limit, MAX_PAGE_LIMIT)


def _stat_of(path: Path) -> tuple[int, int] | None:
    """(size, mtime) for a cache file, or None when it is not there.

    `ETag` needs both and a vanished cache entry needs neither to be an exception:
    the request that races a library reload is legitimate, and 304-versus-404 is the
    wrong place to discover it.
    """

    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_size, int(info.st_mtime)
