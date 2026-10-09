"""The read side, through the objects the Server will actually use (SAPRS 5.3, ADR-009).

Everything here runs against a real artifact from a real pipeline run, for the reason
ADR-010 gives: the contract between the Builder and the Server is the thing under test,
and a hand-written schema in a fixture would let both halves be wrong in the same
direction.

What these tests are *not* is a check on SQL. That is
`tests/integration/test_library_contract.py`, and it is a separate file precisely because
the two fail for different reasons: a query that does not resolve is a Builder change
nobody noticed, and a mapper that returns the wrong object is a Server bug.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast, get_type_hints

import pytest

from encore.domain import (
    Album,
    AlbumId,
    Artist,
    ArtistId,
    Artwork,
    ArtworkKind,
    AudioFormat,
    Metadata,
    Song,
    SongId,
)
from encore.repositories.contract import LIBRARY_SCHEMA_VERSION, Table
from encore.repositories.errors import (
    ContractViolationError,
    SearchUnavailableError,
    StoreNotFoundError,
)
from encore.repositories.library import (
    LibraryConnection,
    LibraryStore,
    SongRepository,
    open_library,
)
from encore.repositories.library.mappers import to_artist, to_artwork, to_song
from encore.repositories.library.queries import SONGS_BY_IDS
from encore.repositories.library.search import LibrarySearch
from tests.conftest import BuiltLibrary

#: SQLite reads a negative `LIMIT` as "no limit", which is how `all()` is one query.
NO_LIMIT = -1


def test_the_store_reads_a_built_library(library_store: LibraryStore) -> None:
    assert library_store.info.schema_version == LIBRARY_SCHEMA_VERSION
    assert library_store.info.song_count == library_store.songs.count()


def test_missing_file_is_a_named_failure(tmp_path: Path) -> None:
    """`sqlite3.connect()` would happily *create* the file. That is the bug ADR-009 names."""

    with pytest.raises(StoreNotFoundError) as error:
        open_library(tmp_path / "nope.db")
    assert error.value.path == tmp_path / "nope.db"
    assert "builder" in str(error.value).lower()


def test_an_empty_database_is_refused(tmp_path: Path) -> None:
    """A typo in `paths.library_db` must not boot an appliance with zero songs."""

    path = tmp_path / "library.db"
    sqlite3.connect(path).close()
    with pytest.raises(Exception, match="is not an Encore library") as error:
        open_library(path)
    assert "builder" in str(error.value).lower()


def test_a_foreign_schema_version_is_refused(tmp_path: Path) -> None:
    """The runtime does not migrate the library (ADR-006); it refuses it."""

    from apps.builder import schema

    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    schema.create_schema(connection)
    schema.stamp_meta(
        connection,
        library_version="99.0",
        rules_version=1,
        built_at="2026-01-01T00:00:00+00:00",
        song_count=0,
        file_count=0,
        builder_version="from-the-future",
    )
    connection.execute("UPDATE library_meta SET value = '99' WHERE key = 'schema_version'")
    connection.commit()
    connection.close()

    with pytest.raises(Exception, match="schema version 99") as error:
        open_library(path)
    assert "rebuild" in str(error.value).lower()


def test_song_round_trips_to_the_domain(library_store: LibraryStore) -> None:
    songs = library_store.songs.page(limit=10)
    assert songs
    assert all(isinstance(song, Song) for song in songs)
    first = songs[0]
    assert first.file_path.is_absolute()
    assert first.duration.total_seconds() > 0
    assert isinstance(first.file_format, AudioFormat)
    assert library_store.songs.by_id(first.id) == first


def test_catalogue_references_resolve_without_an_extra_query(library_store: LibraryStore) -> None:
    """The song row carries its artist and album ids, so a page is three reads."""

    song = library_store.songs.page(limit=1)[0]
    artist = library_store.artists.by_id(song.artist_id)
    album = library_store.albums.by_id(song.album_id)
    assert isinstance(artist, Artist)
    assert isinstance(album, Album)
    assert artist.id == song.artist_id


def test_batch_reads_short_circuit_on_empty(library_store: LibraryStore) -> None:
    """`IN ()` is a syntax error; an empty page must be an empty list."""

    assert library_store.songs.by_ids([]) == []
    assert library_store.artists.by_ids([]) == []
    assert library_store.albums.by_ids([]) == []


def test_batch_read_returns_catalogue_order(library_store: LibraryStore) -> None:
    """The queue's order is the queue's business, not the repository's (ADR-009)."""

    songs = library_store.songs.page(limit=4)
    reversed_ids = [song.id for song in songs][::-1]
    assert [int(song.id) for song in library_store.songs.by_ids(reversed_ids)] == sorted(
        int(song.id) for song in songs
    )


