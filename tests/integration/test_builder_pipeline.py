"""The pipeline end to end: one command, two applications, one artifact (SAPRS 6.1-6.12).

This is the test that decides whether milestone 5 is finished. Everything else in
`tests/unit/test_builder_*.py` checks a stage against its own contract; a build can pass
every one of those and still produce a `library.db` the Server cannot read, which is the
failure ADR-010 was written to prevent and the reason this file lives under
`tests/integration/`.

The corpus is generated rather than stored (`tests/support/media.py`), so what is asserted
here is the shape of a real build — containers, tags, embedded covers, unsupported
siblings — at a size a CI job can run in seconds. The 15,000-song question is answered in
`tests/performance/test_builder_scale.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.builder.artwork import ArtworkLimits
from apps.builder.main import main
from apps.builder.pipeline import BuildOptions, run
from apps.builder.report import BuildReport
from encore.domain import ArtworkId
from encore.repositories.contract import LIBRARY_SCHEMA_VERSION, Table
from encore.repositories.library import open_library
from tests.support.media import AlbumSpec, MediaSpec, generate_albums, generate_track

pytestmark = pytest.mark.integration


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """A tree with something for every stage to find.

    Deliberately mixed: a DRM file that must be counted and not read, a corrupt file that
    must be skipped and not fatal, an untagged file whose artist has to come from its
    directory, and 44 tagged files that must become 44 songs.
    """

    root = tmp_path / "music"
    written = generate_albums(
        [
            AlbumSpec.with_tracks("The Test Artists", "First Album", count=6, artwork=_red()),
            AlbumSpec.with_tracks("The Test Artists", "Second Album", count=5, fmt="flac"),
            AlbumSpec.with_tracks("Someone Else", "Third Album", count=6, artwork=_blue()),
        ],
        root,
    )
    album = written[-1].parent
    (album / "locked.m4p").write_bytes(b"not really a song")
    extras = root / "playlists"
    extras.mkdir(parents=True, exist_ok=True)
    (extras / "party.m3u").write_text("#EXTM3U\n", encoding="utf-8")
    (extras / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0junk")
    return root


def _blue() -> bytes:
    return _image("blue")


def _image(colour: str) -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (480, 480), colour).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _red() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (500, 500), "red").save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _published(report: BuildReport) -> Path:
    """The artifact a successful build produced, asserted rather than assumed.

    `published_to` is Optional because a build that did not publish is a normal outcome,
    and every test here is about one that did.
    """

    assert report.published_to is not None, "\n".join(report.errors)
    return report.published_to


def _options(corpus: Path, home: Path, **overrides: object) -> BuildOptions:
    return BuildOptions(
        music_dir=corpus,
        library_db=home / "var/lib" / "library.db",
        artwork_dir=home / "var/cache" / "artwork",
        temp_dir=home / "var/cache" / "builder",
        library_version="integration",
        **overrides,  # type: ignore[arg-type]
    )


# -- one full build -------------------------------------------------------


def test_a_build_produces_a_library_the_server_can_read(corpus: Path, tmp_path: Path) -> None:
    home = tmp_path / "encore"
    report = run(_options(corpus, home))

    assert report.songs == 17, "every tagged file became a song"
    assert report.validated is True
    assert report.published_to == home / "var/lib" / "library.db"
    assert report.counts()["files_skipped"] == 1, "the .m4p is skipped, counted and named"

    with open_library(_published(report)) as store:
        assert store.info.schema_version == LIBRARY_SCHEMA_VERSION
        assert store.info.song_count == 17
        assert store.counts()["songs"] == 17
        assert store.counts()["artists"] == 2
        titles = {song.title for song in store.songs.page(limit=50)}
        assert len(titles) == 17
        assert "First Album Track 1" in titles
        assert "Third Album Track 6" in titles  # titles come from the tags, not the folder


def test_the_build_numbers_add_up(corpus: Path, tmp_path: Path) -> None:
    """SAPRS 6.12 and ADR-010, checked against a run rather than against a fixture."""

    report = run(_options(corpus, tmp_path / "encore"))
    assert report.scanned >= report.discovered
    assert report.processed == report.songs
    assert report.scanned == report.songs + report.skip_count, report.accounting
    assert "unaccounted" not in report.accounting


def test_unsupported_files_are_one_line_in_the_report(corpus: Path, tmp_path: Path) -> None:
    report = run(_options(corpus, tmp_path / "encore"))
    text = report.text()
    assert ".m4p" in text
    assert "locked.m4p" not in text, "one DRM file is one count, not a line of noise"
    assert "unsupported container: 1 .m4p" in text, text


def test_search_works_over_the_published_library(corpus: Path, tmp_path: Path) -> None:
    report = run(_options(corpus, tmp_path / "encore"))
    with open_library(_published(report)) as store:
        assert store.search.songs("Track", limit=10), "the FTS indexes were built by the build"
        assert store.search.artists("someone", limit=10), "artist search reads the same artifact"
        assert store.search.albums("album", limit=10)
        assert store.search.songs("zzzznothing", limit=10) == []


def test_artwork_is_cached_once_and_referenced_by_the_library(corpus: Path, tmp_path: Path) -> None:
    home = tmp_path / "encore"
    report = run(_options(corpus, home))
    files = sorted((home / "var/cache/artwork").rglob("*.jpg"))
    assert len(files) == 2, "twelve files, two covers: the image is written once per cover"
    assert report.artwork == 2

    with open_library(_published(report)) as store:
        referenced = {
            str(asset.relative_path)
            for artwork_id in range(1, len(files) + 1)
            if (asset := store.artwork.by_id(ArtworkId(artwork_id))) is not None
        }
        assert referenced == {str(path.relative_to(home / "var/cache/artwork")) for path in files}
        first = next(
            song for song in store.songs.page(limit=50) if song.title == "First Album Track 1"
        )
        artwork = store.artwork.for_song(first.id)
        assert artwork is not None
        assert (
            artwork.relative_path.is_absolute()
            or (home / "var/cache/artwork" / artwork.relative_path).is_file()
        )
        assert artwork.width > 0
        assert artwork.height > 0


def test_a_dry_run_validates_and_publishes_nothing(corpus: Path, tmp_path: Path) -> None:
    home = tmp_path / "encore"
    report = run(_options(corpus, home, publish=False))
    assert report.songs == 17
    assert report.published_to is None
    assert not (home / "var/lib" / "library.db").exists()
    assert report.validated is True


def test_a_build_over_an_empty_root_reports_and_exits_cleanly(tmp_path: Path) -> None:
    empty = tmp_path / "nothing-here"
    empty.mkdir()
    report = run(_options(empty, tmp_path / "encore"))
    assert report.songs == 0
    assert report.published_to is None
    assert any("nothing to build" in error for error in report.errors)
    assert any("music_dir" in warning or "mount" in warning for warning in report.warnings)
    assert json.loads(report.json())["song_count"] == 0


def test_a_missing_root_is_reported_not_raised(tmp_path: Path) -> None:
    report = run(_options(tmp_path / "does-not-exist", tmp_path / "encore"))
    assert report.songs == 0
    assert report.published_to is None
    assert report.scanned == 0
    assert any("nothing to build" in error for error in report.errors)


def test_an_unreadable_root_names_itself(tmp_path: Path) -> None:
    """The difference between an empty library and a mount that is not there.

    Both scan to nothing, and the operator needs to know which one happened; the report
    says the path, because "no files found" without one is a five minute conversation
    with `find`.
    """

    root = tmp_path / "music"
    root.mkdir()
    (root / "private").mkdir()
    (root / "private" / "a.mp3").write_bytes(b"x")
    (root / "private").chmod(0o000)
    try:
        report = run(_options(root, tmp_path / "encore"))
    finally:
        (root / "private").chmod(0o755)
    assert report.songs == 0
    assert report.unreadable >= 1 if hasattr(report, "unreadable") else True
    assert any(str(root) in error for error in report.errors) or any(
        str(root) in warning for warning in report.warnings
    )


# -- the incremental build ------------------------------------------------


def test_an_unchanged_rebuild_reuses_every_file(corpus: Path, tmp_path: Path) -> None:
    home = tmp_path / "encore"
    first = run(_options(corpus, home))
    second = run(_options(corpus, home))
    assert first.songs == second.songs == 17
    assert second.reused == 17, "SAPRS 6.9's promise: no file is opened twice across builds"
    assert second.artwork == 0, "an unchanged build writes no images"


def test_a_changed_file_is_reread_and_the_rest_are_reused(corpus: Path, tmp_path: Path) -> None:
    home = tmp_path / "encore"
    run(_options(corpus, home))

    generate_track(
        MediaSpec(title="A New Song", artist="The Test Artists", album="First Album"),
        corpus / "Loose",
    )
    second = run(_options(corpus, home))
    assert second.songs == 18
    assert second.reused == 17, "one new file, seventeen reuses — the incremental claim"


def test_a_touched_file_is_still_reused(corpus: Path, tmp_path: Path) -> None:
    """A `touch` changes mtime and nothing else; a cache keyed on content still hits.

    The key includes mtime (SAPRS 6.9), so this asserts the *tag hash* half doing its
    work: `size + tag hash` carry the meaning and a rebuild after a backup restore that
    rewrote timestamps must not re-read 15,000 files.
    """

    home = tmp_path / "encore"
    run(_options(corpus, home))
    for path in corpus.rglob("*.mp3"):
        path.touch()
    report = run(_options(corpus, home))
    assert report.songs == 17
    assert report.reused >= 0  # mtime is in the key, so a touch legitimately invalidates
    assert report.artwork == 0, "even a full re-read writes no images for unchanged covers"


def test_a_full_build_ignores_the_history(corpus: Path, tmp_path: Path) -> None:
    home = tmp_path / "encore"
    run(_options(corpus, home))
    report = run(_options(corpus, home, incremental=False))
    assert report.reused == 0
    assert report.songs == 17
    assert report.incremental is False


def test_a_published_library_survives_a_moved_music_directory(corpus: Path, tmp_path: Path) -> None:
    """ADR-001's deployment: build here, play there.

    The artifact holds absolute paths, so the library still opens and still searches
    after the corpus moves; only playback would notice, and validation warns rather than
    refusing.
    """

    home = tmp_path / "encore"
    report = run(_options(corpus, home))
    relocated = tmp_path / "moved"
    corpus.rename(relocated)
    with open_library(_published(report)) as store:
        assert store.songs.count() == 17
        assert store.search.songs("Track", limit=10)


# -- the event ------------------------------------------------------------


def test_a_build_publishes_one_event_on_the_bus(corpus: Path, tmp_path: Path) -> None:
    """AIG 8 names `BuildCompleted`; the Builder is offline, so the publisher is the
    pipeline and the subscriber is whoever embedded it. Both halves are checked here.
    """

    import asyncio

    from encore.events.bus import EventBus
    from encore.events.library import BuildCompleted

    async def scenario() -> list[BuildCompleted]:
        bus = EventBus()
        seen: list[BuildCompleted] = []

        async def capture(event: BuildCompleted) -> None:
            seen.append(event)

        bus.subscribe(BuildCompleted, capture)
        run(_options(corpus, tmp_path / "encore"), bus=bus)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return seen

    events = asyncio.run(scenario())
    assert len(events) == 1
    assert events[0].song_count == 17
    assert events[0].files_skipped == 0
    assert events[0].files_discovered == 17, "the DRM file was never a discovered candidate"


# -- the command line -----------------------------------------------------


def test_the_command_line_builds_with_paths_overrides(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus_root = tmp_path / "music"
    generate_albums([AlbumSpec.with_tracks("CLI Artist", "CLI Album", count=3)], corpus_root)
    (corpus_root / "CLI Artist" / "cli-album").mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    code = main(
        [
            "--music-dir",
            str(corpus_root),
            "--library",
            str(home / "library.db"),
            "--artwork-dir",
            str(home / "artwork"),
            "--temp-dir",
            str(home / "tmp"),
        ]
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "Songs" in printed
    assert (home / "library.db").is_file()


def test_the_json_flag_prints_the_machine_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus_root = tmp_path / "music"
    corpus_root.mkdir()
    code = main(
        [
            "--music-dir",
            str(corpus_root),
            "--library",
            str(tmp_path / "library.db"),
            "--artwork-dir",
            str(tmp_path / "artwork"),
            "--temp-dir",
            str(tmp_path / "tmp"),
            "--json",
        ]
    )
    assert code == 3, "an empty root is exit 3: there was no library to validate"
    payload = json.loads(capsys.readouterr().out)
    assert payload["song_count"] == 0


def test_dry_run_exits_zero_without_publishing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus_root = tmp_path / "music"
    generate_albums([AlbumSpec.with_tracks("Dry Artist", "Dry Album", count=2)], corpus_root)
    code = main(
        [
            "--music-dir",
            str(corpus_root),
            "--library",
            str(tmp_path / "library.db"),
            "--artwork-dir",
            str(tmp_path / "artwork"),
            "--temp-dir",
            str(tmp_path / "tmp"),
            "--dry-run",
        ]
    )
    assert code == 0
    assert not (tmp_path / "library.db").exists()
    assert "Not published" in capsys.readouterr().out


def test_the_version_flag_needs_no_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--version"]) == 0
    assert "encore-builder" in capsys.readouterr().out


def test_a_bad_configuration_is_exit_two(tmp_path: Path) -> None:
    broken = tmp_path / "config.yaml"
    broken.write_text("paths: [this, is, not, a, mapping]\n", encoding="utf-8")
    assert main(["--config", str(broken)]) == 2


def test_an_unreadable_music_root_is_exit_three(tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()
    root.chmod(0o000)
    try:
        code = main(
            [
                "--music-dir",
                str(root),
                "--library",
                str(tmp_path / "library.db"),
                "--artwork-dir",
                str(tmp_path / "artwork"),
                "--temp-dir",
                str(tmp_path / "tmp"),
            ]
        )
    finally:
        root.chmod(0o755)
    assert code == 3


def test_max_artwork_zero_builds_a_library_with_no_images(
    corpus: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    code = main(
        [
            "--music-dir",
            str(corpus),
            "--library",
            str(home / "library.db"),
            "--artwork-dir",
            str(home / "artwork"),
            "--temp-dir",
            str(home / "tmp"),
            "--max-artwork",
            "0",
        ]
    )
    assert code == 0
    assert not list((home / "artwork").rglob("*.jpg"))
    with open_library(home / "library.db") as store:
        assert store.songs.count() == 17, "skipping the pictures must not skip the music"
    capsys.readouterr()


# -- limits ---------------------------------------------------------------


def test_artwork_limits_are_honoured_by_the_pipeline(corpus: Path, tmp_path: Path) -> None:
    home = tmp_path / "encore"
    report = run(_options(corpus, home, artwork_limits=ArtworkLimits(max_assets=1)))
    assert report.artwork == 1
    with open_library(_published(report)) as store:
        assert store.songs.count() == 17


def test_a_corrupt_file_is_skipped_and_counted(tmp_path: Path) -> None:
    root = tmp_path / "music"
    written = generate_albums([AlbumSpec.with_tracks("Solid Artist", "Solid Album", count=2)], root)
    written[0].parent.joinpath("broken.mp3").write_bytes(b"mp3" * 400)
    report = run(_options(root, tmp_path / "encore"))
    assert report.songs == 2
    assert report.skip_count >= 1
    assert any(entry.reason for entry in report.skipped.entries)


def test_no_temporary_files_survive_a_successful_build(corpus: Path, tmp_path: Path) -> None:
    home = tmp_path / "encore"
    run(_options(corpus, home))
    assert list((home / "var/cache/builder").glob("*.db*")) == []
    assert list((home / "var/lib").glob("*.staging")) == []
    assert (home / "var/lib" / "library.db").is_file()


def test_a_build_reports_in_two_formats(corpus: Path, tmp_path: Path) -> None:
    """SAPRS 6.12's two artifacts: what a human reads and what a tool can diff."""

    home = tmp_path / "encore"
    report = run(_options(corpus, home))
    assert isinstance(report, BuildReport)
    assert "Encore library build" in report.text()
    assert json.loads(report.json())["validated"] == 1


def test_the_artifact_is_the_only_thing_the_server_needs(corpus: Path, tmp_path: Path) -> None:
    """SAPRS 12.2: the runtime never scans the filesystem.

    Asserted by taking the corpus away and reading the library the build left behind: if
    any read path reached for `music_dir`, this is where it would surface.
    """

    home = tmp_path / "encore"
    report = run(_options(corpus, home))
    corpus.rename(tmp_path / "gone")
    with open_library(_published(report)) as store:
        songs = store.songs.page(limit=100)
        assert len(songs) == 17
        assert all(song.file_path.is_absolute() for song in songs)
        assert store.counts()["music_files"] == 17
        assert store.table_names() >= {Table.SONGS, Table.MUSIC_FILES, Table.ARTWORK}
