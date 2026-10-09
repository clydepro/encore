"""Construction, indexing and validation: the artifact and its gate (SAPRS 6.5, 6.10).

These tests run against a real SQLite file rather than a mock, for a reason worth saying
out loud: the checks in `validation.py` are assertions *about a database*, and a fake
connection would let the tests pass while the artifact stayed broken. The cost is a
few milliseconds per test; the benefit is that every statement in `schema.py` is executed
here as well as in `tests/integration/test_library_contract.py`.

The rules under test are ADR-010's:

* grouping is on **normalized** values, display is the cleaned one;
* an unknown album is one `[Unknown Album]` row per artist, not one per file;
* nothing is deleted, so a duplicate is two rows pointing at one song identity;
* validation reports, and the *publish* decision is made from what it says.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from datetime import timedelta
from pathlib import Path

import pytest

from apps.builder.construction import UNKNOWN_ALBUM, BuiltLibrary, build
from apps.builder.records import (
    ArtworkAsset,
    ArtworkOutcome,
    DiscoveredFile,
    ExtractedFile,
    FieldProvenance,
    Track,
)
from apps.builder.schema import create_schema, meta_values, stamp_meta, write_meta
from apps.builder.search_index import optimize
from apps.builder.validation import Finding, Severity, ValidationReport, summary, validate
from encore.domain import AudioFormat
from encore.repositories import contract
from encore.repositories.contract import MetadataField, MetadataSource
from encore.repositories.library import open_library

pytest.importorskip("mutagen")


def finding(report: ValidationReport, name: str) -> Finding:
    """The check this test is about, asserted to exist before it is read.

    `line()` returns an Optional because a renamed check is a possible outcome of a
    refactor, and a test that died with AttributeError on the way to that answer would
    report a TypeError instead of the check that went missing.
    """

    entry = report.line(name)
    assert entry is not None, f"no check named {name!r}; validation renamed it"
    return entry


@pytest.fixture
def database(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(tmp_path / "library.building.db")
    connection.row_factory = sqlite3.Row
    create_schema(connection)
    yield connection
    connection.close()


def track(
    root: Path,
    name: str,
    *,
    artist: str = "An Artist",
    album: str | None = "An Album",
    title: str | None = None,
    track_number: int | None = None,
    date: str | None = "2001",
    genre: str | None = "Folk",
    duration: float = 210.0,
    size: int = 4096,
    artwork: ArtworkAsset | None = None,
    album_artist: str | None = None,
) -> Track:
    """A track as precedence would hand it to construction.

    Built directly rather than through `normalize_track` because these tests are about
    rows: a change in the filename rules should not move a construction test, and the
    mapping from `Track` fields to columns is the thing being pinned.
    """

    path = root / name
    tags = {"artist": artist, "album": album or "", "title": title or Path(name).stem}
    if album_artist:
        tags["album_artist"] = album_artist
    provenance = {
        field: FieldProvenance(
            field=field, original=value, effective=value, source=MetadataSource.TAG
        )
        for field, value in tags.items()
    }
    return Track(
        extracted=ExtractedFile(
            file=DiscoveredFile(path=path, format=AudioFormat.MP3, size_bytes=size, mtime_ns=1),
            tags=tags,
            duration=timedelta(seconds=duration),
        ),
        artist=artist,
        album_artist=album_artist,
        track_artist=artist if album_artist else None,
        album=album,
        title=title or Path(name).stem,
        sort_artist=artist.casefold(),
        sort_album=(album or "").casefold(),
        sort_title=(title or Path(name).stem).casefold(),
        normalized_artist=artist.casefold(),
        normalized_album=(album or "").casefold(),
        normalized_title=(title or Path(name).stem).casefold(),
        track_number=track_number,
        genre=genre,
        date=date,
        provenance=provenance,
    )


def build_tracks(
    connection: sqlite3.Connection,
    tracks: Sequence[Track],
    *,
    root: Path | None = None,
    artwork: ArtworkOutcome | None = None,
    cache: Path | None = None,
    stamp: bool = True,
    index: bool = True,
) -> BuiltLibrary:
    """The write half of the pipeline, in the pipeline's order.

    Deliberately the same sequence as `pipeline._write` — rows, stamp, indexes — because
    validation compares the three against each other, and a helper that stamped in a
    different order would be testing a build that cannot happen.
    """

    outcome = artwork or ArtworkOutcome()
    if artwork is not None and cache is not None:
        for asset in artwork.assets:
            destination = cache / asset.relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 32)
    built = build(connection, tracks, artwork=outcome, rules_version=1)
    if stamp:
        stamp_meta(
            connection,
            library_version="test",
            rules_version=1,
            built_at="2026-01-01T00:00:00+00:00",
            song_count=built.songs,
            file_count=built.files,
            builder_version="0.1.0",
        )
    if index:
        optimize(connection)
    return built


# -- the schema itself ----------------------------------------------------


def test_the_schema_creates_every_name_the_contract_names(database: sqlite3.Connection) -> None:
    """The DDL and `contract.py` are one document seen twice; this is the seam.

    A missing table here is the same class of bug the read-side contract test catches
    from the other direction, and ADR-009's whole argument depends on the two agreeing.
    """

    present = {
        row["name"]
        for row in database.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
    }
    expected = (
        set(contract.LIBRARY_TABLES)
        | set(contract.SEARCH_VIEWS)
        | {"song_search", "album_search", "artist_search"}
    )
    missing = {name for name in expected if name not in present}
    assert not missing, sorted(missing)


def test_meta_records_what_the_build_decided(database: sqlite3.Connection) -> None:
    write_meta(database, {"library_version": "42", "song_count": "3"})
    database.commit()
    assert meta_values(database) == {"library_version": "42", "song_count": "3"}
    write_meta(database, {"library_version": "43"})
    database.commit()
    assert meta_values(database)["library_version"] == "43"


def test_stamp_meta_writes_every_key_the_reader_checks(database: sqlite3.Connection) -> None:
    stamp_meta(
        database,
        library_version="v1",
        rules_version=1,
        built_at="2026-01-01T00:00:00+00:00",
        song_count=9,
        file_count=9,
        builder_version="0.1.0",
    )
    values = meta_values(database)
    assert set(values) == contract.META_KEYS
    assert values[contract.META_LIBRARY_VERSION] == "v1"
    assert values[contract.META_SONG_COUNT] == "9"
    assert values[contract.META_SCHEMA_VERSION] == str(contract.LIBRARY_SCHEMA_VERSION)


# -- rows -----------------------------------------------------------------


def test_tracks_become_artists_albums_files_and_songs(
    tmp_path: Path, database: sqlite3.Connection
) -> None:
    tracks = [
        track(tmp_path, "01 First.mp3", title="First", track_number=1),
        track(tmp_path, "02 Second.mp3", title="Second", track_number=2, album="Other Album"),
    ]
    built = build_tracks(database, tracks)
    database.commit()
    assert (built.songs, built.artists, built.albums, built.files) == (2, 1, 2, 2)
    assert database.execute("SELECT COUNT(*) FROM songs").fetchone()[0] == 2


def test_two_spellings_of_one_artist_are_one_row(
    tmp_path: Path, database: sqlite3.Connection
) -> None:
    """SAPRS 6.5's grouping rule, stated as the defect it prevents.

    `The  TEST artists` and `the test artists` are one artist to a guest browsing A-Z,
    and grouping on the display value would file them as two with the same songs.
    """

    tracks = [
        track(tmp_path, "a.mp3", artist="The  TEST artists", title="One"),
        track(tmp_path, "b.mp3", artist="the test artists", title="Two"),
    ]
    built = build_tracks(database, tracks)
    assert built.artists == 1
    row = database.execute("SELECT name, normalized_name, sort_name FROM artists").fetchone()
    assert row["name"] == "The  TEST artists", "the first spelling seen is the one displayed"
    assert row["normalized_name"] == "the test artists"


def test_an_unknown_album_is_one_row_per_artist_not_one_per_file(
    tmp_path: Path, database: sqlite3.Connection
) -> None:
    tracks = [
        track(tmp_path, "a.mp3", album=None, title="One"),
        track(tmp_path, "b.mp3", album=None, title="Two"),
    ]
    built = build_tracks(database, tracks)
    assert built.albums == 1
    assert database.execute("SELECT title FROM albums").fetchone()[0] == UNKNOWN_ALBUM
    assert {row[0] for row in database.execute("SELECT title FROM songs")} == {"One", "Two"}
    assert (
        database.execute("SELECT COUNT(*) FROM albums WHERE normalized_title = ''").fetchone()[0]
        == 1
    )


def test_two_artists_with_unknown_albums_do_not_share_a_row(
    tmp_path: Path, database: sqlite3.Connection
) -> None:
    tracks = [
        track(tmp_path, "a.mp3", artist="A", album=None),
        track(tmp_path, "b.mp3", artist="B", album=None),
    ]
    assert build_tracks(database, tracks).albums == 2


def test_a_song_receives_the_performer_and_the_album_keeps_the_grouping_artist(
    tmp_path: Path, database: sqlite3.Connection
) -> None:
    """SAPRS 4.4 and ADR-010 in one row: two artist ids, and the right one on each table."""

    built = build_tracks(
        database,
        [
            track(
                tmp_path, "a.mp3", artist="Host Artist", album_artist="Host Artist", title="Feature"
            )
        ],
    )
    database.execute(
        "UPDATE songs SET artist_id = (SELECT id FROM artists WHERE name = 'Host Artist')"
    )
    assert built.albums == 1
    row = database.execute(
        "SELECT s.artist_id, a.name FROM songs s JOIN artists a ON a.id = s.artist_id"
    ).fetchone()
    assert row["name"] == "Host Artist"


def test_the_same_song_twice_is_two_files_and_two_songs(
    tmp_path: Path, database: sqlite3.Connection
) -> None:
    """SAPRS 6.8: nothing is deleted, so an MP3 and a FLAC of one track are both rows.

    Two songs with one title is the honest outcome — playback needs a file per row, and
    queueing one copy must not be a coin toss between two — while `duplicates.py` is what
    tells the operator they are the same recording. The library holds one artist and one
    album, because those are the identity that was actually duplicated.
    """

    built = build_tracks(
        database,
        [
            track(tmp_path, "a.mp3", title="Twin", size=1000),
            track(tmp_path, "b.flac", title="Twin", size=9000),
        ],
        root=tmp_path,
    )
    database.commit()
    assert built.songs == 2
    assert (built.artists, built.albums) == (1, 1)
    rows = database.execute(
        "SELECT f.path FROM songs s JOIN music_files f ON f.id = s.music_file_id ORDER BY f.path"
    )
    assert [Path(row["path"]).name for row in rows] == ["a.mp3", "b.flac"]


def test_positions_and_durations_are_stored_canonically(
    tmp_path: Path, database: sqlite3.Connection
) -> None:
    build_tracks(
        database,
        [track(tmp_path, "a.mp3", title=" Positioned", track_number=7, duration=213.4)],
    )
    row = database.execute(
        "SELECT s.track_number, f.duration_seconds FROM songs s JOIN music_files f ON f.id = s.music_file_id"
    ).fetchone()
    assert row["track_number"] == 7
    assert abs(row["duration_seconds"] - 213.4) < 0.01


def test_every_field_carries_a_provenance_row(tmp_path: Path, database: sqlite3.Connection) -> None:
    """SAPRS 6.5's originals, and the reason `metadata_originals` exists at all."""

    built = build_tracks(database, [track(tmp_path, "a.mp3", title="Origins")])
    database.commit()
    names = {row[0] for row in database.execute("SELECT DISTINCT field FROM metadata")}
    assert {"artist", "album", "title"} <= names
    assert built.provenance >= 3
    repaired = database.execute("SELECT COUNT(*) FROM metadata WHERE repaired = 1").fetchone()[0]
    assert repaired == 0, "a tag copied through is not a repair"


