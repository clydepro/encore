"""Reopening a library that changed underneath a running appliance (ADR-006, SAPRS 6.11).

The rule is that the runtime never writes `library.db`; the consequence is that it cannot see a
rebuild by waiting. The Builder publishes by renaming a completed file into place (ADR-010) and
a live SQLite handle keeps the inode it opened, so "notice the new library" is exactly one
reopen. This file is the evidence for what that reopen promises: that it is not a write, that it
is not a restart, and that when it fails the appliance keeps playing the library it had.

Everything runs against two real Builder passes over two real corpora, for the reason
`test_library_read.py` gives: the agreement between the two applications is the thing under test,
and a hand-made second database would let both halves be wrong in the same direction.

Three claims worth naming before the tests, because each is a decision that a later edit could
silently undo:

* **Object identity survives.** The repositories, the search index and the services all hold a
  reference to one `LibraryStore`. A reload that built a *new* store would leave the running
  appliance reading the old file through nine still-pointing references — a bug whose symptom is
  a song count that never changes on an otherwise healthy box.
* **A failed reopen changes nothing.** Verification runs on the new handle before it is swapped,
  so a build interrupted mid-write leaves the current library serving rather than a half-file
  becoming the appliance's idea of music (SAPRS 11.9: maintenance must not be able to stop a
  party).
* **Search's availability belongs to the file, not the process.** The FTS verdict is recomputed
  on the same `LibrarySearch` object `SearchService` already holds, which is what makes the flip
  observable through a service that was never rebuilt.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.builder.pipeline import BuildOptions, run
from encore.domain import SongId
from encore.events import LibraryReloaded
from encore.events.bus import EventBus
from encore.repositories.contract import LIBRARY_SCHEMA_VERSION
from encore.repositories.errors import LibraryContractError, StoreNotFoundError
from encore.repositories.library import LibraryStore, open_library
from encore.search import SearchService
from encore.search.errors import SearchUnavailableError
from encore.services.library_service import LibraryService
from tests.conftest import BuiltLibrary
from tests.support.media import AlbumSpec, generate_albums

pytestmark = pytest.mark.unit

#: The fixture corpus: 3 + 2 + 2 tracks. Stated as a number because half of this file is
#: about a count that has to be *different* afterwards.
FIRST_SONGS = 7


class Watched:
    """A store, the service that reads it, and the facts it told.

    The trio is one object because every test here needs all three: the store to reopen, the
    service to reopen through, and the bus to check that the reopening was announced exactly
    once. A test that reconstructed two of them would be testing a wiring nobody has.
    """

    def __init__(self, store: LibraryStore, service: LibraryService, bus: EventBus) -> None:
        self.store = store
        self.service = service
        self.reloads: list[LibraryReloaded] = []
        bus.subscribe(LibraryReloaded, self.reloads.append, name="reload-watcher")


@pytest.fixture
def watched(built_library: BuiltLibrary, encore_home: Path) -> Iterator[Watched]:
    """The fixture library, open, through the objects the Server uses.

    `open_library` rather than the `library_store` fixture because one test needs to know the
    path and the rest need an object whose identity they can compare afterwards; both come from
    the same call, and the service is built the way `apps/server/appliance.py` builds it.
    """

    bus = EventBus()
    store = open_library(built_library.options.library_db)
    service = LibraryService(store, artwork_dir=built_library.options.artwork_dir, events=bus)
    try:
        yield Watched(store, service, bus)
    finally:
        store.close()


def build_another(tmp_path: Path, *, albums: list[AlbumSpec], version: str) -> Path:
    """A second published library at its own path, from its own corpus.

    Args:
        tmp_path: The test's directory, which is where a second build belongs.
        albums: What to write and scan. Smaller than the fixture corpus on purpose.
        version: The `library_version` stamp, which is how a test tells the two files apart
            after one has replaced the other.

    Returns:
        The published `library.db`. Its artwork directory sits beside it and is nobody's
        concern here: this file is about the database handle, not the images.
    """

    database = tmp_path / f"library-{version}.db"
    generate_albums(albums, tmp_path / f"music-{version}")
    run(
        BuildOptions(
            music_dir=tmp_path / f"music-{version}",
            library_db=database,
            artwork_dir=tmp_path / f"artwork-{version}",
            temp_dir=tmp_path / f"builder-{version}",
            library_version=version,
        )
    )
    return database


def publish(watched: Watched, database: Path) -> None:
    """Put a built file where the appliance's path says the library is, the way the Builder does.

    The rename matters as much as the file: the Builder publishes atomically (SAPRS 5.8,
    ADR-010), and a live handle keeps the inode it opened. A `copyfile` onto the destination
    would *not* be that — it truncates the open file and the running store would watch the
    library change underneath it, which is the opposite of what this file is here to establish.
    (That is not a hypothetical: it is how this helper was first written, and the first test in
    the file failed for it.)
    """

    incoming = watched.store.path.with_name("library.db.incoming")
    shutil.copyfile(database, incoming)
    incoming.replace(watched.store.path)


# -- what a reload is -------------------------------------------------------


def test_a_rebuilt_library_is_invisible_until_something_asks(
    watched: Watched, tmp_path: Path
) -> None:
    """ADR-006's premise, asserted rather than assumed.

    If a live handle *did* notice the new file, `reload()` would be a no-op and the library
    page's button would be theatre. The `immutable=1` read is why it does not — and the same
    property is what makes the Builder's rename safe to do while the Server is mid-query, so the
    test that looks like a criticism of the design is really its defence.
    """

    publish(
        watched,
        build_another(
            tmp_path,
            albums=[AlbumSpec.with_tracks("One Artist", "One Album", count=2)],
            version="b",
        ),
    )

    assert watched.store.counts()["songs"] == FIRST_SONGS, "nothing has changed by waiting"
    assert watched.service.summary().library_version == "test"


def test_a_reopen_reads_the_new_file_through_the_same_object(
    watched: Watched, tmp_path: Path
) -> None:
    """The identity claim: one store, one set of repositories, one library that changed.

    The second build is *smaller* and its ids are re-used, which is what makes the last two
    assertions mean something: a reload that re-read the same file would fail the count, and a
    reload that swapped in a new store object would fail the song lookups while still printing
    the right total.
    """

    publish(
        watched,
        build_another(
            tmp_path,
            albums=[AlbumSpec.with_tracks("One Artist", "One Album", count=2)],
            version="b",
        ),
    )

    info = watched.store.reload()

    assert info.library_version == "b"
    assert info.song_count == 2
    assert watched.store.counts()["songs"] == 2, "the same object is reading a different library"
    assert watched.store.songs.by_id(SongId(1)) is not None
    assert watched.store.songs.by_id(SongId(FIRST_SONGS)) is None


def test_a_reopen_leaves_the_store_usable_and_the_check_intact(
    watched: Watched, tmp_path: Path
) -> None:
    """A reload is not a restart, so everything already opened has to still work.

    `LIBRARY_SCHEMA_VERSION` is in the assertion because that check is what decides whether a
    file may be used at all: a library built for another revision is refused rather than
    migrated, and the appliance keeps the one it had (ADR-006).
    """

    publish(
        watched,
        build_another(
            tmp_path, albums=[AlbumSpec.with_tracks("Another", "Another", count=3)], version="c"
        ),
    )
    watched.store.reload()

    assert watched.store.info.schema_version == LIBRARY_SCHEMA_VERSION
    assert watched.store.counts()["albums"] == 1
    assert watched.store.fts_available() is True
    assert watched.store.albums_by_ids([]) == [], "the repository still answers, with nothing"


# -- the announcement -------------------------------------------------------


def test_the_service_publishes_the_change_once_per_reload(watched: Watched, tmp_path: Path) -> None:
    """`LibraryReloaded` has one publisher, and it is `LibraryService.reload` (ADR-004).

    The count is the Builder's own stamp rather than a re-derived one, because the number a
    guest screen is told has to be the number a build reported. The library page prints both
    side by side precisely so a disagreement stays visible.
    """

    publish(
        watched,
        build_another(
            tmp_path, albums=[AlbumSpec.with_tracks("Later", "Later", count=1)], version="d"
        ),
    )

    fact = watched.service.reload()

    assert isinstance(fact, LibraryReloaded)
    assert (fact.song_count, fact.version) == (1, "d")
    assert len(watched.reloads) == 1, "the fact was told once, by the service that saw it"


def test_reopening_from_the_store_does_not_announce_itself(
    watched: Watched, tmp_path: Path
) -> None:
    """The store is a mechanism; the service is the one who tells the room.

    Asserted in the negative because a health probe that decided to `reload()` on a tick would
    publish a `LibraryReloaded` per probe and every screen would repaint forever. The split is
    ADR-004's "one publisher per fact" applied to a method two callers can reach.
    """

    publish(
        watched,
        build_another(
            tmp_path, albums=[AlbumSpec.with_tracks("Quiet", "Quiet", count=2)], version="e"
        ),
    )

    watched.store.reload()

    assert watched.reloads == [], "the store reopened and said nothing"


# -- when it goes wrong -----------------------------------------------------


def test_a_half_written_library_leaves_the_old_one_serving(
    watched: Watched, tmp_path: Path
) -> None:
    """The failure this design is actually about.

    A build interrupted mid-write leaves a file that is not a library. Verification happens on
    the new handle *before* the swap, so the appliance an operator is standing next to keeps
    playing the catalogue it had instead of becoming a box with no music and an interface that
    cannot name a song.
    """

    broken = tmp_path / "half.db"
    broken.write_bytes(b"SQLite format 3\x00" + b"\x00" * 8192)
    publish(watched, broken)

    with pytest.raises(LibraryContractError):
        watched.service.reload()

    assert watched.store.counts()["songs"] == FIRST_SONGS
    assert watched.service.summary().library_version == "test"
    assert watched.store.songs.by_ids([SongId(1)]), "reads still work off the old handle"
    assert watched.reloads == [], "a reload that failed is not a fact about the library"


def test_a_library_that_vanished_leaves_the_old_one_serving(
    watched: Watched, tmp_path: Path
) -> None:
    """`sqlite3.connect()` would happily open an absent file, which is ADR-009's bug by name.

    The distinction the page needs: a missing file and a corrupt file are different sentences to
    an operator ("the SD card moved" versus "the build was interrupted"), and both must arrive
    as refusals rather than as a zero-song library.
    """

    with pytest.raises(StoreNotFoundError):
        open_library(tmp_path / "never-built.db")

    assert watched.store.counts()["songs"] == FIRST_SONGS


def test_search_follows_the_file_rather_than_the_process(watched: Watched, tmp_path: Path) -> None:
    """A rebuild that lost the FTS index is noticed by the service that was already built.

    This is SAPRS 11.2's degraded-search case arriving from the runtime side: the verdict lives
    on the `LibrarySearch` object `SearchService` holds, and `reload()` updates it *in place*
    rather than building a new index object. A replacement would leave search answering from the
    decision a library that is no longer current made — and a rebuild that *added* FTS would look
    like a broken index forever.
    """

    without_index = tmp_path / "no-fts.db"
    shutil.copyfile(watched.store.path, without_index)
    with sqlite3.connect(without_index) as conn:
        conn.execute("DROP TABLE IF EXISTS song_search")
    publish(watched, without_index)

    search = SearchService(index=watched.store.search, catalogue=watched.store)
    watched.service.reload()

    assert watched.store.fts_available() is False
    with pytest.raises(SearchUnavailableError):
        search.search("album")

    publish(
        watched,
        build_another(
            tmp_path, albums=[AlbumSpec.with_tracks("Indexed", "Indexed", count=2)], version="f"
        ),
    )
    watched.service.reload()

    assert watched.store.fts_available() is True
    assert search.search("indexed").albums, "the object the service holds is the one that changed"


# -- what the page prints ---------------------------------------------------


def test_a_reopen_refreshes_both_tallies_the_library_page_shows(
    watched: Watched, tmp_path: Path
) -> None:
    """`meta_count` is what the Builder stamped; `songs` is what a count says now.

    Both are carried because the *disagreement* is the diagnosis SAPRS 6.10 wants visible, and a
    reload that updated one and not the other would raise a false alarm on a healthy box — the
    kind an operator cannot dismiss without a shell.
    """

    before = watched.service.summary()
    assert before.tally_matches
    assert before.songs == FIRST_SONGS

    publish(
        watched,
        build_another(
            tmp_path, albums=[AlbumSpec.with_tracks("Summary", "Summary", count=4)], version="g"
        ),
    )
    watched.service.reload()

    after = watched.service.summary()
    assert (after.meta_count, after.songs) == (4, 4)
    assert after.tally_matches
    assert after.library_version == "g"
    assert after.path == watched.store.path
    assert after.built_at == watched.store.info.built_at


def test_the_reopened_library_still_finds_a_song_on_disk(watched: Watched, tmp_path: Path) -> None:
    """Artwork and file paths come from the new file, not from a cached one.

    The queue is the reason this matters at runtime: a reload that left `Song.path` pointing at
    the previous corpus would have mpv play a file that is no longer there, which is a
    `PlaybackFailed` per song and a silent room. The assertion is loose about *which* path,
    because which one is the Builder's business, not this test's.
    """

    publish(
        watched,
        build_another(
            tmp_path, albums=[AlbumSpec.with_tracks("On Disk", "On Disk", count=1)], version="h"
        ),
    )
    watched.service.reload()

    song = watched.store.songs.by_id(SongId(1))
    assert song is not None
    assert song.file_path.name.endswith((".mp3", ".flac", ".m4a", ".ogg"))
    assert song.file_path.is_file(), "the new library's own files, not the old library's"
    assert watched.service.songs_by_ids([SongId(1)])[0].file_path == song.file_path
