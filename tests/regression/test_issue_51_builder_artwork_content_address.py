"""Regression: #51 — two byte forms of one cover aborted the whole build.

Observed on an installed appliance. `encore-builder` died inside the write:

```text
File "/opt/encore/application/apps/builder/construction.py", line 166, in _write_artwork
    cursor = connection.execute(
sqlite3.IntegrityError: UNIQUE constraint failed: artwork.relative_path
```

and the library the operator ended up publishing — with `--max-artwork 0`, the only way
to get a build to finish — has 0 rows in `artwork` sitting on top of a cache of 222 JPEGs,
which is what a guest sees as a grey tile where every cover should be (SAPRS 5.6, 6.7).

The mechanism, measured over that host's 3,049 files: 224 distinct embedded payloads
normalize to 222 distinct images. Two albums each carried the same picture twice in two
byte forms — a PNG whose ancillary chunks differ from its eight siblings at byte 131, and a
JPEG one byte longer than its thirteen siblings, identical pixels either way. Normalization
decodes and re-encodes, so both forms write identical bytes to one cache file. `generate`
keyed its deduplication on the payload rather than on what it wrote (contradicting its own
docstring and `ArtworkOutcome.assets`), so it returned two assets naming one path, and the
`UNIQUE` index was the first place anybody noticed — inside the transaction, with the build
dead and nothing published.

Three things are asserted here, in the order they failed:

1. A build over two byte forms of one cover publishes, with one `artwork` row that every
   file carrying that cover references.
2. The stage itself collapses byte forms, so construction is not the only thing that knows
   a content address is about bytes-written.
3. Construction cannot abort on a repeated path even if a stage hands it one, and a database
   that cannot be written is still a report rather than a traceback.
"""

from __future__ import annotations

import hashlib
import io
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from apps.builder.artwork import ArtworkLimits, _normalize, _store, generate
from apps.builder.construction import build
from apps.builder.main import main
from apps.builder.pipeline import BuildOptions, run
from apps.builder.precedence import ArtistTrees, normalize_track
from apps.builder.records import (
    ArtworkAsset,
    ArtworkOutcome,
    DiscoveredFile,
    ExtractedFile,
    Track,
)
from apps.builder.schema import create_schema
from encore.domain import AudioFormat
from encore.repositories.contract import Table
from tests.support.media import AlbumSpec, MediaSpec, cover_image, generate_albums

pytestmark = pytest.mark.regression

#: Two greens one pixel apart, so a normalization that stops being deterministic
#: shows up as a fixture failure below rather than as a test that quietly proves
#: nothing.
_COVER = (20, 120, 60)


def _other_form(payload: bytes, *, container: str = "PNG") -> bytes:
    """The same picture in another byte form — what a tagged-up corpus actually carries.

    Decoded and re-encoded, so the pixels are identical and the bytes are not. That is
    the #51 input: two files of one album whose covers differ in an ancillary chunk and
    normalize to one image.
    """

    from PIL import Image

    with Image.open(io.BytesIO(payload)) as image:
        image.load()
        buffer = io.BytesIO()
        image.save(buffer, format=container)
    return buffer.getvalue()


def _cache_address(payload: bytes) -> str:
    """The digest `store_one` would name this payload's cache file after."""

    normalized = _normalize(payload, ArtworkLimits())
    assert normalized is not None, "the fixture stopped being a decodable image"
    return hashlib.sha256(normalized[0]).hexdigest()


def _assert_the_fixture_still_collides(*payloads: bytes) -> None:
    """Guard the premise, or a Pillow edit turns this file into a passing no-op.

    #51 happened because two byte strings normalize to one image. If they ever stop
    doing that, the tests below would go green while the bug stayed in the code, which
    is the one outcome a regression file must not allow.
    """

    assert len({bytes(payload) for payload in payloads}) == len(payloads), (
        "the payloads must be different bytes, or this file tests nothing"
    )
    assert len({_cache_address(payload) for payload in payloads}) == 1, (
        "the payloads must normalize to one image, or #51 no longer reproduces here"
    )


@pytest.fixture
def forms() -> tuple[bytes, bytes]:
    """One cover, as two files would carry it."""

    cover = cover_image(size=180, color=_COVER)
    alternative = _other_form(cover)
    _assert_the_fixture_still_collides(cover, alternative)
    return cover, alternative


