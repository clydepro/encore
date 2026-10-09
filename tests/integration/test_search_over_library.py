"""Search as a running feature: the service over a library the Builder produced.

`tests/unit/test_search_service.py` proves the decisions; this proves the seam. The
expression `encore.search` compiles is bound into the SQL of
`encore/repositories/library/queries.py` against the `song_search` table that
`apps/builder/schema.py` actually creates — the only arrangement in which a mistake in
either half is visible: a contentless index whose columns cannot be selected, a `MATCH`
whose left side must name the table rather than an alias, a `rowid` that has to be
resolved through the view.

The corpus is `tests/conftest.py`'s `music_tree`: two artists, three albums, seven
tracks named "<album> Track <n>", so a query has to cross an artist, an album and a
title boundary to answer anything.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from encore.domain import Album, Artist, Song
from encore.repositories.library import LibraryStore, open_library
from encore.search import CatalogueRead, SearchResults, SearchService, SearchUnavailableError
from encore.search.query import parse


@pytest.fixture
def service(library_store: LibraryStore) -> SearchService:
    """The real index and the real catalogue, wired the way `apps/server` will be."""

    return SearchService(index=library_store.search, catalogue=library_store)


# -- the seam itself ------------------------------------------------------


def test_the_store_satisfies_the_interfaces_search_declares(
    library_store: LibraryStore,
) -> None:
    """Structural typing is only honest if it is checked against the real object.

    A fake that matches `CatalogueRead` proves the protocol is self-consistent. This
    proves it *describes the store*, which is the half a rename breaks — and the
    annotation is the test, because mypy reads it on every commit.
    """

    catalogue: CatalogueRead = library_store

    assert catalogue.artists_by_ids([]) == []
    assert isinstance(library_store.search.songs('"track"*', limit=1), list)


# -- the three kinds (SAPRS 5.5) ------------------------------------------


def test_song_search_finds_tracks_by_a_word_in_the_title(service: SearchService) -> None:
    hits = service.songs("first")

    assert {hit.song.title for hit in hits} == {f"First Album Track {n}" for n in (1, 2, 3)}
    assert all(isinstance(hit.song, Song) for hit in hits)


def test_every_song_hit_says_who_it_is_by(service: SearchService) -> None:
    """SAPRS 9.6 against a built library rather than a fixture: the names resolve.

    `line` is what a guest reads. A batch read that asked for the wrong ids would show
    a title with nothing under it, and every unit test here would still pass.
    """

    hits = service.songs("third")
    assert len(hits) == 2, "both tracks of the two-track album"

    hit = hits[0]
    assert hit.artist is not None
    assert hit.artist.name == "Someone Else"
    assert isinstance(hit.artist, Artist)
    assert hit.album is not None
    assert hit.album.title == "Third Album"
    assert isinstance(hit.album, Album)
    assert hit.line == "Someone Else — Third Album Track 1"


def test_album_search_finds_a_release_and_names_its_artist(service: SearchService) -> None:
    hits = service.albums("second")

    assert [hit.album.title for hit in hits] == ["Second Album"]
    assert hits[0].artist is not None
    assert hits[0].artist.name == "The Test Artists"


def test_artist_search_finds_an_artist_by_name(service: SearchService) -> None:
    hits = service.artists("test")

    assert [hit.artist.name for hit in hits] == ["The Test Artists"]


def test_one_search_answers_all_three_kinds(service: SearchService) -> None:
    """`song_search` indexes artist and album text alongside the title (SAPRS 5.4).

    "test" is therefore one word that reaches all three indexes at once, which is what
    makes this a test of the aggregate rather than of three separate fixtures.
    """

    results = service.search("test")

    assert results.songs
    assert results.albums
    assert results.artists
    assert results.total == len(results.songs) + len(results.albums) + len(results.artists)
    assert isinstance(results.query.match, str)


def test_prefix_matching_reaches_a_word_by_its_start(service: SearchService) -> None:
    """`prefix='2 3'` in the schema is what makes a partial word answer at all.

    Asserted against the built index rather than a fake: if the Builder stopped
    declaring prefixes, this is where it surfaces. A misspelling is still nothing —
    prefix matching is not spelling correction, and pretending otherwise would make
    "firsr" answer with the wrong songs.
    """

    assert service.songs("firsr") == []
    assert service.songs("fir")


def test_matched_fields_name_what_the_index_actually_hit(service: SearchService) -> None:
    """'second' is in the album title, and therefore in every track's album column."""

    (album,) = service.albums("second")
    (song, *_rest) = service.songs("second")

    assert "album" in {field.value for field in album.matched_on}
    assert "album" in {field.value for field in song.matched_on}


# -- what the service must not do -----------------------------------------


def test_the_service_does_not_reorder_what_bm25_ordered(service: SearchService) -> None:
    """The weights live in `queries.py` (title 10, album 1, artist 1) and stop there.

    The claim is negative and specific: the service's order *is* the index's order. A
    Python sort by score would agree on this corpus and disagree on a real one, which is
    the kind of bug that only appears at 15,000 songs.
    """

    direct = service.search("first")
    separate = service.songs("first", limit=50)

    assert [hit.song.id for hit in separate] == [hit.song.id for hit in direct.songs]
    assert parse("first").match == '"first"*'


def test_a_blank_query_touches_no_sql(service: SearchService) -> None:
    assert service.search("   ").empty
    assert service.songs("") == []
    assert service.artists("  ") == []


@pytest.mark.parametrize(
    "raw",
    [
        'AC/DC: "Highway" OR',
        "collector's items -NOT (x)^2",
        '"unbalanced',
        "*",
        "NEAR/A",
        "\u2016",
        "track track track",
    ],
)
def test_nothing_a_guest_can_type_breaks_a_real_query(service: SearchService, raw: str) -> None:
    """The repository takes an expression, so this is where a rejected one lands.

    "no such column" from an aliased FTS table and a syntax error from a bare operator
    are both failures on a guest's phone; the parser's whole job is that neither can
    reach SQLite.
    """

    assert isinstance(service.search(raw), SearchResults)


# -- a degraded library (SAPRS 11.2) --------------------------------------


def test_a_library_that_lost_its_index_says_so_instead_of_returning_nothing(
    library_store: LibraryStore, tmp_path: Path
) -> None:
    """The FTS tables are derived data (SAPRS 5.4), so losing them is survivable.

    The appliance must still play everything it knows, and must not report "no results"
    for a query it could not run. That distinction is the failure SAPRS 11.2 names, and
    an empty result list would pass every other test in this file.
    """

    stranded = tmp_path / "library.db"
    stranded.write_bytes(library_store.path.read_bytes())
    library_store.close()
    connection = sqlite3.connect(stranded)
    try:
        for table in ("song_search", "album_search", "artist_search"):
            connection.execute(f"DROP TABLE {table}")
        connection.commit()
    finally:
        connection.close()

    store = open_library(stranded)
    try:
        assert not store.fts_available(), "the store notices at open, not at search"
        assert store.songs.count() > 0, "and the music is still there"
        with pytest.raises(SearchUnavailableError, match="no search index"):
            SearchService(index=store.search, catalogue=store).songs("first")
    finally:
        store.close()
