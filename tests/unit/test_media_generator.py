"""The synthetic media generator's own contract (PBK 16, SAPRS 14.9).

These were placeholder assertions during bootstrap, when generation raised. The
generator is implemented now, so the assertions are about behaviour rather than about
the absence of it — and the one that matters most is the honesty check: a file this
module writes must report the duration the spec asked for, or every Builder test built
on top of it is asserting on a number that came from the fixture rather than the
container.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.media import (
    GENERABLE_FORMATS,
    SUPPORTED_FORMATS,
    AlbumSpec,
    MediaSpec,
    SyntheticMediaUnavailableError,
    cover_image,
    generate_albums,
    generate_track,
)


def test_formats_match_the_specification() -> None:
    """SAPRS 1.2 promises MP3, FLAC, AAC and m4a."""

    assert set(SUPPORTED_FORMATS) == {"mp3", "flac", "aac", "m4a"}


def test_filename_is_stable_and_slugged() -> None:
    spec = MediaSpec(title="Fly Me to the Moon!", artist="Sinatra", album="Nice", track_number=3)
    assert spec.filename == "03-fly-me-to-the-moon.mp3"


def test_generated_track_reports_the_requested_duration(tmp_path: Path) -> None:
    """The container, not the fixture, is the source of truth for a duration."""

    from mutagen import File

    path = generate_track(
        MediaSpec(title="Silent", artist="Nobody", album="Nothing", duration_seconds=2.0), tmp_path
    )
    audio = File(path)
    assert audio is not None
    assert audio.info is not None
    assert abs(audio.info.length - 2.0) <= 0.06


def test_generated_track_carries_the_tags_it_was_given(tmp_path: Path) -> None:
    from mutagen import File

    audio = File(
        generate_track(
            MediaSpec(
                title="Tagged",
                artist="Nobody",
                album="Nothing",
                track_number=4,
                date="1999",
                genre="Blues",
                album_artist="Nobody With Help",
            ),
            tmp_path,
        )
    )
    tags = dict(audio.tags or {})
    assert str(tags["TPE1"][0]) == "Nobody"
    assert str(tags["TIT2"][0]) == "Tagged"
    assert str(tags["TALB"][0]) == "Nothing"
    assert str(tags["TRCK"][0]) == "4"
    assert str(tags["TPE2"][0]) == "Nobody With Help"


@pytest.mark.parametrize("fmt", sorted(set(SUPPORTED_FORMATS) - set(GENERABLE_FORMATS)))
def test_unsynthesisable_formats_fail_loudly(tmp_path: Path, fmt: str) -> None:
    """A test that asked for m4a and quietly got an mp3 would pass for the wrong reason."""

    spec = MediaSpec(title="Silent", artist="Nobody", album="Nothing", fmt=fmt)
    with pytest.raises(SyntheticMediaUnavailableError, match="cannot synthesise"):
        generate_track(spec, tmp_path)


def test_album_generation_lays_the_tree_out(tmp_path: Path) -> None:
    """`<artist>/<album>/`, because ADR-010's path hint needs a directory to read."""

    album = AlbumSpec.with_tracks("The Test Artists", "Nice", count=2)
    paths = generate_albums([album], tmp_path)
    assert len(paths) == 2
    assert {path.parent.name for path in paths} == {"nice"}
    assert paths[0].parent.parent.name == "the-test-artists"


def test_cover_image_is_decodable() -> None:
    """A hand-assembled JPEG parses as a file and fails as an image (see `cover_image`)."""

    import io

    from PIL import Image

    assert Image.open(io.BytesIO(cover_image())).size == (120, 120)


def test_silence_flag_is_honest(tmp_path: Path) -> None:
    """`silence=False` still writes silence, so the field is documentation of a limit."""

    path = generate_track(
        MediaSpec(title="Tone", artist="Nobody", album="Nothing", silence=False), tmp_path
    )
    assert path.read_bytes().count(b"\x00") > path.stat().st_size // 2
