"""Synthetic media generation (PBK 16, SAPRS 14.9, AEP 12).

Playback, builder, performance and Party Simulation tests all need real files on disk.
The bootstrap shipped this module as a declared placeholder because generating audio
means an encoder, and a Pi in a classroom does not have `ffmpeg`. It turns out not to
need one: the Builder reads *tags and durations*, never samples, and both are facts a
container header can state without a single encoded frame.

**What a generated file is.** A valid MP3 or FLAC container — real MPEG frame headers,
a real STREAMINFO block — carrying zero-filled audio, tagged through mutagen the way a
rip would be. `mutagen` reports a duration to the millisecond, and every code path in
`apps/builder/extraction.py` is exercised for real.

**What it is not.** Decodable audio. The frames are zeros, so `mpv` would play silence
at best. Anything that measures actual playback — milestone 8, the party simulation's
audio assertions — needs a fixture recorded by a real encoder and checked into
`tests/fixtures/`, and must skip rather than substitute these.

`aac` and `m4a` raise `SyntheticMediaUnavailableError` rather than pretending: writing
a valid MP4 container by hand means building an `mp4a` sample table, and the day a test
needs one is the day it should say so out loud.
"""

from __future__ import annotations

import io
import re
import struct
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Final, cast

#: Formats promised by SAPRS 1.2.
SUPPORTED_FORMATS: Final[tuple[str, ...]] = ("mp3", "flac", "aac", "m4a")

#: Formats this module can actually synthesise, and the reason the other two are not a
#: silent fallback: a test that asked for m4a and got an mp3 would pass for the wrong
#: reason.
GENERABLE_FORMATS: Final[tuple[str, ...]] = ("mp3", "flac")

#: MPEG-1 Layer III at 128 kbit/s, 44.1 kHz, mono. Every frame is 417 bytes and lasts
#: 1152 samples, which is what makes an exact duration out of a frame count.
_MP3_FRAME_BYTES: Final = 417
_MP3_FRAME_SECONDS: Final = 1152 / 44100
_MP3_HEADER: Final = bytes((0xFF, 0xFB, 0x90, 0xC4))

#: FLAC is generated at one rate and one depth; STREAMINFO is the whole file.
_SAMPLE_RATE: Final = 44100
_BITS_PER_SAMPLE: Final = 16


class SyntheticMediaUnavailableError(RuntimeError):
    """Raised when a test asks for media this module cannot honestly synthesise."""


@dataclass(frozen=True)
class MediaSpec:
    """One synthetic track.

    Attributes:
        title: Track title written into metadata.
        artist: Artist name.
        album: Album name.
        track_number: Position on the album.
        duration_seconds: Target length of the silent audio.
        fmt: Container/codec, one of `SUPPORTED_FORMATS`.
        silence: Whether to emit silence rather than a tone. Always silence here —
            the frames are zeros — and the field stays because a caller that asked for
            a tone should not silently get silence in a *playback* test.
        album_artist / disc_number / genre / date: Optional tags, absent when None,
            which is a different fixture from an empty one.
        artwork: Cover bytes to embed, or None for a file with no image. Most of a
            real corpus has none (SAPRS 6.7 measures 77% of MP3s), so "no artwork" is
            the default rather than the exception case.
        musicbrainz_album_id / musicbrainz_trackid: Enrichment fixtures (SAPRS 6.6).
        extra: Anything else to write, keyed by tag name.
    """

    title: str
    artist: str
    album: str
    track_number: int = 1
    duration_seconds: float = 1.0
    fmt: str = "mp3"
    silence: bool = True
    album_artist: str | None = None
    disc_number: int | None = None
    genre: str | None = None
    date: str | None = None
    artwork: bytes | None = None
    musicbrainz_album_id: str | None = None
    musicbrainz_trackid: str | None = None
    extra: Mapping[str, str] = field(default_factory=dict)

    @property
    def filename(self) -> str:
        safe = _slug(self.title)
        return f"{self.track_number:02d}-{safe}.{self.fmt}"

    @property
    def duration(self) -> timedelta:
        """The exact duration the container will report, as the domain speaks it."""

        return timedelta(
            seconds=round(self.duration_seconds / _frame_step(self.fmt)) * _frame_step(self.fmt)
        )