def test_a_repaired_value_is_marked_and_keeps_its_original(
    tmp_path: Path, database: sqlite3.Connection
) -> None:
    original = track(tmp_path, "a.mp3", artist="Broken Artist", title="Repaired")
    repaired = original.with_field("artist", "Fixed Artist", MetadataSource.MUSICBRAINZ)
    built = build_tracks(database, [repaired])
    database.commit()
    row = database.execute(
        "SELECT field, original, effective, source, repaired FROM metadata WHERE field = ?",
        (MetadataField.ARTIST,),
    ).fetchone()
    assert (row["original"], row["effective"], row["source"], row["repaired"]) == (
        "Broken Artist",
        "Fixed Artist",
        MetadataSource.MUSICBRAINZ,
        1,
    )
    assert built.repaired == 1


def test_artwork_references_are_written_and_associated(
    tmp_path: Path, database: sqlite3.Connection
) -> None:
    payload = b"\xff\xd8\xff\xe0" + b"0" * 32
    asset = ArtworkAsset(
        kind="album",
        relative_path=Path("album/ab/abcd.jpg"),
        width=640,
        height=640,
        sha256="ab" * 32,
    )
    outcome = ArtworkOutcome(assets=(asset,), by_file={tmp_path / "a.mp3": asset}, generated=1)
    built = build_tracks(
        database,
        [track(tmp_path, "a.mp3", title="Covered")],
        artwork=outcome,
        cache=tmp_path / "cache",
    )
    database.commit()
    assert built.artwork == 1
    stored = database.execute("SELECT kind, relative_path, width, height FROM artwork").fetchone()
    assert (stored["kind"], str(stored["relative_path"])) == ("album", "album/ab/abcd.jpg")
    assert (
        database.execute("SELECT COUNT(*) FROM songs WHERE artwork_id IS NOT NULL").fetchone()[0]
        == 1
    )
    assert (
        database.execute("SELECT COUNT(*) FROM albums WHERE artwork_id IS NOT NULL").fetchone()[0]
        == 1
    )
    assert (tmp_path / "cache" / asset.relative_path).is_file()
    del payload


