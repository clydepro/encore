"""`LibraryService`: paging, batching, and artwork that cannot leave the cache.

Three behaviours make this a service rather than a pass-through, and each is a rule the
HTTP layer would otherwise have to remember on every route:

* **Page sizes are clamped, not trusted.** SAPRS 10.9 asks for "reasonable protection
  against pathological queries", and `?limit=1000000` is the pathological query that a
  working search box hides. Clamping here means a route that forgets is still safe.
* **An artwork path is resolved and then checked against the cache directory.** The Builder
  writes the cache, the Server reads it, and `library.db` holds the filenames (ADR-006).
  "Written by our own Builder" is not a security property on a machine that will be handed
  other people's directories, so a reference that escapes is treated as a missing picture.
* **A page's labels cost two reads, not two hundred.** `encore/api/rows.py` resolves artists
  and albums in batches, which is the difference between SAPRS 1.8's 100 ms budget being
  about SQLite and being about a serialiser that reads in a loop.

The doubles are `LibraryRead`'s rather than the store's, for the reason the protocol is
declared structurally in the first place: the arithmetic of a page and the path of a cache
miss do not need a SQLite file. `tests/integration/test_server_app.py` opens the real ones.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

import pytest

from encore.domain import (
    Album,
    AlbumId,
    Artist,
    ArtistId,
    Artwork,
    ArtworkId,
    ArtworkKind,
    AudioFormat,
    Song,
    SongId,
)
from encore.services.library_service import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    ArtworkFile,
    LibraryService,
)

ARTISTS = [
    Artist(
        id=ArtistId(number),
        name=f"Artist {number}",
        sort_name=f"artist {number}",
        normalized_name=f"artist {number}",
    )
    for number in range(1, 13)
]
ALBUMS = [
    Album(
        id=AlbumId(number),
        artist_id=ArtistId(1 + number % 3),
        title=f"Album {number}",
        sort_title=f"album {number}",
        normalized_title=f"album {number}",
        artwork_id=ArtworkId(1),
    )
    for number in range(1, 8)
]
SONGS = [
    Song(
        id=SongId(number),
        title=f"Track {number}",
        artist_id=ALBUMS[number % len(ALBUMS)].artist_id,
        album_id=ALBUMS[number % len(ALBUMS)].id,
        file_path=Path("/music") / f"{number}.flac",
        file_format=AudioFormat.FLAC,
        duration=timedelta(seconds=200 + number),
        sort_title=f"track {number}",
    )
    for number in range(1, 21)
]


class _Songs:
    def __init__(self, store: Store) -> None:
        self._store = store

    def by_id(self, song_id: SongId) -> Song | None:
        self._store.reads.append(f"song:{int(song_id)}")
        return next((song for song in SONGS if int(song.id) == int(song_id)), None)

    def by_ids(self, ids: Sequence[SongId]) -> list[Song]:
        self._store.reads.append("songs_by_ids")
        wanted = {int(value) for value in ids}
        return [song for song in SONGS if int(song.id) in wanted]

    def by_album(self, album_id: AlbumId) -> list[Song]:
        self._store.reads.append(f"songs_for_album:{int(album_id)}")
        return [song for song in SONGS if int(song.album_id) == int(album_id)]

    def by_artist(self, artist_id: ArtistId) -> list[Song]:
        self._store.reads.append(f"songs_for_artist:{int(artist_id)}")
        return [song for song in SONGS if int(song.artist_id) == int(artist_id)]

    def count(self) -> int:
        return len(SONGS)


class _Artists:
    def __init__(self, store: Store) -> None:
        self._store = store

    def by_id(self, artist_id: ArtistId) -> Artist | None:
        self._store.reads.append(f"artist:{int(artist_id)}")
        return next((artist for artist in ARTISTS if int(artist.id) == int(artist_id)), None)

    def by_ids(self, ids: Sequence[ArtistId]) -> list[Artist]:
        self._store.reads.append("artists_by_ids")
        wanted = {int(value) for value in ids}
        return [artist for artist in ARTISTS if int(artist.id) in wanted]

    def page(self, *, limit: int, offset: int = 0) -> list[Artist]:
        self._store.reads.append(f"artists_page:{limit}:{offset}")
        return ARTISTS[offset : offset + limit]

    def count(self) -> int:
        return len(ARTISTS)


class _Albums:
    def __init__(self, store: Store) -> None:
        self._store = store

    def by_id(self, album_id: AlbumId) -> Album | None:
        self._store.reads.append(f"album:{int(album_id)}")
        return next((album for album in ALBUMS if int(album.id) == int(album_id)), None)

    def by_ids(self, ids: Sequence[AlbumId]) -> list[Album]:
        self._store.reads.append("albums_by_ids")
        wanted = {int(value) for value in ids}
        return [album for album in ALBUMS if int(album.id) in wanted]

    def by_artist(self, artist_id: ArtistId) -> list[Album]:
        self._store.reads.append(f"albums_for_artist:{int(artist_id)}")
        return [album for album in ALBUMS if int(album.artist_id) == int(artist_id)]

    def count(self) -> int:
        return len(ALBUMS)


class _Artwork:
    def __init__(self, store: Store) -> None:
        self._store = store

    def by_id(self, artwork_id: ArtworkId) -> Artwork | None:
        self._store.reads.append(f"artwork:{int(artwork_id)}")
        return self._store.artwork_rows.get(int(artwork_id))

    def for_song(self, song_id: SongId) -> Artwork | None:
        """The repository's own fallback: a song's picture, else its album's (SAPRS 6.7)."""

        self._store.reads.append(f"artwork_for_song:{int(song_id)}")
        return self._store.artwork_rows.get(int(song_id) % 5)