def test_album_tracks_are_ordered_disc_then_track(library_store: LibraryStore) -> None:
    album = library_store.albums.all()[0]
    numbers = [
        (song.disc_number or 1, song.track_number or 0)
        for song in library_store.songs.by_album(album.id)
    ]
    assert numbers == sorted(numbers)


def test_artist_browse_lists_that_artist_songs(library_store: LibraryStore) -> None:
    artist = library_store.artists.all()[0]
    songs = library_store.songs.by_artist(artist.id)
    assert songs
    assert {song.artist_id for song in songs} == {artist.id}


def test_pages_are_bounded_and_offset(library_store: LibraryStore) -> None:
    first = library_store.songs.page(limit=2, offset=0)
    second = library_store.songs.page(limit=2, offset=2)
    assert len(first) == 2
    assert {song.id for song in first}.isdisjoint({song.id for song in second})
    assert len(library_store.artists.page(limit=1)) == 1


def test_artwork_reference_is_relative_and_resolvable(
    built_library: BuiltLibrary, library_store: LibraryStore
) -> None:
    """SAPRS 5.6: a path relative to `paths.artwork_dir`, never an absolute one."""

    rows = library_store._connection.rows(f"SELECT id, relative_path FROM {Table.ARTWORK}", dict)
    for row in rows:
        relative = Path(str(row["relative_path"]))
        assert not relative.is_absolute()
        assert (built_library.options.artwork_dir / relative).is_file()


def test_artwork_for_song_resolves_the_album_cover(
    built_library: BuiltLibrary, library_store: LibraryStore
) -> None:
    song = _song_with_artwork(library_store)
    artwork = library_store.artwork.for_song(song.id)
    assert isinstance(artwork, Artwork)
    assert artwork.kind is ArtworkKind.ALBUM
    assert artwork.id == song.artwork_id


def test_a_song_without_artwork_answers_none(library_store: LibraryStore) -> None:
    """SAPRS 6.7: missing artwork must never be invented, only reported."""

    songs = library_store.songs.page(limit=NO_LIMIT)
    plain = [song for song in songs if song.artwork_id is None]
    for song in plain:
        assert library_store.artwork.for_song(song.id) is None


def test_metadata_reassembles_what_the_file_said(library_store: LibraryStore) -> None:
    """SAPRS 4.1 keeps `original`; the admin page shows both sides of a repair."""

    song = library_store.songs.page(limit=1)[0]
    metadata = library_store.files.metadata_for(song.id)
    assert isinstance(metadata, Metadata)
    assert metadata.artist
    assert metadata.title
    assert metadata.duration == song.duration
    assert set(metadata.original) >= {"artist", "title"}


def test_provenance_names_the_source_of_every_field(library_store: LibraryStore) -> None:
    song = library_store.songs.page(limit=1)[0]
    rows = library_store.provenance.for_song(song.id)
    assert rows
    assert {str(row["source"]) for row in rows} == {"tag"}
    assert all(row["field"] in {"artist", "album", "title"} for row in rows)


def test_mapper_reports_a_missing_column_as_a_contract_violation() -> None:
    """ADR-009's cost, made legible: not a `KeyError`, and the column in the message."""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE artists (id INTEGER, name TEXT)")
    connection.execute("INSERT INTO artists VALUES (1, 'Nobody')")
    row = connection.execute("SELECT id, name FROM artists").fetchone()
    with pytest.raises(ContractViolationError) as error:
        to_artist(row)
    assert error.value.column == "sort_name"
    connection.close()


