"""Regression: #52 — an incremental rebuild published a library with no artwork in it.

Reproduced on the appliance, over its own 3,049 files:

```console
$ encore-builder --music-dir /opt/music --library /tmp/inc/library.db --artwork-dir /tmp/inc/artwork
  Artwork            0  images written to the cache
  [ok     ] artwork
Published /tmp/inc/library.db

$ sqlite3 /tmp/inc/library.db 'select count(*) from songs where artwork_id is not null'
0
$ find /tmp/inc/artwork -name '*.jpg' | wc -l
222
```

The library it built *from* held 222 images and 1,060 covered songs. Nothing crashed, the
artifact validated, and the rename happened, which is what a guest notices as every album
turning grey.

The mechanism was a cache field with a loader and no reader. `Reusable.artwork` is documented
("so an unchanged build writes zero images"), populated by `PreviousLibrary` from
`music_files.embedded_artwork_sha`, and was read by nothing — while `_extract` kept only
`earlier.probe`, and a restored `ProbeResult` carries tags and a duration and no image bytes
(SAPRS 5.6 forbids BLOBs, so nothing stored could hold them). Stage 5 therefore saw 3,049
files with no embedded artwork and wrote zero references.

Three tests, and the third is what made the first two interesting:

1. Build twice. The second artifact references exactly the images the first one did, and
   wrote no new files.
2. The same for a build resumed from a stage checkpoint, which is #52's other cache and has
   the same shape.
3. Retag one file and rebuild. Its reference must come from a re-read, not from the cache —
   the rule `Reusable.matches` states and, until this issue's fix, nothing called. That test
   fails for a second reason too: before the key was applied, the *title* stayed stale as
   well, because the reuse decision had been "is this path in the last build", which is not a
   question about the file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.builder.pipeline import BuildOptions, run
from apps.builder.report import BuildReport
from encore.repositories.contract import Table
from tests.support.media import AlbumSpec, MediaSpec, cover_image, generate_albums, generate_track

pytestmark = pytest.mark.regression

_COVER = (30, 90, 150)


def _corpus(root: Path) -> Path:
    """Twelve files, two covers, one of them FLAC: a library with pictures in it."""

    music = root / "music"
    generate_albums(
        [
            AlbumSpec.with_tracks("Covered Artist", "First Album", count=6, artwork=cover_image()),
            AlbumSpec.with_tracks("Covered Artist", "Second Album", count=6, fmt="flac"),
        ],
        music,
    )
    return music


def _options(music: Path, home: Path, **overrides: object) -> BuildOptions:
    return BuildOptions(
        music_dir=music,
        library_db=home / "library.db",
        artwork_dir=home / "artwork",
        temp_dir=home / "tmp",
        library_version="regression-52",
        incremental=True,
        **overrides,  # type: ignore[arg-type]
    )


def _published(report: BuildReport) -> Path:
    assert report.published_to is not None, "\n".join([*report.errors, *report.warnings])
    return report.published_to


def _references(database: Path) -> dict[str, str]:
    """Every catalogued file, and the image it points at (empty for none).

    The subject of this file. Counting cache *writes* is the assertion #52 grew up behind:
    it is true of the broken build and of the fixed one, and the difference between those two
    states is in this map.
    """

    with sqlite3.connect(database) as connection:
        return {
            str(path): str(digest or "")
            for path, digest in connection.execute(
                f"SELECT f.path, a.sha256 FROM {Table.MUSIC_FILES} AS f"
                f" LEFT JOIN {Table.SONGS} AS s ON s.music_file_id = f.id"
                f" LEFT JOIN {Table.ARTWORK} AS a ON a.id = s.artwork_id"
            )
        }


def _album_covers(database: Path) -> dict[str, str]:
    with sqlite3.connect(database) as connection:
        return {
            str(title): str(digest or "")
            for title, digest in connection.execute(
                f"SELECT a.title, w.sha256 FROM {Table.ALBUMS} AS a"
                f" LEFT JOIN {Table.ARTWORK} AS w ON w.id = a.artwork_id"
            )
        }


def _cache(cache_dir: Path) -> dict[str, int]:
    """The cache as an operator's `find` sees it: name, and when it was last written."""

    return {path.name: path.stat().st_mtime_ns for path in cache_dir.rglob("*.jpg")}


def _retag(path: Path, **values: str) -> None:
    """Rewrite one file's tags, leaving its audio and its cover alone.

    A real retag through any tagger: the tag block is rewritten, so the file's size and
    `mtime` move even though the sound does not.
    """

    import mutagen.easyid3

    audio = mutagen.easyid3.EasyID3(str(path))
    for name, value in values.items():
        audio[name] = value
    audio.save()


# -- 1. the two builds ----------------------------------------------------


def test_an_incremental_rebuild_keeps_every_reference(tmp_path: Path) -> None:
    music, home = _corpus(tmp_path).absolute(), tmp_path / "encore"
    first = run(_options(music, home))
    before = _references(_published(first))

    second = run(_options(music, home))

    assert not second.errors
    assert second.reused == 12, "the incremental claim: nothing was opened a second time"
    assert second.artwork == 0, "the other incremental claim: nothing was written twice"
    assert _references(_published(second)) == before, (
        "#52: the second artifact references the same images the first one did"
    )
    assert first.artwork_held == second.artwork_held == 1
    assert second.artwork_reused == 6, "the six files that carried a cover still have one"


