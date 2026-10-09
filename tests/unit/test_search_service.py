"""`SearchService`: the behaviour SAPRS 9.6 and 11.2 ask of search (AIG 21 step 7).

The index and the catalogue are fakes here, which is the point: ranking, labelling and
the shape of the answer are this service's own decisions, and they should be testable
without a Builder run. `tests/integration/test_search_over_library.py` then runs the
same service against a real FTS5 library, so the seam is exercised from both ends.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

import pytest

from encore.domain import Album, AlbumId, Artist, ArtistId, AudioFormat, Song, SongId
from encore.repositories.errors import SearchUnavailableError as NoIndexError
from encore.repositories.library import AlbumHit, ArtistHit, SongHit
from encore.search import SearchResults, SearchService, SearchUnavailableError, parse
from encore.search.results import MatchedField

ARTISTS = {
    ArtistId(1): Artist(id=ArtistId(1), name="Cream"),
    ArtistId(2): Artist(id=ArtistId(2), name="AC/DC"),
}
ALBUMS = {
    AlbumId(10): Album(id=AlbumId(10), artist_id=ArtistId(1), title="Disraeli Gears"),
    AlbumId(20): Album(id=AlbumId(20), artist_id=ArtistId(2), title="Highway to Hell"),
}


def _song(id_: int, title: str, *, artist: int = 1, album: int = 10) -> Song:
    return Song(
        id=SongId(id_),
        title=title,
        artist_id=ArtistId(artist),
        album_id=AlbumId(album),
        file_path=Path(f"/music/{id_}.flac"),
        file_format=AudioFormat.FLAC,
        duration=timedelta(seconds=200),
    )


SONGS = (
    _song(100, "Strange Brew", artist=1, album=10),
    _song(101, "Highway to Hell", artist=2, album=20),
    _song(102, "Hell Bells", artist=2, album=20),
)


class FakeIndex:
    """Substring matching standing in for bm25, with the calls recorded.

    What matters to these tests is *which expression* the index was handed and how
    many times, so the fake keeps both and leaves the ranking to SQLite.
    """

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.calls: list[tuple[str, str, int]] = []
        self.song_rows = list(SONGS)

    def songs(self, match: str, *, limit: int) -> Sequence[SongHit]:
        self.calls.append(("song", match, limit))
        self._require()
        needle = match.replace('"', "").replace("*", "").split()
        return [
            SongHit(song=song, score=-1.0)
            for song in self.song_rows
            if any(word in song.title.casefold() for word in needle)
        ][:limit]

    def albums(self, match: str, *, limit: int) -> Sequence[AlbumHit]:
        self.calls.append(("album", match, limit))
        self._require()
        return [AlbumHit(album=album, score=-2.0) for album in ALBUMS.values()][:limit]

    def artists(self, match: str, *, limit: int) -> Sequence[ArtistHit]:
        self.calls.append(("artist", match, limit))
        self._require()
        return [ArtistHit(artist=artist, score=-3.0) for artist in ARTISTS.values()][:limit]

    def _require(self) -> None:
        if not self.available:
            raise NoIndexError


class FakeCatalogue:
    def __init__(self) -> None:
        self.artist_requests: list[Sequence[ArtistId]] = []
        self.album_requests: list[Sequence[AlbumId]] = []

    def artists_by_ids(self, ids: Sequence[ArtistId]) -> list[Artist]:
        self.artist_requests.append(ids)
        return [ARTISTS[id_] for id_ in ids if id_ in ARTISTS]

    def albums_by_ids(self, ids: Sequence[AlbumId]) -> list[Album]:
        self.album_requests.append(ids)
        return [ALBUMS[id_] for id_ in ids if id_ in ALBUMS]


@pytest.fixture
def index() -> FakeIndex:
    return FakeIndex()


@pytest.fixture
def catalogue() -> FakeCatalogue:
    return FakeCatalogue()


@pytest.fixture
def service(index: FakeIndex, catalogue: FakeCatalogue) -> SearchService:
    return SearchService(index=index, catalogue=catalogue, page_size=50)


# -- the answer's shape ---------------------------------------------------


def test_one_search_asks_each_index_once(service: SearchService, index: FakeIndex) -> None:
    results = service.search("hell")

    assert [call[0] for call in index.calls] == ["song", "album", "artist"]
    assert len({call[1] for call in index.calls}) == 1, "one parse, three reads"
    assert results.total > 0


def test_a_song_hit_carries_the_names_that_identify_it(
    service: SearchService, catalogue: FakeCatalogue
) -> None:
    """SAPRS 9.6: results must say what matched, not merely what scored.

    A title alone is unusable on a 15,000-track library, so the artist and album are
    resolved at the same time as the hits — and the catalogue proves it was one batch,
    not one call per row.
    """

    hits = service.songs("highway")

    assert hits, "the fake matches on title words"
    assert hits[0].song.title == "Highway to Hell"
    assert hits[0].artist is not None
    assert hits[0].artist.name == "AC/DC"
    assert hits[0].album is not None
    assert hits[0].album.title == "Highway to Hell"
    assert len(catalogue.artist_requests) == 1
    assert len(catalogue.album_requests) == 1


def test_names_are_requested_once_for_the_whole_list(
    service: SearchService, catalogue: FakeCatalogue
) -> None:
    """Two songs, one artist: the batch read must ask for one row.

    A per-hit lookup here is fifty artist queries inside a 100 ms budget, and it looks
    exactly like correct code until the library is big enough to notice.
    """

    service.songs("hell")

    assert list(catalogue.artist_requests[0]) == [ArtistId(2)]
    assert list(catalogue.album_requests[0]) == [AlbumId(20)]


def test_matched_fields_name_the_columns_the_tokens_reached(service: SearchService) -> None:
    (hit,) = service.songs("strange")

    assert hit.matched_on == {MatchedField.TITLE}, "the fake only matches titles"


def test_results_keep_the_query_that_produced_them(service: SearchService) -> None:
    results = service.search("  Strange  ")

    assert results.query == parse("Strange")
    assert results.query.text == "Strange"


# -- limits and empties ---------------------------------------------------


def test_blank_input_never_reaches_the_index(index: FakeIndex) -> None:
    service = SearchService(index=index, catalogue=FakeCatalogue())

    assert service.search("   ").empty
    assert index.calls == []
    assert service.songs("") == []
    assert service.albums('""') == []


def test_repeated_words_are_one_read(index: FakeIndex) -> None:
    service = SearchService(index=index, catalogue=FakeCatalogue())

    service.search("the the the")

    assert index.calls[0][1] == '"the"*'


def test_each_list_is_capped_at_the_page_size(index: FakeIndex) -> None:
    index.song_rows = [_song(200 + i, f"Track {i}") for i in range(200)]
    service = SearchService(index=index, catalogue=FakeCatalogue(), page_size=20)

    results = service.search("track")

    assert len(results.songs) == 20
    assert results.truncated


def test_a_smaller_ask_loses_to_the_configuration(service: SearchService, index: FakeIndex) -> None:
    """`search.page_size` is the ceiling; a caller cannot raise it.

    The budget is per request (SAPRS 1.8), and one screen of five thousand rows is how
    a 100 ms feature becomes a second.
    """

    service.search("hell", limit=10_000)

    assert all(call[2] == 50 for call in index.calls)


def test_an_explicit_smaller_limit_is_honoured(service: SearchService, index: FakeIndex) -> None:
    service.songs("hell", limit=3)

    assert index.calls[0][2] == 3


@pytest.mark.parametrize("limit", [0, -1])
def test_a_limit_below_one_is_a_programming_error(service: SearchService, limit: int) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        service.songs("hell", limit=limit)


def test_a_page_size_below_one_is_refused_at_construction(index: FakeIndex) -> None:
    with pytest.raises(ValueError, match="page_size"):
        SearchService(index=index, catalogue=FakeCatalogue(), page_size=0)


# -- failure --------------------------------------------------------------


def test_an_index_that_is_not_there_is_said_aloud(catalogue: FakeCatalogue) -> None:
    """SAPRS 11.2 forbids the silent empty result.

    A library without FTS5 and a library with no matches must not answer the same way,
    and the difference has to survive to the caller as a type rather than as prose.
    """

    service = SearchService(index=FakeIndex(available=False), catalogue=catalogue)

    with pytest.raises(SearchUnavailableError, match="no search index"):
        service.search("hell")


def test_results_default_to_three_empty_lists() -> None:
    results = SearchResults.nothing("anything", limit=5)

    assert results.empty
    assert not results.truncated
    assert results.songs == ()
    assert results.albums == ()
    assert results.artists == ()