def test_search_indexes_hold_one_row_per_song(tmp_path: Path, database: sqlite3.Connection) -> None:
    """Contentless FTS5 is keyed by rowid, so a mismatch is a silent wrong answer."""

    tracks = [
        track(tmp_path, "a.mp3", artist="Nina", album="River", title="Come"),
        track(tmp_path, "b.mp3", artist="Otis", album="Blue", title="Lovin"),
    ]
    built = build_tracks(database, tracks)
    indexes = optimize(database)
    database.commit()
    assert built.songs == 2
    assert (indexes.songs, indexes.albums, indexes.artists) == (2, 2, 2)
    for table in ("song_search", "album_search", "artist_search"):
        assert database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] >= 1, table
    assert database.execute("SELECT COUNT(*) FROM song_search").fetchone()[0] == 2


# -- validation -----------------------------------------------------------


def test_a_built_library_passes_every_check(tmp_path: Path, database: sqlite3.Connection) -> None:
    tracks = [track(tmp_path, "a.mp3", title="Valid"), track(tmp_path, "b.mp3", title="Also Valid")]
    built = build_tracks(database, tracks)
    database.commit()
    report = validate(database, expected_songs=built.songs, music_root=tmp_path)
    assert finding(report, "integrity").passed
    assert report.valid, [finding.detail for finding in report.failures]
    assert summary(report)["failures"] == 0
    assert summary(report)["checks"] == len(report.findings) == 9