class Store:
    """A `LibraryRead` that counts what it was asked.

    The counting is the point: a row-builder that looped over a page calling `artist()` per
    item would return exactly the same entities and fail
    `test_a_pages_labels_cost_two_reads_whatever_the_page_size`.
    """

    def __init__(self, *, artwork: dict[int, Artwork], fts: bool) -> None:
        self.artwork_rows = artwork
        self._fts = fts
        self.reads: list[str] = []
        self.songs = _Songs(self)
        self.artists = _Artists(self)
        self.albums = _Albums(self)
        self.artwork = _Artwork(self)

    def counts(self) -> dict[str, int]:
        self.reads.append("counts")
        return {"songs": len(SONGS), "albums": len(ALBUMS), "artists": len(ARTISTS)}

    def fts_available(self) -> bool:
        return self._fts


def built(
    tmp_path: Path, *, references: dict[int, str] | None = None, fts: bool = True
) -> LibraryService:
    """A service over one store, with artwork references resolved to real files.

    `references` maps an artwork id to a path inside the cache; a value that cannot be
    written (an escape, a missing file) is left as a row, which is the situation each test
    is about.
    """

    rows: dict[int, Artwork] = {}
    for identifier, relative in (references or {}).items():
        path = tmp_path / "artwork" / relative
        if not path.is_symlink() and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\xff\xd8\xff\xd9")
        rows[identifier] = Artwork(
            id=ArtworkId(identifier),
            kind=ArtworkKind.ALBUM,
            relative_path=Path(relative),
            width=640,
            height=640,
        )
    store = Store(artwork=rows, fts=fts)
    return LibraryService(store, artwork_dir=tmp_path / "artwork")


def reads_of(service: LibraryService) -> list[str]:
    reads: list[str] = service._store.reads  # type: ignore[attr-defined]
    return reads


# -- paging (SAPRS 9.4, 10.9) ---------------------------------------------


def test_a_page_carries_the_total_so_a_screen_can_say_how_many(tmp_path: Path) -> None:
    """`total` is why the artists page can print "12 artists" from one request."""

    service = built(tmp_path)

    page = service.artists(offset=2, limit=5)

    assert [int(artist.id) for artist in page.items] == [3, 4, 5, 6, 7]
    assert (page.offset, page.limit, page.total) == (2, 5, 12)
    assert page.has_more
    assert page.next_offset == 7


def test_an_oversized_limit_is_clamped_rather_than_refused(tmp_path: Path) -> None:
    """SAPRS 10.9's "reasonable protection", in the one place a route cannot forget it.

    A 400 would be defensible and a worse appliance: the caller asked for more than the
    budget allows and got a page, so pasting a bigger `limit` into a URL teaches nobody
    anything.
    """

    service = built(tmp_path)

    assert service.artists(limit=MAX_PAGE_LIMIT * 40).limit == MAX_PAGE_LIMIT
    assert service.artists(limit=12).limit == 12


@pytest.mark.parametrize("limit", [1, 20, MAX_PAGE_LIMIT, MAX_PAGE_LIMIT * 2])
def test_the_clamp_holds_at_every_size_it_claims_to(tmp_path: Path, limit: int) -> None:
    service = built(tmp_path)

    assert service.artists(limit=limit).limit == min(limit, MAX_PAGE_LIMIT)


def test_the_default_page_is_the_one_the_developer_docs_quote(tmp_path: Path) -> None:
    assert built(tmp_path).artists().limit == DEFAULT_PAGE_LIMIT


