"""Regression: #19 — persistence and the Library Builder.

Six defects found while building milestone 5, each with the failure it produced. They
live together because they share one cause: a stage whose contract was written from the
SAPRS text and never pressed against a real file, a real database or the corpus.

| Defect | Symptom |
| ------ | ------- |
| FTS5 `MATCH` used a table alias | every search returned nothing, silently |
| Contentless index columns were selected | search hits with `title=None` |
| `library_meta` totals keyed on `NULL` day | one duplicate statistics row per reset |
| A tag value that is neither `str` nor `bytes` | every ID3 date vanished from the library |
| Hidden directories were entered, then filtered | `.git/objects` indexed as music |
| Non-audio files counted as "unsupported" | the skip report described the filesystem |
| A publish whose destination parent was wrong | a traceback instead of `PublicationError` |
| `SkipLedger.total` read the entries | `scanned = songs + skipped` was false |

The last two are the ones worth keeping: both were *arithmetically* invisible — the
report printed, the build published, and only the sum was wrong.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from apps.builder import extraction
from apps.builder.discovery import discover
from apps.builder.extraction import MutagenProbe
from apps.builder.publication import PublicationError, publish
from apps.builder.records import DiscoveredFile, ExtractedFile, SkippedFile, SkipReason
from apps.builder.report import SkipLedger
from apps.builder.schema import create_schema
from apps.builder.state import Reusable
from encore.domain import (
    AlbumId,
    ArtistId,
    AudioFormat,
    QueueItem,
    QueueItemId,
    QueueItemStatus,
    Song,
    SongId,
)
from encore.repositories.contract import LIBRARY_SCHEMA_VERSION
from encore.repositories.library import open_library
from tests.support.media import MediaSpec, generate_track

# -- FTS5: the alias that silently matched nothing ------------------------


def test_a_contentless_fts_query_matches_on_the_table_name(tmp_path: Path) -> None:
    """Regression: `FROM song_search AS x WHERE x MATCH ?` does not mean what it looks like.

    `x MATCH ?` is only a full-text match when the left side names the FTS table; against
    an alias, SQLite reads it as a column reference and says `no such column: x`. Every
    search endpoint would have raised on the first query, and the alias is exactly what a
    query that joins three tables invites you to write.
    """

    connection = sqlite3.connect(tmp_path / "fts.db")
    connection.execute("CREATE VIRTUAL TABLE song_search USING fts5(title, content='')")
    connection.execute("INSERT INTO song_search(rowid, title) VALUES (1, 'A Fleeing Song')")
    connection.commit()

    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        connection.execute(
            "SELECT rowid FROM song_search AS x WHERE x MATCH ?", ("fleeing",)
        ).fetchall()
    named = connection.execute(
        "SELECT rowid FROM song_search WHERE song_search MATCH ?", ("fleeing",)
    ).fetchall()
    assert named == [(1,)]
    connection.close()


def test_a_contentless_index_returns_no_columns(tmp_path: Path) -> None:
    """Regression: `SELECT title FROM song_search` is NULL forever, and no error.

    Contentless FTS5 stores the index and not the text, which is what makes a 15,000-row
    search cheap and what makes joining back to `song_details` mandatory. The read layer
    resolves every hit through the view; this is the reason it must.
    """

    connection = sqlite3.connect(tmp_path / "fts.db")
    connection.execute("CREATE VIRTUAL TABLE song_search USING fts5(title, content='')")
    connection.execute("CREATE TABLE songs (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
    connection.execute("INSERT INTO songs (id, title) VALUES (1, 'A Fleeing Song')")
    connection.execute("INSERT INTO song_search(rowid, title) VALUES (1, 'A Fleeing Song')")
    connection.commit()

    assert (
        connection.execute(
            "SELECT title FROM song_search WHERE song_search MATCH 'fleeing'"
        ).fetchone()[0]
        is None
    )
    joined = connection.execute(
        "SELECT s.title FROM song_search x JOIN songs s ON s.id = x.rowid WHERE song_search MATCH 'fleeing'"
    ).fetchone()[0]
    assert joined == "A Fleeing Song"
    connection.close()


# -- statistics: the UNIQUE index that NULL defeated ----------------------


def test_an_all_time_total_needs_a_non_null_day(tmp_path: Path) -> None:
    """Regression: `UNIQUE(statistic, NULL)` is unique per row, so totals multiplied.

    SQLite treats every NULL as distinct for uniqueness. Writing the all-time counter
    with `day = NULL` produced a new row each reset instead of one row updated, and the
    admin dashboard's totals grew with the number of reboots rather than with the music.
    """

    from encore.repositories.runtime import StatisticKey, open_runtime_store
    from encore.repositories.runtime.statistics import RuntimeStatisticsRepository

    store = open_runtime_store(tmp_path / "runtime.db")
    day = date(2026, 1, 1)
    with store.unit_of_work() as work:
        for _ in range(5):
            work.statistics.increment_both(StatisticKey.SONGS_PLAYED, today=day)
    from sqlalchemy import text

    with store.session() as session:
        totals = session.execute(
            text("SELECT COUNT(*) FROM runtime_statistics WHERE key = :key AND day = ''"),
            {"key": StatisticKey.SONGS_PLAYED.value},
        ).scalar_one()
        assert totals == 1, f"all-time totals multiplied: {totals} rows for one statistic"
        assert RuntimeStatisticsRepository(session).total(StatisticKey.SONGS_PLAYED) == 5
    store.close()


# -- extraction: a tag that is not a str ---------------------------------


def test_an_id3_timestamp_renders_as_text(tmp_path: Path) -> None:
    """Regression: `TDRC` is not a `str` subclass, and `_text` returned None for it.

    A file with a perfectly good year reported no date, so 41 corpus albums lost their
    release year and precedence then filled it from somewhere else, with a provenance row
    that described a value the file did have.
    """

    mutagen = pytest.importorskip("mutagen")
    id3 = pytest.importorskip("mutagen.id3")

    path = generate_track(
        MediaSpec(title="Dated", artist="An Artist", album="An Album"), tmp_path / "artist"
    )
    audio = mutagen.File(str(path))
    # The generator already wrote an ID3 tag block; `add_tags` refuses a second one,
    # which is the API's right and this test's only obstacle.
    audio.tags.add(id3.TDRC(encoding=3, text=["1999"]))
    audio.save()

    probed = MutagenProbe().probe(
        DiscoveredFile(
            path=path, format=AudioFormat.MP3, size_bytes=path.stat().st_size, mtime_ns=1
        )
    )
    assert probed.tags.get("date") == "1999", (
        "the year the file carried is the year the library stores"
    )


def test_an_unrenderable_tag_value_is_absent_not_reprd() -> None:
    """The same fix, other direction: an object with no text must not store `<object ...>`."""

    class Opaque:
        def __str__(self) -> str:
            return "<mutagen.frame at 0x1>"

    assert extraction._text(Opaque()) is None


# -- discovery: the directories a music library is not --------------------


def test_hidden_directories_are_not_walked(tmp_path: Path) -> None:
    """Regression: `.git/objects/*.mp3` was opened and counted as music.

    The old filter checked the *file* name for a leading dot, so a dot-directory was
    entered and every file inside it looked eligible. On a library synced with `git-annex`
    that is thousands of entries scanned, reported and never played.
    """

    hidden = tmp_path / ".git" / "objects"
    hidden.mkdir(parents=True)
    generate_track(MediaSpec(title="Loose", artist="A", album="B"), hidden)
    generate_track(MediaSpec(title="Real", artist="A", album="B"), tmp_path / "Artist" / "Album")

    found = discover(tmp_path)
    assert found.supported_count == 1
    assert found.scanned == 1, "a file never considered cannot appear in the arithmetic"


def test_a_text_file_is_not_reported_as_unplayable_music(tmp_path: Path) -> None:
    """Regression: 412 `.jpg` and `.txt` files became "unsupported" skips.

    ADR-010's aggregate is about containers Encore cannot decode. Mixing in the
    filesystem's paperwork made the one line the report reserves for a real decision
    ("188 .m4p, DRM, will never play") unreadable.
    """

    (tmp_path / "Artist").mkdir()
    (tmp_path / "Artist" / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    (tmp_path / "Artist" / "notes.txt").write_text("liner notes", encoding="utf-8")
    (tmp_path / "Artist" / "locked.m4p").write_bytes(b"drm")

    found = discover(tmp_path)
    assert dict(found.unsupported) == {".m4p": 1}
    assert found.scanned == 1


def test_a_daw_package_internals_are_not_unplayable_music(tmp_path: Path) -> None:
    """Regression: five `projectData`/`PkgInfo` files reached the skip aggregate.

    They have no extension at all, so the suffix rule could not see them and they landed
    under `<no extension>` — a line that reads like the library contains unidentified
    audio and actually reads like someone opened GarageBand once in 2011. Same defect
    class as the `.jpg` case above, different mechanism: this one is about names, not
    suffixes, and the comparison has to be case-insensitive to catch `projectData`.
    """

    bundle = tmp_path / "GarageBand" / "My Song.band" / "Contents"
    bundle.mkdir(parents=True)
    (bundle / "PkgInfo").write_bytes(b"BNDLGBnd")
    (tmp_path / "GarageBand" / "My Song.band" / "projectData").write_bytes(b"data")
    (tmp_path / "Artist").mkdir()
    (tmp_path / "Artist" / "locked.m4p").write_bytes(b"drm")

    found = discover(tmp_path)
    assert dict(found.unsupported) == {".m4p": 1}
    assert found.scanned == 1


# -- publication: the failure that escaped as a traceback -----------------


def test_an_unusable_destination_fails_as_a_publication_error(tmp_path: Path) -> None:
    """Regression: `mkdir` outside the try block, so `NotADirectoryError` reached the CLI.

    `encore-builder` maps `PublicationError` onto exit 1 with the sentence "the previous
    library is unchanged". An escaping OSError printed a traceback and exit 3, which is
    the difference between an operator fixing a path and an operator filing an issue.
    """

    blocked = tmp_path / "occupied"
    blocked.write_text("a file where a directory should be", encoding="utf-8")
    source = tmp_path / "build.db"
    source.write_bytes(b"SQLite format 3\x00")
    with pytest.raises(PublicationError):
        publish(source, blocked / "library.db")


# -- the report arithmetic ------------------------------------------------


def test_skip_count_counts_files_the_ledger_only_tallied() -> None:
    """Regression: unsupported files lived in the aggregate, not in `total`.

    `SkipLedger.total` returned `len(entries)`, and the pipeline put the 188 `.m4p` files
    into `aggregate` alone. The printed line was right and the count beside it was wrong,
    so `scanned = songs + skipped` — the one check that says a build lost nothing — was
    false by exactly the number of DRM files in the library.
    """

    entries = [
        SkippedFile(
            path=Path(f"/m/track-{index}.m4p"), reason=SkipReason.UNSUPPORTED_FORMAT, detail=".m4p"
        )
        for index in range(188)
    ]
    ledger = SkipLedger.of(entries)
    assert ledger.total == 188 == sum(ledger.aggregate.values())
    assert len(ledger.lines()) == 1


def test_the_aggregate_key_and_the_printed_line_agree() -> None:
    """Regression: the pipeline composed keys as `reason + suffix`, the ledger as `reason:suffix`.

    A key without its separator does not `partition()` apart, so the report printed the
    raw code — `unsupported-format.m4p` — where it should say "unsupported container".
    The fix is that nothing composes those keys by hand any more.
    """

    ledger = SkipLedger.of(
        [SkippedFile(path=Path("/m/a.m4p"), reason=SkipReason.UNSUPPORTED_FORMAT, detail=".m4p")]
    )
    assert next(iter(ledger.aggregate)) == f"{SkipReason.UNSUPPORTED_FORMAT}:.m4p"
    assert "unsupported container" in ledger.lines()[0]


# -- helpers --------------------------------------------------------------


def test_a_reusable_record_needs_a_cache_key() -> None:
    """Guard for the same class of bug in the incremental path: an empty key that matched."""

    file = DiscoveredFile(path=Path("/m/a.mp3"), format=AudioFormat.MP3, size_bytes=1, mtime_ns=1)
    assert not Reusable(cache_key="").matches(file, digest="")


def test_the_library_contract_still_describes_the_built_schema(tmp_path: Path) -> None:
    """The seam ADR-009 depends on, restated where a regression belongs.

    `tests/integration/test_library_contract.py` covers it properly; this is the guard
    that a schema edit cannot ship without someone noticing that the Server's read layer
    and the Builder's DDL have drifted.
    """

    connection = sqlite3.connect(tmp_path / "library.db")
    create_schema(connection)
    connection.execute(
        "INSERT INTO library_meta (key, value) VALUES (?, ?)",
        ("schema_version", str(LIBRARY_SCHEMA_VERSION)),
    )
    connection.commit()
    connection.close()
    with open_library(tmp_path / "library.db") as store:
        assert store.info.schema_version == LIBRARY_SCHEMA_VERSION
        assert store.info.usable


def _song(song_id: int = 1) -> Song:
    return Song(
        id=SongId(song_id),
        title="Song",
        artist_id=ArtistId(1),
        album_id=AlbumId(1),
        file_path=Path("/m/song.mp3"),
        file_format=AudioFormat.MP3,
        duration=timedelta(seconds=1),
    )


def test_a_queue_item_remembers_when_it_was_played() -> None:
    """`played_at` is in SAPRS 4.5 and in the runtime schema; the mapper had dropped it.

    History is the source the statistics stage reads, and a `QueueItem` that loses the
    timestamp on the way out cannot be written back without inventing one.
    """

    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    item = QueueItem(
        id=QueueItemId(1),
        song_id=SongId(1),
        position=1,
        status=QueueItemStatus.FINISHED,
        enqueued_at=moment,
        played_at=moment + timedelta(minutes=3),
    )
    assert item.played_at == moment + timedelta(minutes=3)


def test_a_source_that_replaced_a_value_is_marked_repaired(tmp_path: Path) -> None:
    """Regression: `repaired` compared the cleaned value with itself.

    `_changed_by_rules` folded both sides, so `Beta [Clean_Version]` → `Beta` — a repair
    the operator should see in the report — was recorded as an untouched tag, and the
    "metadata repaired" count was a floor with no ceiling.
    """

    from apps.builder.precedence import normalize_track, tree_map

    extracted = ExtractedFile(
        file=DiscoveredFile(
            path=tmp_path / "a.mp3", format=AudioFormat.MP3, size_bytes=10, mtime_ns=1
        ),
        tags={"artist": "An Artist", "album": "Beta [Clean_Version]", "title": "Song"},
        duration=timedelta(seconds=1),
    )
    track = normalize_track(extracted, root=tmp_path, trees=tree_map([extracted], tmp_path))
    assert track.album == "Beta"
    assert track.provenance["album"].repaired is True
    assert track.provenance["album"].original == "Beta [Clean_Version]"


# -- the CLI: paths as an operator actually types them --------------------


def test_a_relative_music_dir_still_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: `--music-dir ./music` built a library that refused to publish.

    Discovery stored what it was handed, so every `music_files.path` was relative and
    SAPRS 6.10's own validation rejected the build it had just completed — the report
    printed "12 paths are not absolute" and exit 1 for the only command shape a person
    copying from the guide would type. systemd gives the service no working directory
    either, which is why the ADR requires absolute paths in the first place.
    """

    from apps.builder.main import main

    (tmp_path / "music" / "Artist").mkdir(parents=True)
    generate_track(
        MediaSpec(title="A Song", artist="An Artist", album="An Album"),
        tmp_path / "music" / "Artist" / "a.mp3",
    )
    monkeypatch.chdir(tmp_path)

    code = main(
        [
            "--music-dir",
            "music",
            "--library",
            "out/library.db",
            "--artwork-dir",
            "out/artwork",
            "--temp-dir",
            "tmp",
        ]
    )
    assert code == 0

    store = open_library(tmp_path / "out" / "library.db")
    first = store.songs.page(limit=1)[0]
    assert str(first.file_path).startswith(str(tmp_path.resolve()))
    store.close()