def test_the_albums_are_still_covered(tmp_path: Path) -> None:
    """The artist and album pictures are derived from the same references.

    `_associate_artwork` fills `albums.artwork_id` from the files it saw, so the grey tile
    a guest notices is a second thing #52 broke, and it is asserted separately because a
    build can restore `songs.artwork_id` and still lose the grouping.
    """

    music, home = _corpus(tmp_path).absolute(), tmp_path / "encore"
    first = run(_options(music, home))
    before = _album_covers(_published(first))

    second = run(_options(music, home))
    assert _album_covers(_published(second)) == before
    assert any(before.values()), (
        "the fixture must have produced a covered album, or this is nothing"
    )


def test_an_incremental_rebuild_writes_no_cache_files(tmp_path: Path) -> None:
    """`Reusable.artwork`'s sentence, executed: an unchanged build writes zero images."""

    music, home = _corpus(tmp_path).absolute(), tmp_path / "encore"
    run(_options(music, home))
    cache = home / "artwork"
    before = _cache(cache)
    assert before, "the first build has to have written something for this to mean anything"

    report = run(_options(music, home))

    assert _published(report)
    assert _cache(cache) == before, "not a rewrite, not a new file, not a touched mtime"


# -- 2. the resumed build -------------------------------------------------


def test_a_resumed_build_keeps_the_references_the_interrupted_one_made(tmp_path: Path) -> None:
    """#52's acceptance criterion: a resumed build is not worse than an incremental one.

    A `publish=False` run is the shape of a build that died after stage 5 — it records its
    stage checkpoints and renames nothing — so the next run's only memory of the covers is
    the checkpoint file. `Checkpoints` stores tags and durations, and image bytes are not a
    thing a JSON cache holds, so what it carries now is the reference stage 5 resolved to.
    """

    music, home = _corpus(tmp_path).absolute(), tmp_path / "encore"
    interrupted = run(_options(music, home, publish=False))
    assert interrupted.published_to is None
    assert interrupted.artwork == 1, "stage 5 ran, and wrote the cache"
    assert (home / "tmp" / "extraction.json").is_file()

    resumed = run(_options(music, home))

    assert _published(resumed)
    assert resumed.reused == 12, "the checkpoint did its job: no file was opened again"
    assert len(_references(_published(resumed))) == 12
    assert sum(1 for digest in _references(_published(resumed)).values() if digest) == 6, (
        "and the six covered files are still covered"
    )


# -- 3. the key rule ------------------------------------------------------


def test_a_retagged_file_is_reread_for_its_picture(tmp_path: Path) -> None:
    """ADR-010's key, applied: a file whose tags changed is not the file that was cached.

    This is the half of #52 that the reuse of `Reusable.artwork` would have got wrong
    quietly: carry the reference for *every* file the previous library mentions and the
    tests above pass while a retagged album keeps its old cover forever.
    """

    music, home = _corpus(tmp_path).absolute(), tmp_path / "encore"
    target = generate_track(
        MediaSpec(
            title="Loose Track", artist="Covered Artist", album="First Album", artwork=cover_image()
        ),
        music / "Loose",
    )
    first = run(_options(music, home))
    #: Read before the second build: both runs publish to `paths.library_db`, so the first
    #: artifact is the file the second one renames over — asking for it afterwards is asking
    #: the same file twice and calling it a comparison.
    before = _references(_published(first))

    # The same file, re-saved by the tagger that fixed its metadata: same name, same title,
    # a different picture. Only size and `mtime` say it moved, and they are the parts of the
    # key a `stat()` can answer.
    generate_track(
        MediaSpec(
            title="Loose Track",
            artist="Covered Artist",
            album="First Album",
            artwork=cover_image(size=140, color=(200, 40, 40)),
        ),
        target.parent,
    )

    second = run(_options(music, home))

    references = _references(_published(second))
    assert references[str(target)] != before[str(target)], (
        "the file was rewritten; its cover is a different image and must be re-read"
    )
    assert second.reused == 12, "one file moved and twelve did not"
    assert second.artwork == 1, "one new image written, six references carried over"
    assert second.artwork_held == 2
    assert sum(1 for digest in references.values() if digest) == 7, (
        "thirteen files, seven with an embedded cover: the six of the first album, the one "
        "that was re-saved, and none of the FLACs (ADR-010 measures 0% of them)"
    )
    assert second.artwork_reused == 6


def test_a_stale_reuse_is_a_stale_library(tmp_path: Path) -> None:
    """What #52's mechanism cost beyond the pictures: the metadata itself never updated.

    `_extract` reused a previous library's probe on the strength of the path alone, so the
    tag hash and `mtime` ADR-010 puts in the key were compared by nothing, and an operator
    who retagged a file and rebuilt got the library they had. Found while writing the test
    above, and the reason `Reusable.matches` is now called rather than tested in isolation.
    """

    music, home = _corpus(tmp_path).absolute(), tmp_path / "encore"
    first = run(_options(music, home))
    target = sorted(music.rglob("*.mp3"))[0]
    reference_before = _references(_published(first))[str(target)]
    _retag(target, title="A Brand New Title")

    second = run(_options(music, home))

    with sqlite3.connect(_published(second)) as connection:
        titles = {
            str(path): str(title)
            for path, title in connection.execute(
                f"SELECT f.path, s.title FROM {Table.MUSIC_FILES} AS f"
                f" JOIN {Table.SONGS} AS s ON s.music_file_id = f.id"
            )
        }
    assert titles[str(target)] == "A Brand New Title", (
        "a retagged file is a changed file (SAPRS 6.9, ADR-010)"
    )
    assert second.reused == 11, "eleven reuses and one read: that is the whole of the promise"
    assert _references(_published(second))[str(target)] == reference_before, (
        "a title change is not a picture change; the cache file is the same image either way"
    )
