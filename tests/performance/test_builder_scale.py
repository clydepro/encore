"""Builder scale and library read latency (SAPRS 1.8, 6.13, ADR-010's measurements).

The one performance file that measures something rather than asserting a budget against
a stub, because milestone 5 has a real subsystem to measure: a built `library.db` and the
read path over it. The corpus is synthetic but structurally like the measured one — the
same tag density, the same tracks per album, the same cover-file ratio — so a regression
in a search query is a regression here.

Two numbers matter and they are different in kind:

* **Build throughput**, which SAPRS 6.13 bounds at "under a minute for 15,000 files" on
  reference hardware. The build is timed here and printed rather than asserted: a
  wall-clock line that fails on a busy shared runner teaches people to disable the suite,
  and the ratio test below is the assertion that actually catches a bad change.
* **Query latency**, which is the p95 target of 100 ms and can be asserted honestly at
  any size, because it questions SQLite's index depth and not the machine's speed.

Run with `--run-slow`.
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

import pytest

from apps.builder.pipeline import BuildOptions, run
from encore.repositories.library import open_library
from tests.support.media import AlbumSpec, cover_image, generate_albums

pytestmark = [pytest.mark.slow, pytest.mark.performance]

#: Albums x tracks. 1,500 files is a tenth of the budget: enough for the planner to have
#: to choose an index, small enough that generating them stays inside a minute.
ALBUMS = 150
TRACKS_PER_ALBUM = 10
SONGS = ALBUMS * TRACKS_PER_ALBUM

#: One artist every seven albums, so the artist list is small and the album list is not —
#: the ratio the measured corpus has, and the reason an artist page is a lookup and an
#: album page is a scan.
ARTISTS = len({f"Artist {album // 7:03d}" for album in range(ALBUMS)})


@pytest.fixture(scope="module")
def large_corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("scale-music")
    image = cover_image(size=96)
    specs = [
        AlbumSpec.with_tracks(
            f"Artist {album // 7:03d}",
            f"Album {album:04d}",
            count=TRACKS_PER_ALBUM,
            duration_seconds=2.0,
            artwork=image if album % 3 else None,
        )
        for album in range(ALBUMS)
    ]
    generate_albums(specs, root)
    return root


@pytest.fixture(scope="module")
def large_library(large_corpus: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One real build of the corpus, published, with its timing printed for the log."""

    home = tmp_path_factory.mktemp("scale-home")
    options = BuildOptions(
        music_dir=large_corpus,
        library_db=home / "library.db",
        artwork_dir=home / "artwork",
        temp_dir=home / "tmp",
        library_version="scale",
    )
    started = time.perf_counter()
    report = run(options)
    elapsed = time.perf_counter() - started
    assert report.songs == SONGS, "the fixture built what it says it built"
    print(
        f"\nbuild: {report.songs} songs in {elapsed:.2f}s ({report.songs / elapsed:,.0f} files/s)"
    )
    return options.library_db


@pytest.mark.parametrize("term", ["track", "album", "artist"])
def test_song_search_latency_under_load(large_library: Path, term: str) -> None:
    """p95 < 100 ms (SAPRS 1.8), over 40 queries against a 1,500-row index."""

    samples: list[float] = []
    with open_library(large_library) as store:
        for _ in range(40):
            started = time.perf_counter()
            store.search.songs(term, limit=50)
            samples.append((time.perf_counter() - started) * 1_000)
    p95 = statistics.quantiles(samples, n=20)[18]
    print(f"\nsong search '{term}': p50 {statistics.median(samples):.1f} ms, p95 {p95:.1f} ms")
    assert p95 < 100.0, f"p95 {p95:.1f} ms over 40 '{term}' queries"


def test_artist_and_album_search_are_not_faster_by_accident(large_library: Path) -> None:
    """The other two indexes, at the same budget.

    Asserted separately because a `song_search` that silently fell back to a scan of
    `song_details` would still pass the test above and still show every artist page.
    """

    with open_library(large_library) as store:
        songs = store.search.songs("album", limit=10)
        albums = store.search.albums("album", limit=10)
        artists = store.search.artists("artist", limit=10)
        assert songs
        assert albums
        assert artists
        started = time.perf_counter()
        store.search.albums("album", limit=50)
        store.search.artists("artist", limit=50)
        assert (time.perf_counter() - started) * 1_000 < 200.0


def test_navigation_reads_stay_under_the_budget(large_library: Path) -> None:
    """SAPRS 1.8's 200 ms navigation figure, at the layer a page actually pays for.

    Deep offsets are included on purpose: the All Songs page is a paged `ORDER BY
    sort_title, id` over every row, and an index that the planner cannot use shows up
    first at the end of the list.
    """

    samples: list[float] = []
    with open_library(large_library) as store:
        total = store.songs.count()
        assert total == SONGS
        for offset in range(0, min(total, 1_200), 60):
            started = time.perf_counter()
            page = store.songs.page(limit=50, offset=offset)
            samples.append((time.perf_counter() - started) * 1_000)
            assert len(page) == 50
        started = time.perf_counter()
        artists = store.artists.page(limit=50, offset=0)
        samples.append((time.perf_counter() - started) * 1_000)
        assert len(artists) == ARTISTS
    print(f"\nnavigation: worst page {max(samples):.1f} ms over {len(samples)} reads")
    assert max(samples) < 200.0, f"worst page read {max(samples):.1f} ms"


def test_an_incremental_rebuild_does_less_work_than_a_full_one(
    large_corpus: Path, tmp_path: Path
) -> None:
    """SAPRS 6.9's promise, stated as work avoided rather than as a wall clock.

    An incremental build that re-reads every file is not *wrong* — it produces the same
    library — it is a full build wearing a flag, and the only cheap way to notice is to
    count what it opened.
    """

    home = tmp_path / "encore"
    options = BuildOptions(
        music_dir=large_corpus,
        library_db=home / "library.db",
        artwork_dir=home / "artwork",
        temp_dir=home / "tmp",
        library_version="scale",
    )
    started = time.perf_counter()
    full = run(options)
    full_seconds = time.perf_counter() - started

    started = time.perf_counter()
    incremental = run(options)
    incremental_seconds = time.perf_counter() - started

    assert full.reused == 0
    assert incremental.reused == SONGS
    assert incremental.artwork == 0, "an unchanged build writes no images"
    assert incremental.songs == full.songs
    print(
        f"\nfull {full_seconds:.2f}s, incremental {incremental_seconds:.2f}s, {incremental.reused} files reused"
    )
    assert incremental_seconds < full_seconds, "the second build re-read the first one's files"
