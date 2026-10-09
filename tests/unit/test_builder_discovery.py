"""Discovery and extraction: stages 1 and 2 (SAPRS 6.3, 6.4, ADR-010).

These two stages are where a wrong decision is cheapest to make and most expensive to
notice, because everything downstream trusts them. The assertions are therefore mostly
about *what was left out*: which files were never opened, and what the report says about
them.

Extraction has one rule that outlives every other one here — SAPRS 6.4's "missing
metadata shall not prevent a file from being indexed" — and it is the reason no test in
this file expects an exception from a file with no tags.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest

from apps.builder.discovery import discover
from apps.builder.extraction import (
    MutagenProbe,
    ProbeResult,
    extract,
    tag_hash,
)
from apps.builder.records import DiscoveredFile, ExtractedFile
from apps.builder.state import Checkpoints, PreviousLibrary, cache_key
from encore.domain import AudioFormat
from tests.conftest import BuiltLibrary
from tests.support.media import (
    AlbumSpec,
    MediaSpec,
    SyntheticMediaUnavailableError,
    cover_image,
    generate_albums,
    generate_track,
)

UNTAGGED_TOLERANCE = timedelta(milliseconds=80)


def _file(path: Path) -> DiscoveredFile:
    stat = path.stat()
    return DiscoveredFile(
        path=path,
        format=AudioFormat.for_path(path) or AudioFormat.MP3,
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


# -- discovery ------------------------------------------------------------


def test_discovery_finds_every_supported_file(tmp_path: Path) -> None:
    paths = generate_albums(
        [
            AlbumSpec.with_tracks("Artist", "Album", count=4),
            AlbumSpec.with_tracks("Other", "Disc", count=2, fmt="flac"),
        ],
        tmp_path,
    )
    found = discover(tmp_path)
    assert found.scanned == len(paths)
    assert found.supported_count == len(paths)
    assert {item.path for item in found.files} == {path.resolve() for path in paths}


def test_discovery_reports_a_missing_root_as_empty(tmp_path: Path) -> None:
    """The pipeline turns this into a warning with the path in it (SAPRS 6.8, 12.5)."""

    found = discover(tmp_path / "nowhere")
    assert found.scanned == 0
    assert found.files == ()


def test_unsupported_containers_are_aggregated_by_suffix(tmp_path: Path) -> None:
    """ADR-010: 188 `.m4p` files are one line, not 188.

    Measured on the corpus, where 231 lines of warnings trained nobody to read the
    report.
    """

    for index in range(3):
        (tmp_path / f"track-{index}.m4p").write_bytes(b"not really a playlist")
    (tmp_path / "song.wma").write_bytes(b"asf")
    found = discover(tmp_path)
    assert found.scanned == 4
    assert found.supported_count == 0
    assert found.unsupported == {".m4p": 3, ".wma": 1}
    assert found.unsupported_count == 4


def test_discovery_ignores_the_files_a_library_scan_should_ignore(tmp_path: Path) -> None:
    for name in (".DS_Store", "Thumbs.db", "cover.jpg", "notes.txt", "album.m3u8"):
        (tmp_path / name).write_bytes(b"x")
    found = discover(tmp_path)
    assert found.supported_count == 0
    assert ".jpg" not in found.unsupported
    assert ".txt" not in found.unsupported


def test_hidden_directories_are_skipped_wholesale(tmp_path: Path) -> None:
    """`.git` and `.Trash` are not music, and walking them costs a party's build time."""

    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "objects" / "track.mp3").write_bytes(b"x")
    generate_track(MediaSpec(title="Real", artist="A", album="B"), tmp_path)
    found = discover(tmp_path)
    assert found.supported_count == 1
    assert found.scanned == 1


def test_an_unreadable_entry_is_recorded_not_raised(tmp_path: Path) -> None:
    """ADR-010: an unreadable file is an outcome, reported with a reason."""

    blocked = tmp_path / "locked"
    blocked.mkdir()
    (blocked / "song.mp3").write_bytes(b"x")
    blocked.chmod(0o000)
    try:
        found = discover(tmp_path)
    finally:
        blocked.chmod(0o755)
    assert found.scanned == 0
    assert found.unreadable, "an unreadable directory must appear in the report"


def test_paths_are_recorded_absolute(tmp_path: Path) -> None:
    """SAPRS 7.7: nothing resolves a path at runtime, so the Builder records it absolute."""

    generate_track(MediaSpec(title="Plain", artist="A", album="B"), tmp_path / "nested")
    found = discover(tmp_path)
    assert found.supported_count == 1
    assert all(item.path.is_absolute() for item in found.files)