def test_mapper_refuses_a_relative_path() -> None:
    """`Song`'s own invariant, checked where it is cheapest to check (SAPRS 7.7)."""

    row = _song_row(path="relative.mp3")
    with pytest.raises(ContractViolationError, match="path"):
        to_song(row)


def test_a_path_as_the_builder_records_it_is_accepted() -> None:
    row = _song_row(path="/opt/music/Artist/Album/01.mp3")
    assert to_song(row).file_path.is_absolute()


def test_enumeration_mismatch_is_a_contract_violation() -> None:
    """A library built for a kind of artwork Encore does not name."""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE artwork (id INTEGER, kind TEXT, relative_path TEXT, width INTEGER, height INTEGER)"
    )
    connection.execute("INSERT INTO artwork VALUES (1, 'poster', 'a.jpg', 10, 10)")
    row = connection.execute("SELECT * FROM artwork").fetchone()
    with pytest.raises(ContractViolationError, match="kind"):
        to_artwork(row)
    connection.close()


def test_search_returns_hits_with_scores(built_library: BuiltLibrary, sqlite_fts5: None) -> None:
    del sqlite_fts5
    store = open_library(built_library.options.library_db)
    search = _search(store)
    song = store.songs.page(limit=1)[0]
    hits = search.songs(f'"{song.title.split()[0]}"', limit=10)
    assert hits, f"nothing matched the first word of {song.title!r}"
    assert any(hit.song.id == song.id for hit in hits)
    assert all(hit.score <= 0 for hit in hits), (
        "bm25 is negative-good; a positive score is a mistake"
    )
    store.close()


def test_search_refuses_a_store_without_the_index(built_library: BuiltLibrary) -> None:
    """Distinct from "no results", which SAPRS 11.2 says the operator must be told."""

    store = open_library(built_library.options.library_db)
    search = LibrarySearch(store._connection, available=False)
    with pytest.raises(SearchUnavailableError):
        search.songs("anything", limit=5)
    store.close()


def test_counts_agree_with_the_stamp(library_store: LibraryStore) -> None:
    """`library_meta` says how many songs the Builder wrote; the rows must agree."""

    counts = library_store.counts()
    assert counts[Table.SONGS] == library_store.info.song_count
    assert counts[Table.MUSIC_FILES] == counts[Table.SONGS]
    assert counts[Table.ARTISTS] >= 1


def test_the_connection_holds_one_file_handle(built_library: BuiltLibrary) -> None:
    connection = LibraryConnection(built_library.options.library_db)
    assert connection.path == built_library.options.library_db
    connection.close()
    connection.close()  # idempotent, so a failed startup cannot leak it


def test_a_write_through_the_servers_own_connection_is_refused_by_sqlite(
    built_library: BuiltLibrary,
) -> None:
    """#19 criterion 2, proven rather than asserted.

    The separation test in `test_database_separation.py` proves a *test helper's*
    read-only connection refuses a write. That is not the claim ADR-009 makes. Its
    argument is that the driver choice is irrelevant and the URI does the work, so the
    only test that matters is one that reaches for `INSERT` through the same connection
    the Server uses — the one built by `open_library()`, from the same URI builder,
    through the same `_execute` path a real bug would take.

    It expects SQLite itself to refuse, not a repository guard: a check in
    `LibraryConnection` would pass this test while leaving the file writable to anyone
    who constructed the connection differently, which is precisely how this fails in
    practice.
    """

    store = open_library(built_library.options.library_db)
    # `.raw` is the package's own escape hatch and exists for this check: the point of
    # the test is that nothing above it is what stops a write.
    raw = store._connection.raw
    with pytest.raises(sqlite3.OperationalError):
        raw.execute(f"INSERT INTO {Table.ARTISTS} (name, sort_name) VALUES (?, ?)", ("x", "x"))
    with pytest.raises(sqlite3.OperationalError):
        raw.execute("CREATE TABLE a_backdoor (id INTEGER)")
    with pytest.raises(sqlite3.OperationalError):
        raw.execute(f"DELETE FROM {Table.SONGS}")
    store.close()

    # And the file is genuinely untouched, not merely the handle polite.
    probe = sqlite3.connect(built_library.options.library_db)
    assert probe.execute(f"SELECT COUNT(*) FROM {Table.ARTISTS}").fetchone()[0] >= 1
    assert (
        probe.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'a_backdoor'").fetchone()[0]
        == 0
    )
    probe.close()