def test_a_negative_offset_is_the_first_page(tmp_path: Path) -> None:
    """`?offset=-5` from a hand-edited URL is a browse, not a 500.

    SQLite answers `LIMIT 60 OFFSET -5` with every row, which is the opposite of what a
    pager means; clamping here is what keeps the SQL honest.
    """

    page = built(tmp_path).artists(offset=-5)

    assert page.offset == 0
    assert len(page.items) == len(ARTISTS)


def test_an_offset_past_the_end_is_an_empty_page_not_a_missing_page(tmp_path: Path) -> None:
    """A bookmarked `/artists?offset=400` after a rebuild (SAPRS 12.1's honesty).

    The alternative is a 404 on a route that exists. An empty list beside a true total says
    what is actually the case: nothing here, twelve somewhere.
    """

    page = built(tmp_path).artists(offset=400)

    assert page.items == []
    assert page.total == 12
    assert not page.has_more


# -- labels, in batches (SAPRS 1.8) ---------------------------------------


def test_a_pages_labels_cost_two_reads_whatever_the_page_size(tmp_path: Path) -> None:
    """The query-count assertion behind the 100 ms search budget.

    `rows_for_songs` could have been written as a comprehension calling `library.artist()`
    per row. On a 200-row album page that is 400 statements instead of two, over one
    connection that cannot parallelise them, and every result would have looked right.
    """

    from encore.api.rows import rows_for_songs

    service = built(tmp_path)
    rows = rows_for_songs(SONGS, service)

    assert len(rows) == len(SONGS)
    assert all(row.artist is not None and row.album is not None for row in rows)
    assert reads_of(service).count("artists_by_ids") == 1
    assert reads_of(service).count("albums_by_ids") == 1
    assert not [read for read in reads_of(service) if read.startswith(("artist:", "album:"))], (
        "a per-row label read: the N+1 this batch exists to prevent"
    )


def test_an_artist_screen_is_three_reads(tmp_path: Path) -> None:
    """SAPRS 9.5's "one interaction to reach a track", priced.

    Three statements for a whole artist — the artist, their albums, their tracks. A fourth
    per album would still be correct and would be the shape of a slow page.
    """

    from encore.api.views import artist_context

    service = built(tmp_path)
    context = artist_context(_JustLibrary(service), ArtistId(2))  # type: ignore[arg-type]

    assert context["artist"] is not None
    assert context["albums"]
    assert context["tracks"]
    # Three catalogue reads, and two more to label the tracks the third returned. A screen
    # that asked once per row would be `1 + 2 x tracks` statements, and every one of them
    # would be right — which is why the count is asserted rather than the content.
    assert reads_of(service) == [
        "artist:2",
        "albums_for_artist:2",
        "songs_for_artist:2",
        "artists_by_ids",
        "albums_by_ids",
    ]


class _JustLibrary:
    """Just enough of the `Jukebox` protocol for one context builder.

    `views` takes the protocol rather than the composition root so a builder can be read
    against one service; the full graph is what `tests/integration/test_server_app.py`
    boots, which is where the rest of the protocol's honesty is checked.
    """

    def __init__(self, library: LibraryService) -> None:
        self.library = library


def test_a_song_that_is_not_in_the_library_resolves_to_nothing(tmp_path: Path) -> None:
    """SAPRS 14.10 from the read side: `runtime.db` outlives `library.db` (ADR-009)."""

    assert built(tmp_path).song(SongId(90_000)) is None


# -- artwork (SAPRS 5.6, 6.7, 10.10) --------------------------------------


def test_artwork_in_the_cache_is_returned_with_a_strong_validator(tmp_path: Path) -> None:
    service = built(tmp_path, references={1: "ab/cd.jpg"})

    found = service.artwork_for_album(AlbumId(1))

    assert found is not None
    assert found.exists
    assert found.path == tmp_path / "artwork" / "ab" / "cd.jpg"
    assert found.etag.startswith('"')
    assert found.etag.endswith('"')


def test_a_reference_that_escapes_the_cache_is_refused(tmp_path: Path) -> None:
    """The file-read bug this service exists to prevent.

    Resolving first and comparing after is the difference between a check and a gesture:
    `..` has more spellings than a filter has branches, and this is a URL-reachable read.
    """

    (tmp_path / "id_ed25519").write_text("private key")
    service = built(tmp_path, references={})
    # Written by hand rather than through `built`, whose job is to create real files.
    service._store.artwork_rows[1] = Artwork(  # type: ignore[attr-defined]
        id=ArtworkId(1), kind=ArtworkKind.ALBUM, relative_path=Path("../id_ed25519")
    )

    assert service.artwork_for_album(AlbumId(1)) is None


