"""Guards for the synthetic media generator hooks (PBK 16).

The generator is intentionally unimplemented during bootstrap. These tests make
that visible: if a future milestone implements generation, the placeholder tests
must be replaced by real assertions rather than silently deleted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.media import (
    SUPPORTED_FORMATS,
    AlbumSpec,
    MediaSpec,
    SyntheticMediaUnavailableError,
    generate_albums,
    generate_track,
)


def test_formats_match_the_specification() -> None:
    """SAPRS 1.2 promises MP3, FLAC, AAC and M4A."""

    assert set(SUPPORTED_FORMATS) == {"mp3", "flac", "aac", "m4a"}


def test_filename_is_stable_and_slugged() -> None:
    spec = MediaSpec(title="Fly Me to the Moon!", artist="Sinatra", album="Nice", track_number=3)
    assert spec.filename == "03-fly-me-to-the-moon.mp3"


def test_track_generation_is_a_declared_placeholder(tmp_path: Path) -> None:
    spec = MediaSpec(title="Silent", artist="Nobody", album="Nothing")
    with pytest.raises(SyntheticMediaUnavailableError, match="not implemented"):
        generate_track(spec, tmp_path)


def test_album_generation_is_a_declared_placeholder(tmp_path: Path) -> None:
    spec = MediaSpec(title="Silent", artist="Nobody", album="Nothing")
    with pytest.raises(SyntheticMediaUnavailableError, match="not implemented"):
        generate_albums([AlbumSpec(artist="Nobody", album="Nothing", tracks=(spec,))], tmp_path)