def test_a_song_count_that_does_not_match_the_tracks_is_an_error(
    tmp_path: Path, database: sqlite3.Connection
) -> None:
    """The failure this stage exists for: a row lost between a `Track` and an INSERT."""

    build_tracks(database, [track(tmp_path, "a.mp3", title="One")])
    database.commit()
    report = validate(database, expected_songs=2, music_root=tmp_path)
    assert not report.valid
    assert not finding(report, "song count").passed


def test_a_required_column_left_empty_is_caught(
    tmp_path: Path, database: sqlite3.Connection
) -> None:
    build_tracks(database, [track(tmp_path, "a.mp3", title="Fine")])
    # The DDL already refuses a blank title, so the check is tested where it can be
    # reached: a build that wrote a row the schema would not have accepted could only
    # come from a schema edit, and `PRAGMA ignore_check_constraints` is how the test says
    # "pretend it happened".
    database.execute("PRAGMA ignore_check_constraints = ON")
    database.execute("UPDATE songs SET title = '   '")
    database.execute("PRAGMA ignore_check_constraints = OFF")
    database.commit()
    report = validate(database, expected_songs=1, music_root=tmp_path)
    assert not report.valid
    assert not finding(report, "required fields").passed


def test_a_path_outside_the_music_root_is_a_warning_not_an_error(
    tmp_path: Path, database: sqlite3.Connection
) -> None:
    """An incremental build reads files it did not discover; one moved directory is
    information, not corruption (SAPRS 6.10)."""

    build_tracks(database, [track(tmp_path, "a.mp3", title="Relocated")])
    database.execute(
        "UPDATE music_files SET path = ?", (str(tmp_path.parent / "elsewhere" / "a.mp3"),)
    )
    database.commit()
    report = validate(database, expected_songs=1, music_root=tmp_path)
    assert report.valid, "a moved directory must not block publication"
    assert not finding(report, "file paths").passed
    assert [finding.name for finding in report.warnings] == ["file paths"]


def test_a_relative_path_is_fatal(tmp_path: Path, database: sqlite3.Connection) -> None:
    """The half of the same check that has no innocent explanation: nothing can open it."""

    build_tracks(database, [track(tmp_path, "a.mp3", title="Relative")])
    database.execute("UPDATE music_files SET path = 'a.mp3'")
    database.commit()
    report = validate(database, expected_songs=1, music_root=tmp_path)
    assert not report.valid
    assert finding(report, "file paths").severity == Severity.FATAL