@dataclass(frozen=True)
class AlbumSpec:
    """A group of tracks sharing artist/album metadata."""

    artist: str
    album: str
    tracks: Sequence[MediaSpec] = field(default_factory=tuple)
    artwork: bytes | None = None

    @classmethod
    def with_tracks(
        cls,
        artist: str,
        album: str,
        *,
        count: int = 3,
        fmt: str = "mp3",
        duration_seconds: float = 1.0,
        artwork: bytes | None = None,
        **tags: object,
    ) -> AlbumSpec:
        """Build a whole album in one line, for the tests that need volume not detail."""

        return cls(
            artist=artist,
            album=album,
            artwork=artwork,
            tracks=tuple(
                MediaSpec(
                    # The album's own words, not the folder's: a title that reads
                    # "First-Album Track 1" would make every assertion about a built
                    # library a test of this helper's slug function.
                    title=f"{album} Track {index}",
                    artist=artist,
                    album=album,
                    track_number=index,
                    duration_seconds=duration_seconds,
                    fmt=fmt,
                    artwork=artwork,
                    # `**tags` is open-ended on purpose: an album spec that could only set
                    # the four fields below would force every unusual tag through a
                    # subclass, and the tests that need one are about unusual tags.
                    **cast("dict[str, str]", tags),  # type: ignore[arg-type]
                )
                for index in range(1, count + 1)
            ),
        )

    @property
    def total_duration(self) -> timedelta:
        return sum((track.duration for track in self.tracks), timedelta(0))


def generate_track(spec: MediaSpec, destination: Path) -> Path:
    """Create one synthetic track inside `destination` and return its path.

    The file is written, tagged and closed; nothing is registered anywhere, so a test
    may call this in a loop over a temporary directory without a fixture in the way.

    Raises:
        SyntheticMediaUnavailableError: `spec.fmt` is a format Encore promises and this
            generator cannot honestly synthesise.
    """

    if spec.fmt not in GENERABLE_FORMATS:
        raise SyntheticMediaUnavailableError(
            f"cannot synthesise {spec.fmt!r} without an encoder (PBK 16); generated "
            f"files exist for {', '.join(GENERABLE_FORMATS)}"
        )
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / spec.filename
    if spec.fmt == "flac":
        _write_flac(path, spec.duration_seconds)
    else:
        _write_mp3(path, spec.duration_seconds)
    _tag(path, spec)
    return path


def generate_albums(specs: Iterable[AlbumSpec], destination: Path) -> list[Path]:
    """Write albums under `destination/<artist>/<album>/`, the corpus layout.

    The layout matters more than it looks: ADR-010's path-hint level only ever applies
    where the directory above a file is an artist tree, so a fixture that dumps every
    file in one flat folder cannot test that rule at all.
    """

    written: list[Path] = []
    for album in specs:
        directory = destination / _slug(album.artist) / _slug(album.album)
        written.extend(generate_track(track, directory) for track in album.tracks)
    return written


def generate_library(root: Path, albums: Iterable[AlbumSpec]) -> list[Path]:
    """Alias of `generate_albums` under the name the Builder tests read better with."""

    return generate_albums(albums, root)


def cover_image(*, size: int = 120, color: tuple[int, int, int] = (10, 60, 140)) -> bytes:
    """A real, decodable JPEG to embed as artwork.

    Hand-assembled JPEG bytes parse as a file and fail as an image, which is
    indistinguishable from corruption to Pillow. Pillow is a declared runtime
    dependency (SAPRS 3.1), so a fixture can afford to encode one properly — and a test
    that asserts on a cover the Builder can actually resize is worth more than one that
    asserts on a header.
    """

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def silent_track(
    *,
    title: str,
    artist: str = "Silent Artist",
    album: str = "Silent Album",
    track_number: int = 1,
    duration_seconds: float = 1.0,
    fmt: str = "mp3",
) -> MediaSpec:
    """The one-line form most Builder tests want."""

    return MediaSpec(
        title=title,
        artist=artist,
        album=album,
        track_number=track_number,
        duration_seconds=duration_seconds,
        fmt=fmt,
    )


# -- containers -----------------------------------------------------------


def _write_mp3(path: Path, seconds: float) -> None:
    """A silent MPEG-1 Layer III: valid frame headers, zeroed payloads.

    mutagen measures an MP3 by counting frames and will do that work on a file whose
    audio data is all zeros as cheerfully as on a recording, which is the whole trick
    behind this module.
    """

    frames = max(1, round(seconds / _MP3_FRAME_SECONDS))
    payload = _MP3_HEADER + bytes(_MP3_FRAME_BYTES - 4)
    path.write_bytes(payload * frames)