def test_a_symlink_out_of_the_cache_is_refused(tmp_path: Path) -> None:
    """The same rule, one step later: the link is inside, its target is not."""

    cache = tmp_path / "artwork"
    cache.mkdir()
    secret = tmp_path / "id_ed25519"
    secret.write_text("private key")
    (cache / "cover.jpg").symlink_to(secret)
    service = built(tmp_path, references={})
    service._store.artwork_rows[1] = Artwork(  # type: ignore[attr-defined]
        id=ArtworkId(1), kind=ArtworkKind.ALBUM, relative_path=Path("cover.jpg")
    )

    assert service.artwork_for_album(AlbumId(1)) is None


def test_a_row_naming_a_file_that_has_vanished_keeps_naming_it(tmp_path: Path) -> None:
    """An artwork cache deleted from under the database, which is a real Tuesday.

    The service reports the reference and whether the file is behind it, rather than folding
    the two into `None`. That distinction is what lets `/artwork/...` answer the two cases
    differently: nothing depicted is a placeholder (SAPRS 6.7's rule, and a page of broken
    images otherwise), a picture named and missing is a refusal, because a cache that has gone
    is a fault and a fault that renders as a quiet grey square is a fault nobody finds until a
    rebuild.

    The pair this test belongs to is in `tests/regression/test_issue_25_http_runtime.py`, which
    checks the two HTTP answers these two states produce.
    """

    service = built(tmp_path, references={1: "ab/here.jpg"})
    (tmp_path / "artwork" / "ab" / "here.jpg").unlink()

    found = service.artwork_for_album(AlbumId(1))

    assert found is not None, "the library still names this picture; say so rather than not"
    assert not found.exists


def test_a_song_with_no_artwork_row_is_none_and_the_row_falls_back(tmp_path: Path) -> None:
    """SAPRS 6.7: a missing picture never makes a track unplayable."""

    assert built(tmp_path, references={}).artwork_for_song(SongId(90)) is None


def test_search_availability_is_the_stores_verdict(tmp_path: Path) -> None:
    """SAPRS 11.2's degraded-not-broken rule, surfaced for the banner.

    A library built without FTS5 browses fine. The difference is one boolean the shell turns
    into a sentence, and it has to come from the file rather than from a configuration key
    that can be wrong about what the Builder actually did.
    """

    assert built(tmp_path).search_available is True
    assert built(tmp_path, fts=False).search_available is False


def test_the_placeholder_path_is_a_static_asset_not_a_route() -> None:
    """One file, one URL, named in `rows.py` and in `html.py`'s filter (SAPRS 10.10).

    A route that generated a placeholder would make a cacheable URL sometimes uncacheable,
    and would answer every miss — including the one above, which should stay visible.
    """

    from encore.api.rows import SongRow

    assert SongRow(song=None).artwork_url == "/static/img/no-cover.svg"


def test_an_etag_changes_when_the_file_changes(tmp_path: Path) -> None:
    """`immutable` caching is only safe if a rebuild busts the validator (ADR-006).

    The Builder names cache files by content (SAPRS 5.6) so in practice the URL changes too;
    the `ETag` is the belt, and this is the test that says it is not decorative.
    """

    service = built(tmp_path, references={1: "ab/same.jpg"})
    before = service.artwork_for_album(AlbumId(1))
    assert before is not None
    # Read the validator now. `etag` is a property over `stat()`, so a reference held to the
    # file would be the same object's answer after the write and the comparison below would
    # prove nothing about either.
    before_tag = before.etag
    (tmp_path / "artwork" / "ab" / "same.jpg").write_bytes(b"\xff\xd8\xff\xd9" + b"0" * 40)
    after = service.artwork_for_album(AlbumId(1))

    assert after is not None
    assert after.etag != before_tag


def test_artwork_file_reports_the_dimensions_the_builder_wrote(tmp_path: Path) -> None:
    """SAPRS 9.2's layout stability: a sized image slot stops the panel jumping."""

    service = built(tmp_path, references={1: "ab/big.png"})

    found: ArtworkFile | None = service.artwork_for_album(AlbumId(1))

    assert found is not None
    assert (found.width, found.height) == (640, 640)


# -- facts about the library (SAPRS 10.3) ---------------------------------


def test_counts_are_the_number_the_pages_predict(tmp_path: Path) -> None:
    """The banner's "15,000 songs" and the browse page's total must be one number."""

    service = built(tmp_path)

    assert service.counts()["artists"] == service.artists().total
    assert service.song_count == len(SONGS)