def _make(path: Path) -> Path:
    """A real file to point a symlink at, in the directory the caller named."""

    return (
        path
        if path.exists()
        else generate_track(MediaSpec(title="Link", artist="A", album="B"), path.parent)
    )


# -- extraction -----------------------------------------------------------


def test_probe_reads_the_tags_a_file_actually_carries(tmp_path: Path) -> None:
    path = generate_track(
        MediaSpec(
            title="Alpha",
            artist="The Test Artists",
            album="Beta",
            track_number=3,
            disc_number=2,
            genre="Rock",
            date="1999",
        ),
        tmp_path,
    )
    result = MutagenProbe().probe(_file(path))
    assert result.error is None
    tags = result.tags
    assert tags["title"] == "Alpha"
    assert tags["artist"] == "The Test Artists"
    assert tags["album"] == "Beta"
    assert tags["track"] == "3"
    assert tags["disc"] == "2"
    assert tags["genre"] == "Rock"
    assert tags["date"] == "1999"


@pytest.mark.parametrize("fmt", ["mp3", "flac"])
def test_probe_reports_the_container_duration(tmp_path: Path, fmt: str) -> None:
    path = generate_track(
        MediaSpec(title="Timed", artist="A", album="B", duration_seconds=2.0, fmt=fmt), tmp_path
    )
    result = MutagenProbe().probe(_file(path))
    assert abs(result.duration - timedelta(seconds=2.0)) <= UNTAGGED_TOLERANCE


def test_a_corrupt_file_is_an_error_and_not_an_exception(tmp_path: Path) -> None:
    path = tmp_path / "broken.mp3"
    path.write_bytes(b"this is not audio at all, not even close")
    result = MutagenProbe().probe(_file(path))
    assert result.error
    assert result.duration == timedelta(0)


def test_an_untagged_file_still_yields_a_duration(tmp_path: Path) -> None:
    """SAPRS 6.4: missing metadata never prevents indexing.

    A container with no tag block at all, which is what a file straight off an old
    encoder looks like and what `generate_track` cannot produce — it always tags.
    """

    from tests.support.media import _write_mp3

    path = tmp_path / "bare.mp3"
    _write_mp3(path, 1.0)
    result = MutagenProbe().probe(_file(path))
    assert result.tags == {}
    assert result.error is None
    assert result.duration > timedelta(0)


def test_embedded_artwork_is_extracted_with_its_mime(tmp_path: Path) -> None:
    payload = cover_image()
    for fmt in ("mp3", "flac"):
        path = generate_track(
            MediaSpec(title="Covered", artist="A", album="B", fmt=fmt, artwork=payload),
            tmp_path / fmt,
        )
        result = MutagenProbe().probe(_file(path))
        assert result.artwork_bytes == payload, fmt
        assert result.artwork_mime == "image/jpeg"


def test_tag_hash_is_stable_and_order_independent() -> None:
    one = {"title": "A", "artist": "B", "album": "C"}
    other = {"album": "C", "artist": "B", "title": "A"}
    assert tag_hash(one) == tag_hash(other)
    assert tag_hash({**one, "title": "different"}) != tag_hash(one)
    assert tag_hash({}) != tag_hash(one)


def test_extract_reads_every_file_once(tmp_path: Path) -> None:
    paths = generate_albums([AlbumSpec.with_tracks("A", "B", count=5)], tmp_path)
    files = [_file(path) for path in paths]
    probe = _CountingProbe()
    results = extract(files, probe)
    assert len(results) == 5
    assert probe.calls == 5
    assert all(item.usable for item in results)


def test_a_reuse_hit_skips_the_read(tmp_path: Path) -> None:
    """SAPRS 6.9's incremental build, at the stage where the cost actually is."""

    path = generate_track(MediaSpec(title="Kept", artist="A", album="B"), tmp_path)
    item = _file(path)
    probe = _CountingProbe()
    earlier = MutagenProbe().probe(item)
    extract([item], probe, previous={item.cache_key: earlier})
    assert probe.calls == 0, "a cached probe result still opened the file"


def test_a_changed_mtime_invalidates_the_cache(tmp_path: Path) -> None:
    """The whole point of the key: a touched file is read again.

    The cached entry is keyed by the file *as it was*, so the caller must look it up
    with the file as it is now — which is why `extract` takes a mapping of cache keys
    rather than a mapping of paths.
    """

    path = generate_track(MediaSpec(title="Touched", artist="A", album="B"), tmp_path)
    before = _file(path)
    earlier = MutagenProbe().probe(before)
    os.utime(path, ns=(before.mtime_ns, before.mtime_ns + 5_000_000))

    after = _file(path)
    probe = _CountingProbe()
    assert after.cache_key != before.cache_key
    extract([after], probe, previous={before.cache_key: earlier})
    assert probe.calls == 1

    unchanged = _file(path)
    hits = _CountingProbe()
    extract([unchanged], hits, previous={unchanged.cache_key: earlier})
    assert hits.calls == 0