def _write_flac(path: Path, seconds: float) -> None:
    """A FLAC with a real STREAMINFO block and no audio frames.

    mutagen reads the length out of STREAMINFO rather than by decoding, so the file is
    two facts — a sample count and a sample rate — and does not need to contain a
    single valid frame. Encoding silence would have meant shipping `ffmpeg` as a test
    dependency.
    """

    samples = max(1, round(seconds * _SAMPLE_RATE))
    stream_info = struct.pack(">HH", 4096, 4096) + bytes(6) + _bits(samples) + bytes(16)
    header = struct.pack(">I", len(stream_info))[1:]
    # 0x80 is the "last metadata block" flag; without it mutagen reads past STREAMINFO
    # looking for another header and finds the end of the file.
    path.write_bytes(b"fLaC" + bytes((0x80,)) + header + stream_info)


def _bits(samples: int) -> bytes:
    """STREAMINFO's packed tail: rate, channels, depth, sample count.

    Bit-packed by hand because the fields cross byte boundaries in three places and
    there is no `struct` format for it. Mono, 16-bit: 4 + 6 + 8 + 16 is STREAMINFO's
    34 bytes, which mutagen checks.
    """

    value = (_SAMPLE_RATE << 44) | (0 << 41) | ((_BITS_PER_SAMPLE - 1) << 36) | samples
    return value.to_bytes(8, "big")


def _frame_step(fmt: str) -> float:
    """The quantum a container's length comes in, for `MediaSpec.duration`."""

    return _MP3_FRAME_SECONDS if fmt == "mp3" else 1 / _SAMPLE_RATE


# -- tags -----------------------------------------------------------------


def _tag(path: Path, spec: MediaSpec) -> None:
    """Write the tags through mutagen, so the file is tagged like a real one.

    Vorbis comments on FLAC and ID3 on MP3, because those are what the encoders in the
    corpus actually wrote and what `apps/builder/extraction.py` translates from.
    """

    if spec.fmt == "flac":
        _tag_flac(path, spec)
    else:
        _tag_mp3(path, spec)


def _fields(spec: MediaSpec) -> dict[str, str | None]:
    return {
        "artist": spec.artist,
        "title": spec.title,
        "album": spec.album,
        "album_artist": spec.album_artist,
        "track": f"{spec.track_number}" if spec.track_number else None,
        "disc": str(spec.disc_number) if spec.disc_number else None,
        "genre": spec.genre,
        "date": spec.date,
        "musicbrainz_album_id": spec.musicbrainz_album_id,
        "musicbrainz_recording_id": spec.musicbrainz_trackid,
        **spec.extra,
    }


def _tag_flac(path: Path, spec: MediaSpec) -> None:
    from mutagen.flac import FLAC, Picture

    flac = FLAC(path)
    for key, value in _fields(spec).items():
        if value is not None:
            flac[key.upper()] = [value]
    if spec.artwork is not None:
        picture = Picture()
        picture.type = 3
        picture.mime = "image/jpeg"
        picture.data = spec.artwork
        flac.add_picture(picture)
    flac.save()


def _tag_mp3(path: Path, spec: MediaSpec) -> None:
    from mutagen.id3 import (
        APIC,
        ID3,
        TALB,
        TCMP,
        TCON,
        TDRC,
        TIT2,
        TPE1,
        TPE2,
        TPOS,
        TRCK,
        TXXX,
    )

    frames: list[object] = []
    for frame, value in (
        (TIT2, spec.title),
        (TPE1, spec.artist),
        (TPE2, spec.album_artist),
        (TALB, spec.album),
        (TRCK, f"{spec.track_number}" if spec.track_number else None),
        (TPOS, str(spec.disc_number) if spec.disc_number else None),
        (TCON, spec.genre),
        (TDRC, spec.date),
        (TCMP, "1" if spec.disc_number else None),
    ):
        if value is not None:
            frames.append(frame(encoding=3, text=[value]))
    for key, value in {
        "MUSICBRAINZ ALBUM ID": spec.musicbrainz_album_id,
        "MUSICBRAINZ TRACKID": spec.musicbrainz_trackid,
        **spec.extra,
    }.items():
        if value is not None:
            frames.append(TXXX(encoding=3, desc=key, text=[value]))
    if spec.artwork is not None:
        frames.append(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=spec.artwork))

    tags = ID3()
    for frame in frames:
        tags.add(frame)
    tags.save(path, v2_version=3)


_SLUG_STRIP: Final = re.compile(r"[^\w\s-]")
_SLUG_SPACE: Final = re.compile(r"[\s_]+")


def _slug(value: str) -> str:
    """A filesystem-safe directory or file name, lowercase, no punctuation.

    Stable across runs by construction: no counter, no hash, no timestamp. A fixture
    whose name changes between runs is a fixture that cannot be asserted on.
    """

    text = _SLUG_STRIP.sub("", value).strip().lower()
    return _SLUG_SPACE.sub("-", text) or "untitled"