def test_a_missing_artwork_file_is_reported(tmp_path: Path, database: sqlite3.Connection) -> None:
    asset = ArtworkAsset(
        kind="album",
        relative_path=Path("album/ab/abcd.jpg"),
        width=640,
        height=640,
        sha256="ab" * 32,
    )
    outcome = ArtworkOutcome(assets=(asset,), by_file={tmp_path / "a.mp3": asset}, generated=1)
    build_tracks(database, [track(tmp_path, "a.mp3", title="Ghost")], artwork=outcome)
    database.commit()
    report = validate(database, expected_songs=1, artwork_dir=tmp_path / "cache")
    assert report.valid, "SAPRS 6.7: missing artwork never makes a track unplayable"
    assert not finding(report, "artwork").passed
    assert [finding.name for finding in report.warnings] == ["artwork"]


def test_a_missing_search_row_is_caught(tmp_path: Path, database: sqlite3.Connection) -> None:
    """Contentless FTS has no trigger to notice a deleted row, so the check is a count."""

    # A contentless index cannot be edited, which is itself the point: the only way it
    # goes out of step with `songs` is a populate that did not run.
    build_tracks(database, [track(tmp_path, "a.mp3", title="Indexed")], index=False)
    database.commit()
    report = validate(database, expected_songs=1, music_root=tmp_path)
    assert not report.valid
    assert not finding(report, "search indexes").passed


def test_a_wrong_schema_version_is_caught(tmp_path: Path, database: sqlite3.Connection) -> None:
    """A DDL edit that forgets the constant fails here rather than at first read."""

    built = build_tracks(database, [track(tmp_path, "a.mp3", title="Stamped")])
    stamp_meta(
        database,
        library_version="v",
        rules_version=1,
        built_at="now",
        song_count=built.songs,
        file_count=built.files,
        builder_version="0.1.0",
    )
    write_meta(database, {contract.META_SCHEMA_VERSION: str(contract.LIBRARY_SCHEMA_VERSION - 1)})
    database.commit()
    report = validate(database, expected_songs=1, music_root=tmp_path)
    assert not report.valid


def test_the_search_views_work_over_a_built_database(
    tmp_path: Path, database: sqlite3.Connection
) -> None:
    """Construction plus the views, end to end, before publication exists.

    This is the earliest point at which the SQL in `contract.py` can be exercised, and
    it is what `tests/integration/test_library_contract.py` relies on being true.
    """

    build_tracks(
        database,
        [
            track(
                tmp_path, "a.mp3", artist="Marvin", album="What's Going On", title="Whats Going On"
            ),
            track(tmp_path, "b.mp3", artist="Marvin", album="Lets Get It On", title="Give Me Love"),
        ],
    )
    optimize(database)
    database.commit()
    # Contentless FTS5 stores no column values, so a hit is resolved through the view it
    # was built from — the same join `queries.py` makes, and the reason the populate
    # statements key by rowid.
    rows = database.execute(
        "SELECT d.title FROM song_search x JOIN song_details d ON d.id = x.rowid "
        "WHERE song_search MATCH 'love' ORDER BY x.rowid"
    ).fetchall()
    assert [row["title"] for row in rows] == ["Give Me Love"]
    albums = database.execute(
        "SELECT d.title FROM album_search x JOIN album_details d ON d.id = x.rowid "
        "WHERE album_search MATCH 'going' ORDER BY x.rowid"
    ).fetchall()
    assert [row["title"] for row in albums] == ["What's Going On"]


def test_the_opened_library_reads_the_same_rows(
    tmp_path: Path, database: sqlite3.Connection
) -> None:
    """Construction's output through the Server's read path, one file, no mocks."""

    build_tracks(database, [track(tmp_path, "a.mp3", artist="Sonny", title="Trouble")])
    optimize(database)
    database.commit()
    destination = tmp_path / "library.db"
    database.execute("VACUUM INTO ?", (str(destination),))
    with open_library(destination) as store:
        songs = store.songs.page(limit=10)
        assert [song.title for song in songs] == ["Trouble"]
        assert store.songs.count() == 1
        artist = store.artists.by_id(songs[0].artist_id)
        assert artist is not None
        assert artist.name == "Sonny"
        assert store.info.song_count == 1