def _corpus(root: Path, forms: tuple[bytes, bytes]) -> Path:
    """A two-track album whose tracks carry one cover in two byte forms.

    Deliberately one album: #51 was not a strange library, it was 14 copies of a cover
    and a fifteenth that a different tagger had re-saved.
    """

    first, second = forms
    music = root / "music"
    generate_albums(
        [
            AlbumSpec(
                artist="Two Forms",
                album="One Cover",
                tracks=(
                    MediaSpec(
                        title="First Form",
                        artist="Two Forms",
                        album="One Cover",
                        track_number=1,
                        artwork=first,
                    ),
                    MediaSpec(
                        title="Second Form",
                        artist="Two Forms",
                        album="One Cover",
                        track_number=2,
                        artwork=second,
                    ),
                ),
            )
        ],
        music,
    )
    return music


def _options(music: Path, home: Path) -> BuildOptions:
    return BuildOptions(
        music_dir=music,
        library_db=home / "library.db",
        artwork_dir=home / "artwork",
        temp_dir=home / "tmp",
        library_version="regression-51",
        incremental=False,
    )


def _track(root: Path, name: str, *, payload: bytes) -> Track:
    """A track whose file carries `payload`, for the stage-level test below."""

    path = root / name
    extracted = ExtractedFile(
        file=DiscoveredFile(path=path, format=AudioFormat.MP3, size_bytes=1024, mtime_ns=1),
        tags={"artist": "Two Forms", "album": "One Cover", "title": path.stem},
        duration=timedelta(seconds=180),
        embedded_artwork=payload,
    )
    return normalize_track(extracted, root=root, trees=ArtistTrees())


# -- 1. the build ---------------------------------------------------------


def test_two_byte_forms_of_one_cover_still_build_a_library(
    tmp_path: Path, forms: tuple[bytes, bytes]
) -> None:
    """The original failure: this call raised sqlite3.IntegrityError out of `run`."""

    home = tmp_path / "encore"
    report = run(_options(_corpus(tmp_path, forms), home))

    assert not report.errors, "nothing here is a validation finding"
    assert report.published_to == home / "library.db", "\n".join(report.errors)
    assert report.songs == 2, "a cover that cannot be written must not cost a song (6.7)"


def test_the_library_references_the_cover_it_wrote(
    tmp_path: Path, forms: tuple[bytes, bytes]
) -> None:
    home = tmp_path / "encore"
    report = run(_options(_corpus(tmp_path, forms), home))
    assert report.published_to is not None

    cached = sorted((home / "artwork").rglob("*.jpg"))
    assert len(cached) == 1, "one image is one cache file, whatever it arrived as"
    assert report.artwork == 1

    with sqlite3.connect(report.published_to) as connection:
        rows = connection.execute(
            f"SELECT id, kind, relative_path, sha256 FROM {Table.ARTWORK}"
        ).fetchall()
        assert len(rows) == 1, "artwork.relative_path is UNIQUE; the stage must be too"
        assert rows[0][1] == "album"
        assert Path(rows[0][2]) == cached[0].relative_to(home / "artwork")

        referenced = connection.execute(
            f"SELECT COUNT(DISTINCT artwork_id) FROM {Table.SONGS} WHERE artwork_id IS NOT NULL"
        ).fetchone()[0]
        assert referenced == 1, "both byte forms resolve to the one row"

        album = connection.execute(
            f"SELECT artwork_id FROM {Table.ALBUMS} WHERE title = 'One Cover'"
        ).fetchone()[0]
        assert album == rows[0][0], "the album is covered too"


# -- 2. the stage ---------------------------------------------------------


def test_the_artwork_stage_addresses_what_it_wrote(
    tmp_path: Path, forms: tuple[bytes, bytes]
) -> None:
    """`generate` deduplicates on the cache address, not on the payload digest.

    The stage's own docstring already says the address is "the digest of the bytes that
    will be written, not of the bytes that were found"; this is that sentence, executed.
    """

    first, second = forms
    tracks = (_track(tmp_path, "a.mp3", payload=first), _track(tmp_path, "b.mp3", payload=second))
    outcome = generate(tracks, tmp_path / "cache")

    paths = [asset.relative_path for asset in outcome.assets]
    assert len(paths) == len(set(paths)) == 1, paths
    assert len({asset.sha256 for asset in outcome.assets}) == 1
    assert outcome.generated == 1
    assert outcome.failed == 0
    assert {outcome.by_file[track.path] for track in tracks} == {outcome.assets[0]}, (
        "each file must point at the asset that names its image"
    )


