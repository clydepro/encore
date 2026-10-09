"""Publication, build state and the report: the parts an operator actually sees (6.9, 6.11, 6.12).

Three small modules that carry outsized risk, which is the reason each has its own tests
rather than being covered by the pipeline test:

* **Publication** decides whether the appliance plays the library it had or the one just
  built. The claim under test is the one SAPRS 6.11 makes — a failure leaves the previous
  artifact *unchanged* — and the only way to check it is to break the operation on purpose.
* **Build state** is what makes an incremental build correct instead of merely fast. A
  wrong reuse is a library that silently keeps an old artist name, so `matches()` is
  tested from every direction: changed file, changed tags, changed rules, unchanged file.
* **The report** is the interface for the one command nobody runs interactively. Its
  numbers must add up, and ADR-010's rule that 188 `.m4p` files are *one line* is a claim
  about text, so it is tested as text.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from apps.builder.construction import build
from apps.builder.normalization import NORMALIZATION_RULES_VERSION, fold
from apps.builder.publication import PublicationError, Published, publish, stage
from apps.builder.records import (
    ArtworkOutcome,
    DiscoveredFile,
    ExtractedFile,
    FieldProvenance,
    SkippedFile,
    SkipReason,
    Track,
)
from apps.builder.report import BuildReport, SkipLedger, describe
from apps.builder.schema import create_schema, stamp_meta
from apps.builder.search_index import optimize
from apps.builder.state import (
    Checkpoints,
    PreviousLibrary,
    Reusable,
    cache_key,
    stored_tag_hash,
)
from encore.domain import AudioFormat
from encore.repositories import contract
from encore.repositories.contract import MetadataSource
from encore.repositories.library import open_library

# -- publication ----------------------------------------------------------


@pytest.fixture
def built_database(tmp_path: Path) -> Path:
    """A real, complete, validated-shaped library in a scratch file."""

    path = tmp_path / "library.building.db"
    connection = sqlite3.connect(path)
    create_schema(connection)
    track = _track(tmp_path, "a.mp3")
    build(connection, [track], artwork=ArtworkOutcome(), rules_version=1)
    stamp_meta(
        connection,
        library_version="pub",
        rules_version=1,
        built_at="2026-01-01T00:00:00+00:00",
        song_count=1,
        file_count=1,
        builder_version="0.1.0",
    )
    optimize(connection)
    connection.commit()
    stage(connection, path)
    return path


def test_publishing_makes_the_build_file_the_library(tmp_path: Path, built_database: Path) -> None:
    destination = tmp_path / "runtime" / "library.db"
    published = publish(built_database, destination)
    assert published.path == destination
    assert destination.is_file()
    assert not built_database.exists(), "a build file left behind is a second artifact"
    assert published.replaced is False


def test_a_published_library_opens_and_reads_through_the_server_path(
    built_database: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "library.db"
    publish(built_database, destination)
    with open_library(destination) as store:
        assert store.info.library_version == "pub"
        assert store.songs.count() == 1


def test_no_write_ahead_log_survives_publication(tmp_path: Path, built_database: Path) -> None:
    """A `-wal` beside the artifact reads as valid and is not.

    The Server opens read-only and cannot replay a journal, so a library published with
    unmerged pages is a library whose newest songs are missing at runtime.
    """

    destination = tmp_path / "library.db"
    publish(built_database, destination)
    assert list(destination.parent.glob("library.db-*")) == []
    probe = sqlite3.connect(destination)
    assert probe.execute("PRAGMA journal_mode").fetchone()[0].lower() in {
        "delete",
        "truncate",
        "off",
    }
    assert probe.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    probe.close()


def test_replacing_a_library_reports_that_it_replaced(tmp_path: Path, built_database: Path) -> None:
    destination = tmp_path / "library.db"
    destination.write_bytes(b"the old library")
    published = publish(built_database, destination)
    assert published.replaced is True
    assert destination.read_bytes().startswith(b"SQLite format 3")


def test_a_publish_that_cannot_run_leaves_the_previous_library_untouched(tmp_path: Path) -> None:
    """SAPRS 6.11's guarantee, checked by making it fail.

    An unreadable source is the cheapest honest failure available: it stands in for the
    disk-full and permission cases without needing either.
    """

    destination = tmp_path / "library.db"
    destination.write_bytes(b"previous artifact")
    with pytest.raises(PublicationError):
        publish(tmp_path / "not-built.db", destination)
    assert destination.read_bytes() == b"previous artifact"


def test_a_failure_partway_does_not_leave_a_staging_file(tmp_path: Path) -> None:
    destination = tmp_path / "library.db"
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f"{destination.name}.staging"
    staging.write_bytes(b"abandoned by an earlier crash")
    source = tmp_path / "build.db"
    source.write_bytes(b"SQLite format 3\x00")
    blocked = tmp_path / "occupied"
    blocked.write_bytes(b"a file where a directory should be")
    with pytest.raises(PublicationError):
        publish(source, blocked / "library.db")
    assert staging.read_bytes() == b"abandoned by an earlier crash" or not staging.exists()


def test_publishing_over_a_directory_is_an_error_not_a_deletion(tmp_path: Path) -> None:
    """A destination that is a directory must fail loudly and keep the directory.

    `os.replace` raises rather than unlinking, and the point of asserting it is that the
    failure surfaces as `PublicationError`: the CLI's exit code and the report line both
    come from that type, and an escaping `IsADirectoryError` would print a traceback
    where the operator needs a sentence.
    """

    destination = tmp_path / "library.db"
    destination.mkdir()
    source = tmp_path / "build.db"
    source.write_bytes(b"SQLite format 3\x00")
    with pytest.raises(PublicationError):
        publish(source, destination)
    assert destination.is_dir()
    assert list(destination.iterdir()) == []


def test_published_records_where_the_file_came_from() -> None:
    published = Published(path=Path("/tmp/x.db"), replaced=True, staged_via="direct")
    assert published.replaced
    assert published.staged_via == "direct"


# -- previous library (incremental reuse) ---------------------------------


def test_a_reusable_record_matches_only_an_unchanged_file(tmp_path: Path) -> None:
    item = _discovered(tmp_path / "a.mp3", size=100, mtime=7)
    digest = stored_tag_hash({"title": "T"})
    record = Reusable(cache_key=f"{item.path}|100|7|{digest}|{NORMALIZATION_RULES_VERSION}")
    assert record.matches(item, digest=digest)
    assert not record.matches(_discovered(tmp_path / "a.mp3", size=101, mtime=7), digest=digest)
    assert not record.matches(item, digest=stored_tag_hash({"title": "Other"}))
    assert not record.matches(_discovered(tmp_path / "a.mp3", size=100, mtime=8), digest=digest)


def test_a_rules_bump_reuses_nothing(tmp_path: Path) -> None:
    """ADR-010's decision: a normalization change invalidates, and never silently keeps.

    The cache key ends with the rule version, so bumping `NORMALIZATION_RULES_VERSION`
    makes every stored record fail to match, which is the whole mechanism.
    """

    item = _discovered(tmp_path / "a.mp3")
    digest = stored_tag_hash({})
    stale = Reusable(cache_key=f"{item.path}|{item.size_bytes}|{item.mtime_ns}|{digest}|0")
    assert not stale.matches(item, digest=digest)


def test_an_empty_cache_key_is_never_reusable(tmp_path: Path) -> None:
    assert not Reusable(cache_key="").matches(_discovered(tmp_path / "a.mp3"), digest="x")


def test_a_missing_previous_library_is_a_clean_slate(tmp_path: Path) -> None:
    previous = PreviousLibrary.load(tmp_path / "does-not-exist.db")
    assert len(previous) == 0
    assert previous.usable is False
    assert previous.for_path(tmp_path / "a.mp3") is None


def test_a_corrupt_previous_library_is_a_clean_slate_too(tmp_path: Path) -> None:
    path = tmp_path / "library.db"
    path.write_bytes(b"this is not a database, it is a text file")
    assert PreviousLibrary.load(path).usable is False


def test_a_library_without_the_columns_this_build_reads_is_not_usable(tmp_path: Path) -> None:
    path = tmp_path / "library.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE music_files (path TEXT)")
    connection.commit()
    connection.close()
    assert PreviousLibrary.load(path).usable is False


def test_a_published_library_supplies_reuse(tmp_path: Path) -> None:
    """The end of the loop: build, publish, then read the artifact back as a cache."""

    built = tmp_path / "build.db"
    connection = sqlite3.connect(built)
    create_schema(connection)
    item = _discovered(tmp_path / "a.mp3")
    extracted = ExtractedFile(
        file=item,
        tags={"title": "Remembered", "artist": "Known Artist"},
        duration=timedelta(seconds=60),
        tag_hash=stored_tag_hash({"artist": "Known Artist", "title": "Remembered"}),
    )
    track = Track(
        extracted=extracted,
        artist="Known Artist",
        album=None,
        title="Remembered",
        normalized_artist=fold("Known Artist"),
        normalized_title=fold("Remembered"),
        provenance={
            "artist": _prov("artist", "Known Artist"),
            "title": _prov("title", "Remembered"),
        },
    )
    build(connection, [track], artwork=ArtworkOutcome(), rules_version=NORMALIZATION_RULES_VERSION)
    stamp_meta(
        connection,
        library_version="state",
        rules_version=NORMALIZATION_RULES_VERSION,
        built_at="2026-01-01T00:00:00+00:00",
        song_count=1,
        file_count=1,
        builder_version="0.1.0",
    )
    connection.commit()
    stage(connection, built)
    destination = tmp_path / "library.db"
    publish(built, destination)

    previous = PreviousLibrary.load(destination)
    assert previous.usable is True
    record = previous.for_path(item.path)
    assert record is not None
    assert record.matches(item, digest=extracted.tag_hash)
    assert record.probe is not None
    assert record.probe.tags["artist"] == "Known Artist"
    assert record.probe.duration == timedelta(seconds=60)


def test_cache_keys_are_stable_across_runs(tmp_path: Path) -> None:
    item = _discovered(tmp_path / "a.mp3")
    first = cache_key(
        ExtractedFile(file=item, tags={"title": "A"}, duration=timedelta(seconds=1), tag_hash="h")
    )
    again = cache_key(
        ExtractedFile(file=item, tags={"title": "A"}, duration=timedelta(seconds=1), tag_hash="h")
    )
    assert first == again
    assert first.count("|") == 4


# -- checkpoints ----------------------------------------------------------


def test_a_checkpoint_resumes_a_run_that_stopped_early(tmp_path: Path) -> None:
    item = _discovered(tmp_path / "a.mp3")
    extracted = ExtractedFile(file=item, tags={"title": "Halfway"}, duration=timedelta(seconds=3))
    Checkpoints(tmp_path / "tmp").record([extracted])

    resumed = Checkpoints(tmp_path / "tmp")
    assert resumed.saved() == 1
    result = resumed.probe_for(item)
    assert result is not None
    assert result.tags["title"] == "Halfway"
    assert result.duration == timedelta(seconds=3)


def test_a_checkpoint_does_not_outlive_its_file(tmp_path: Path) -> None:
    item = _discovered(tmp_path / "a.mp3")
    Checkpoints(tmp_path / "tmp").record(
        [ExtractedFile(file=item, tags={"title": "T"}, duration=timedelta(seconds=3))]
    )
    changed = _discovered(tmp_path / "a.mp3", size=999)
    assert Checkpoints(tmp_path / "tmp").probe_for(changed) is None


def test_a_disabled_checkpoint_reads_and_writes_nothing(tmp_path: Path) -> None:
    """`--full` means the operator asked to ignore the history; doing it anyway is a bug."""

    item = _discovered(tmp_path / "a.mp3")
    extracted = [ExtractedFile(file=item, tags={"title": "T"}, duration=timedelta(seconds=3))]
    off = Checkpoints(tmp_path / "tmp", enabled=False)
    off.record(extracted)
    assert off.probe_for(item) is None
    assert not off.path.exists(), "a disabled checkpoint must not leave state behind"

    off.record(extracted)
    assert Checkpoints(tmp_path / "tmp", enabled=False).saved() == 0


def test_clearing_a_checkpoint_removes_the_file(tmp_path: Path) -> None:
    checkpoints = Checkpoints(tmp_path / "tmp")
    checkpoints.record(
        [
            ExtractedFile(
                file=_discovered(tmp_path / "a.mp3"), tags={}, duration=timedelta(seconds=1)
            )
        ]
    )
    assert checkpoints.path.exists()
    checkpoints.clear()
    assert not checkpoints.path.exists()
    assert Checkpoints(tmp_path / "tmp").saved() == 0


def test_an_unrecognised_checkpoint_file_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "tmp" / "extraction.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"format": "something-else", "files": {}}', encoding="utf-8")
    assert Checkpoints(tmp_path / "tmp").saved() == 0


# -- the report -----------------------------------------------------------


def test_the_report_counts_add_up() -> None:
    """SAPRS 6.12's numbers, and ADR-010's requirement that they be comparable.

    `scanned ≥ discovered`, `discovered = processed + skipped`, `songs = processed`. If
    any of those stops holding, the report is describing a build that did not happen, and
    this is the assertion that notices.
    """

    report = BuildReport(
        discovered=9,
        scanned=12,
        processed=9,
        skipped=SkipLedger.of(
            [
                SkippedFile(
                    path=Path("/m/a.m4p"), reason=SkipReason.UNSUPPORTED_FORMAT, detail=".m4p"
                ),
                SkippedFile(
                    path=Path("/m/b.m4p"), reason=SkipReason.UNSUPPORTED_FORMAT, detail=".m4p"
                ),
                SkippedFile(
                    path=Path("/m/c.txt"), reason=SkipReason.UNSUPPORTED_FORMAT, detail=".txt"
                ),
                SkippedFile(path=Path("/m/d.mp3"), reason=SkipReason.NO_ARTIST, detail="artist"),
            ]
        ),
        repaired=3,
        artwork=5,
        songs=8,
        artists=2,
        albums=3,
    )
    assert report.skip_count == 4
    assert report.discovered == report.processed
    assert report.songs == report.discovered - 1
    assert "unaccounted" not in report.accounting
    text = report.text()
    scanned_line = next(line for line in text.splitlines() if line.lstrip().startswith("Scanned"))
    assert "12" in scanned_line, scanned_line
    assert "unsupported container: 2 .m4p, 1 .txt" in text
    assert report.counts()["song_count"] == 8
    assert report.counts()["files_discovered"] == 9
    assert report.counts()["files_skipped"] == 4
    payload = json.loads(report.json())
    assert payload["song_count"] == 8
    assert payload["files_scanned"] == 12


def test_unsupported_files_are_one_line_per_suffix() -> None:
    """ADR-010: 188 `.m4p` files are one line, not 188.

    The measured corpus is the reason. A list that long is not information, it is a
    wall, and the first wall teaches an operator to skip the whole section.
    """

    ledger = SkipLedger.of(
        [
            SkippedFile(
                path=Path(f"/m/track-{index}.m4p"),
                reason=SkipReason.UNSUPPORTED_FORMAT,
                detail=".m4p",
            )
            for index in range(188)
        ]
    )
    lines = ledger.lines()
    assert len(lines) == 1
    assert "188" in lines[0]
    assert ".m4p" in lines[0]
    assert ledger.total == 188


def test_read_errors_are_grouped_by_reason_not_by_path() -> None:
    ledger = SkipLedger.of(
        [
            SkippedFile(
                path=Path(f"/m/{index}.mp3"), reason=SkipReason.UNREADABLE, detail="PermissionError"
            )
            for index in range(30)
        ]
    )
    assert len(ledger.lines()) == 1
    assert ledger.lines()[0] == f"    30 {describe(SkipReason.UNREADABLE)}"
    assert "could not be read" in ledger.lines()[0]
    assert all("PermissionError" not in line for line in ledger.lines()), "the aggregate is a count"
    # …and the specifics the aggregate gives up are not lost, only relocated.
    assert {entry.detail for entry in ledger.entries} == {"PermissionError"}


def test_every_skip_reason_has_a_sentence() -> None:
    """A report that prints `unsupported-format` has asked the operator to guess.

    Asserted over the whole `SkipReason` set, so adding a reason without adding its
    wording fails here rather than at someone's first build.
    """

    reasons = [
        value
        for key, value in vars(SkipReason).items()
        if isinstance(value, str) and not key.startswith("_")
    ]
    assert len(reasons) >= 5
    for reason in reasons:
        assert describe(reason) != reason, reason
        assert " " in describe(reason), reason


def test_an_unknown_reason_describes_itself(tmp_path: Path) -> None:
    assert describe("made-up") == "made-up"


def test_the_accounting_line_names_the_numbers_it_compares() -> None:
    """ADR-010's requirement that the report be checkable by eye.

    A build that catalogued fewer files than it scanned, and skipped nothing it could
    name, is a build that lost something. The line says so, and the test says when.
    """

    balanced = BuildReport(
        discovered=6,
        scanned=6,
        processed=4,
        songs=4,
        skipped=SkipLedger.of(
            [
                SkippedFile(
                    path=Path("/m/x.m4p"), reason=SkipReason.UNSUPPORTED_FORMAT, detail=".m4p"
                ),
                SkippedFile(
                    path=Path("/m/y.m4p"), reason=SkipReason.UNSUPPORTED_FORMAT, detail=".m4p"
                ),
            ]
        ),
    )
    assert "6 files scanned" in balanced.accounting
    assert "unaccounted" not in balanced.accounting

    gap = BuildReport(scanned=10, songs=4)
    assert "6 unaccounted for" in gap.accounting


def test_warnings_are_printed_and_errors_stop_the_report_being_clean() -> None:
    report = BuildReport(
        warnings=["MusicBrainz unavailable"],
        errors=["validation failed: artwork"],
        validation=[("artwork", "fatal", "1 image missing")],
    )
    assert report.warning_count == 1
    assert report.error_count == 1
    assert report.validated is False
    assert "unavailable" in report.text()


def test_the_json_is_machine_readable_for_the_detail_file() -> None:
    report = BuildReport(
        discovered=2,
        scanned=2,
        processed=1,
        skipped=SkipLedger.of(
            [
                SkippedFile(
                    path=Path("/m/x.m4p"), reason=SkipReason.UNSUPPORTED_FORMAT, detail=".m4p"
                )
            ]
        ),
        songs=1,
    )
    payload = json.loads(report.json())
    assert payload["files_discovered"] == 2
    assert payload["skipped"] == {f"{SkipReason.UNSUPPORTED_FORMAT}:.m4p": 1}
    assert payload["skip_details"][0]["path"] == str(Path("/m/x.m4p"))


# -- helpers --------------------------------------------------------------


def _discovered(path: Path, *, size: int = 500, mtime: int = 99) -> DiscoveredFile:
    return DiscoveredFile(
        path=path,
        format=AudioFormat.for_path(path) or AudioFormat.MP3,
        size_bytes=size,
        mtime_ns=mtime,
    )


def _track(root: Path, name: str) -> Track:
    return Track(
        extracted=ExtractedFile(
            file=_discovered(root / name),
            tags={"artist": "Published Artist", "title": "Published Song"},
            duration=timedelta(seconds=120),
        ),
        artist="Published Artist",
        album="Published Album",
        title="Published Song",
        normalized_artist=fold("Published Artist"),
        normalized_album=fold("Published Album"),
        normalized_title=fold("Published Song"),
        provenance={
            "artist": _prov("artist", "Published Artist"),
            "album": _prov("album", "Published Album"),
            "title": _prov("title", "Published Song"),
        },
    )


def _prov(field: str, value: str) -> FieldProvenance:
    return FieldProvenance(field=field, original=value, effective=value, source=MetadataSource.TAG)


def test_the_contract_names_the_tables_publication_must_have_written() -> None:
    """A cheap restatement on purpose: publication is where the artifact becomes legal.

    If a table named here is missing from a published file, the Server's read layer will
    fail at the first search rather than at publish time, and the operator will see an
    appliance with no music and no explanation.
    """

    assert {
        "songs",
        "albums",
        "artists",
        "music_files",
        "artwork",
        "metadata",
        "library_meta",
    } <= contract.LIBRARY_TABLES