class _CountingProbe:
    """A probe that counts, because "did we read the file" is the whole question."""

    def __init__(self) -> None:
        self.calls = 0

    def probe(self, file: DiscoveredFile) -> ProbeResult:
        self.calls += 1
        return MutagenProbe().probe(file)


# -- incremental state ----------------------------------------------------


def test_the_cache_key_names_the_file_not_its_contents(tmp_path: Path) -> None:
    """Path, size and mtime: the three facts available before opening the file.

    Which is the whole design of stage 2's cache: a digest of the tags is only
    computable after the read the caching exists to avoid, so it belongs to the
    `Reusable` comparison and not to this key.
    """

    path = generate_track(MediaSpec(title="Keyed", artist="A", album="B"), tmp_path)
    item = _file(path)
    assert (
        cache_key(ExtractedFile(file=item, tags={"title": "Keyed"}, duration=timedelta(seconds=1)))
        != item.cache_key
    )
    assert item.cache_key == _file(path).cache_key


def test_a_missing_previous_library_is_a_full_build(tmp_path: Path) -> None:
    previous = PreviousLibrary.load(tmp_path / "nope.db")
    assert previous.usable is False
    assert previous.for_path(tmp_path / "anything.mp3") is None


def test_a_previous_library_supplies_probe_results(
    built_library: BuiltLibrary, tmp_path: Path
) -> None:
    """The reuse path: the published store's own tags answer stage 2 (SAPRS 6.9)."""

    previous = PreviousLibrary.load(built_library.options.library_db)
    assert previous.usable is True
    first = built_library.options.music_dir
    sample = next(first.rglob("*.mp3"))
    record = previous.for_path(sample.resolve())
    assert record is not None
    assert record.probe is not None
    assert record.probe.tags, "a reused row carried no tags"


def test_checkpoints_round_trip(tmp_path: Path) -> None:
    path = generate_track(MediaSpec(title="Resumed", artist="A", album="B"), tmp_path)
    item = _file(path)
    checkpoints = Checkpoints(tmp_path / "scratch", enabled=True)
    checkpoints.record(
        [ExtractedFile(file=item, tags={"title": "Resumed"}, duration=timedelta(seconds=1))]
    )

    reopened = Checkpoints(tmp_path / "scratch", enabled=True)
    assert reopened.probe_for(item) is not None


def test_a_checkpoint_stale_by_mtime_is_ignored(tmp_path: Path) -> None:
    path = generate_track(MediaSpec(title="Stale", artist="A", album="B"), tmp_path)
    checkpoints = Checkpoints(tmp_path / "scratch", enabled=True)
    checkpoints.record(
        [ExtractedFile(file=_file(path), tags={"title": "Stale"}, duration=timedelta(seconds=1))]
    )

    moved = path.stat()
    os.utime(path, ns=(moved.st_atime_ns, moved.st_mtime_ns + 5_000_000))
    assert Checkpoints(tmp_path / "scratch", enabled=True).probe_for(_file(path)) is None


def test_a_disabled_checkpoint_reads_nothing(tmp_path: Path) -> None:
    """`--full` means the opposite of resuming, so resuming would be a bug."""

    path = generate_track(MediaSpec(title="Full", artist="A", album="B"), tmp_path)
    enabled = Checkpoints(tmp_path / "scratch", enabled=True)
    enabled.record([ExtractedFile(file=_file(path), tags={}, duration=timedelta(seconds=1))])
    assert Checkpoints(tmp_path / "scratch", enabled=False).probe_for(_file(path)) is None


def test_clearing_checkpoints_removes_the_file(tmp_path: Path) -> None:
    checkpoints = Checkpoints(tmp_path / "scratch", enabled=True)
    checkpoints.record([])
    checkpoints.clear()
    assert not checkpoints.path.exists()


def test_the_unsynthesisable_container_is_still_discoverable(tmp_path: Path) -> None:
    """A guard for the guard: if `generate_track` starts writing m4a, the aggregate test above lies."""

    with pytest.raises(SyntheticMediaUnavailableError):
        generate_track(MediaSpec(title="DRM", artist="A", album="B", fmt="m4a"), tmp_path)


def test_discovery_of_a_built_tree_agrees_with_the_report(built_library: BuiltLibrary) -> None:
    """Stage 1, run twice: once by the pipeline and once here, must say the same thing."""

    found = discover(built_library.options.music_dir)
    assert found.scanned == built_library.report.scanned
    assert found.supported_count == built_library.report.discovered