def test_one_payload_still_normalizes_once(
    tmp_path: Path, forms: tuple[bytes, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The payload memo survives the fix, or a 40-track album decodes 40 times.

    #51's fix is a second key, not a replacement of the first: identical bytes are still
    recognized before the expensive part, which is what keeps a 15,000-song build inside
    its time budget (SAPRS 1.8, 14.9).
    """

    first, _second = forms
    tracks = tuple(_track(tmp_path, f"{index}.mp3", payload=first) for index in range(1, 13))
    calls: list[bytes] = []

    def counting(
        payload: bytes, cache_dir: Path, **kwargs: object
    ) -> tuple[ArtworkAsset | None, bool]:
        calls.append(payload)
        return _store(payload, cache_dir, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("apps.builder.artwork._store", counting)
    outcome = generate(tracks, tmp_path / "cache")

    assert len(calls) == 1, "twelve copies of one payload are one normalization"
    assert len(outcome.assets) == 1
    assert len(outcome.by_file) == 12


def test_the_cache_cap_counts_images_not_payloads(
    tmp_path: Path, forms: tuple[bytes, bytes]
) -> None:
    """`--max-artwork` bounds files on disk, which is what its help text promises."""

    first, second = forms
    tracks = (_track(tmp_path, "a.mp3", payload=first), _track(tmp_path, "b.mp3", payload=second))
    outcome = generate(tracks, tmp_path / "cache", limits=ArtworkLimits(max_assets=1))

    assert len(outcome.assets) == 1
    assert outcome.generated == 1
    assert len(list((tmp_path / "cache").rglob("*.jpg"))) == 1


# -- 3. the write, and the way out ----------------------------------------


def test_one_row_per_path_even_when_a_stage_repeats_one(tmp_path: Path) -> None:
    """Construction is the place the `UNIQUE` index bites, so it must not bite.

    The outcome below is what #51 produced. Fixed at the stage, it should no longer
    reach here — but the row is written in the transaction that publishes the library,
    and an aborted build is the cost of getting this wrong twice.
    """

    connection = sqlite3.connect(tmp_path / "library.building.db")
    create_schema(connection)
    shared = Path("album/ab/abcd.jpg")
    asset = ArtworkAsset(kind="album", relative_path=shared, width=600, height=600, sha256="abcd")
    twin = ArtworkAsset(kind="album", relative_path=shared, width=600, height=600, sha256="abcd")
    outcome = ArtworkOutcome(
        assets=(asset, twin),
        by_file={tmp_path / "a.mp3": asset, tmp_path / "b.mp3": twin},
        generated=2,
    )
    built = build(
        connection,
        (_track(tmp_path, "a.mp3", payload=b""), _track(tmp_path, "b.mp3", payload=b"")),
        artwork=outcome,
        rules_version=1,
    )

    rows = connection.execute(f"SELECT COUNT(*) FROM {Table.ARTWORK}").fetchone()[0]
    assert rows == 1
    assert built.artwork == 1, "the reported count is rows written, not assets handed over"
    assert (
        connection.execute(
            f"SELECT COUNT(*) FROM {Table.SONGS} WHERE artwork_id IS NOT NULL"
        ).fetchone()[0]
        == 2
    )
    connection.close()


def test_a_database_that_cannot_be_written_reports_rather_than_traces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other half of #51: an exception escaped `main`, whose docstring promises not to.

    `main` catches `(OSError, ValueError)`; a `sqlite3.IntegrityError` is neither, so the
    operator of an appliance with no covers got a traceback instead of the sentence that
    says the previous library is intact.
    """

    def refusing(options: BuildOptions, **kwargs: object) -> object:
        del kwargs
        raise sqlite3.IntegrityError("UNIQUE constraint failed: artwork.relative_path")

    monkeypatch.setattr("apps.builder.main.run", refusing)
    code = main(
        [
            "--music-dir",
            str(tmp_path),
            "--library",
            str(tmp_path / "library.db"),
            "--artwork-dir",
            str(tmp_path / "artwork"),
            "--temp-dir",
            str(tmp_path / "tmp"),
        ]
    )

    assert code == 3, "a build that could not run is exit 3, documented in main's table"
    assert "previous library is unchanged" in capsys.readouterr().err