def test_one_refuses_a_statement_that_returned_two_rows(built_library: BuiltLibrary) -> None:
    connection = LibraryConnection(built_library.options.library_db)
    with connection, pytest.raises(ContractViolationError, match="row count"):
        connection.one(f"SELECT * FROM {Table.ARTISTS}", lambda row: row)


def test_the_repositories_share_the_mappers() -> None:
    """A repository that inlined its own mapping would let the two drift unnoticed."""

    from encore.repositories.library import mappers

    assert mappers.to_song is to_song


def test_identifiers_are_the_newtypes_the_domain_declares(library_store: LibraryStore) -> None:
    """SAPRS 4.1's identifiers, so `by_album(song.id)` is a type error and not a bug.

    Asserted on the annotations rather than on `type(value)`: `NewType` is erased at
    runtime and returns a plain `int`, which is the whole reason it costs nothing and
    the whole reason a runtime check would be a lie.
    """

    hints = get_type_hints(Song)
    song = library_store.songs.page(limit=1)[0]
    assert hints["id"] is SongId
    assert hints["artist_id"] is ArtistId
    assert hints["album_id"] is AlbumId
    assert isinstance(song.id, int)
    assert isinstance(song.artist_id, int)


def test_batch_statement_carries_one_placeholder_per_id() -> None:
    assert SONGS_BY_IDS.replace("{ids}", ", ".join("?" for _ in range(3))).count("?") == 3


def test_store_close_is_idempotent(built_library: BuiltLibrary) -> None:
    store = open_library(built_library.options.library_db)
    store.close()
    store.close()


def test_the_song_repository_is_reusable_across_reads(library_store: LibraryStore) -> None:
    """It holds a connection, not a cursor, so a second call is not a closed statement."""

    repository = library_store.songs
    assert isinstance(repository, SongRepository)
    assert repository.page(limit=1)
    assert repository.page(limit=1)


def _song_with_artwork(store: LibraryStore) -> Song:
    songs = store.songs.page(limit=NO_LIMIT)
    with_artwork = [song for song in songs if song.artwork_id is not None]
    assert with_artwork, "the fixture library has no artwork at all"
    return with_artwork[0]


def _search(store: LibraryStore) -> LibrarySearch:
    return LibrarySearch(store._connection, available=store.info.fts_available)


def _song_row(*, path: str) -> sqlite3.Row:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE songs (id INTEGER, title TEXT, sort_title TEXT, artist_id INTEGER,"
        " album_id INTEGER, music_file_id INTEGER, track_number INTEGER, disc_number INTEGER,"
        " artwork_id INTEGER)"
    )
    connection.execute("CREATE TABLE music_files (path TEXT, format TEXT, duration_seconds REAL)")
    connection.execute("INSERT INTO songs VALUES (1, 'x', 'x', 1, 1, 1, 1, 1, NULL)")
    connection.execute("INSERT INTO music_files VALUES (?, 'mp3', 1.5)", (path,))
    row = connection.execute(
        "SELECT s.id, s.title, s.sort_title, s.artist_id, s.album_id, s.music_file_id,"
        " s.track_number, s.disc_number, s.artwork_id, f.path, f.format, f.duration_seconds"
        " FROM songs s, music_files f"
    ).fetchone()
    connection.close()
    assert row is not None
    return cast("sqlite3.Row", row)
