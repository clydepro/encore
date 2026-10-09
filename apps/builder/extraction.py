"""Stage 2 — extraction (SAPRS 6.2, 6.4; ADR-010).

Reads what a file says about itself, and nothing else. No cleanup, no guessing, no
MusicBrainz: SAPRS 6.2 gives those their own stages, and ADR-010 explains why the
split matters — extraction is per-format, so a rule applied here would be
re-derived in every reader.

One open per file is deliberate. Mutagen's `easy=True` view would give uniform tag
names for one open and then need a second one for embedded artwork, which at
15,000 files means reading the directory twice. So this module opens a file once
and translates each container's own names into the vocabulary
`encore.domain.CANONICAL_TAGS` fixes. Those translation tables are the whole of
the format knowledge here, and they are data so that the disagreement they encode
is visible.

A file that cannot be read comes back with `error` set rather than raising. The
measured corpus contains files whose tags contradict their path, whose duration is
a lie, and one that is half a GarageBand project; a build that stops at track
3,000 of 3,049 because one header is truncated is exactly the failure ADR-010
calls "one mystery file on a 20 GB drive making the library unbuildable".
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from typing import Any, Final, Protocol, cast, runtime_checkable

from mutagen import File as MutagenFile

from apps.builder.records import DiscoveredFile, ExtractedFile
from encore.domain.media import AudioFormat

__all__ = ["MediaProbe", "MutagenProbe", "ProbeResult", "extract", "tag_hash"]

#: Tag names per container, mapped onto the canonical names `Metadata` uses. Keys
#: are what Mutagen hands back: ID3 frame identifiers, Vorbis comment names, MP4
#: atom names. Order is precedence, so `TDRC` (recording time) beats the legacy
#: `TYER`, and `aART` beats `©ART` for the album artist but not for the track
#: artist — which is the compilation case SAPRS 4.4 cares about.
_ID3_NAMES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "artist": ("TPE1",),
        "album_artist": ("TPE2",),
        "album": ("TALB",),
        "title": ("TIT2",),
        "track": ("TRCK",),
        "disc": ("TPOS",),
        "genre": ("TCON",),
        "date": ("TDRC", "TDRL", "TDOR", "TYER"),
        "musicbrainz_recording_id": ("TXXX:MusicBrainz Track Id",),
        "musicbrainz_release_id": ("TXXX:MusicBrainz Album Id",),
        "musicbrainz_artist_id": ("TXXX:MusicBrainz Artist Id",),
    }
)

_VORBIS_NAMES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "artist": ("artist",),
        "album_artist": ("albumartist",),
        "album": ("album",),
        "title": ("title",),
        "track": ("tracknumber",),
        "disc": ("discnumber",),
        "genre": ("genre",),
        "date": ("date", "originaldate", "year"),
        "musicbrainz_recording_id": ("musicbrainz_trackid", "musicbrainz_recordingid"),
        "musicbrainz_release_id": ("musicbrainz_albumid", "musicbrainz_releasegroupid"),
        "musicbrainz_artist_id": ("musicbrainz_artistid",),
    }
)

_MP4_NAMES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "artist": ("\xa9ART",),
        "album_artist": ("aART",),
        "album": ("\xa9alb",),
        "title": ("\xa9nam",),
        "track": ("trkn",),
        "disc": ("disk",),
        "genre": ("\xa9gen", "gnre"),
        "date": ("\xa9day",),
        "musicbrainz_recording_id": ("----:com.apple.iTunes:MusicBrainz Track Id",),
        "musicbrainz_release_id": ("----:com.apple.iTunes:MusicBrainz Album Id",),
        "musicbrainz_artist_id": ("----:com.apple.iTunes:MusicBrainz Artist Id",),
    }
)

#: Which table a container is read with. Raw ADTS AAC carries no tag block at
#: all, so it lands on the Vorbis table, finds nothing, and falls through to
#: ADR-010's levels 3 and 4 like any other untagged file.
_TABLES: Final[Mapping[AudioFormat, Mapping[str, tuple[str, ...]]]] = MappingProxyType(
    {
        AudioFormat.MP3: _ID3_NAMES,
        AudioFormat.FLAC: _VORBIS_NAMES,
        AudioFormat.M4A: _MP4_NAMES,
        AudioFormat.AAC: _VORBIS_NAMES,
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ProbeResult:
    """What one container reported about itself.

    Attributes:
        tags: Tag names as the container spells them, values reduced to text.
            Absent tags are absent keys, never empty strings: SAPRS 6.4
            distinguishes "no album tag" from `[Unknown Album]`, and an empty
            string would erase that before normalization ever saw it.
        duration: Decoded length from the container, not from a tag. Zero means
            "could not tell", which is a skip, not an unknown.
        artwork_bytes / artwork_mime: Embedded image, if any. Present on 98% of
            M4A, 23% of MP3 and 0% of FLAC in the measured corpus, so None is a
            normal value and not a warning (SAPRS 6.7).
        error: Why the file could not be read. Set means "skip me".
    """

    tags: Mapping[str, str] = MappingProxyType({})
    duration: timedelta = timedelta(0)
    artwork_bytes: bytes | None = None
    artwork_mime: str = "image/jpeg"
    error: str | None = None

    @property
    def usable(self) -> bool:
        return self.error is None


@runtime_checkable
class MediaProbe(Protocol):
    """Something that can answer "what does this file say about itself?".

    A seam rather than an abstraction hunt: the cases ADR-010's precedence rule
    turns on — a tag that contradicts the path, a promo album suffix, an
    `AlbumWrap - ` artist — have to be asserted with real data, and writing
    binary audio for each is slower and flakier than describing it. The
    integration suite uses `MutagenProbe` against files `tests/support/media.py`
    actually generates, so the format knowledge below stays under test.
    """

    def probe(self, file: DiscoveredFile) -> ProbeResult: ...


class MutagenProbe:
    """Reads tags, duration and embedded artwork with Mutagen, one open per file."""

    def __init__(self, *, include_untagged: bool = False) -> None:
        """Args:
        include_untagged: Keep tags outside `CANONICAL_TAGS` in the result, under
            their container's own name. Off by default: the `metadata` table is
            per-field provenance for the fields Encore reasons about, and 2,400
            `BPM`, `MOOD` and `PLAYCOUNT` tags inflate a 15,000-song library to
            store values nothing reads. The originals are still on disk — SAPRS
            6.5's "where appropriate" is this flag.
        """

        self._include_untagged = include_untagged

    def probe(self, file: DiscoveredFile) -> ProbeResult:
        """Return what `file` reports, or an `error` if it could not be read."""

        try:
            audio = MutagenFile(file.path)
        except Exception as error:
            return ProbeResult(error=f"{type(error).__name__}: {error}")
        if audio is None:
            return ProbeResult(error="no known audio structure")

        tags = _translate(audio, file.format, include_untagged=self._include_untagged)
        artwork, mime = _artwork(audio)
        return ProbeResult(
            tags=tags, duration=_duration(audio), artwork_bytes=artwork, artwork_mime=mime
        )


def extract(
    files: Sequence[DiscoveredFile],
    probe: MediaProbe | None = None,
    *,
    previous: Mapping[str, ProbeResult] | None = None,
) -> list[ExtractedFile]:
    """Probe every file, reusing `previous` where the incremental key matches.

    `previous` maps a cache key to a probe result from an earlier build (SAPRS
    6.9). A hit means the file, its size, its timestamps and its tags are all
    unchanged, so the read is skipped; a miss re-reads. Nothing here knows about
    MusicBrainz — 6.6's "not degraded to un-enriched data" is enrichment's
    problem, and enrichment reads its own previous state.
    """

    reader = probe if probe is not None else MutagenProbe()
    results: list[ExtractedFile] = []
    for item in files:
        cached = None if previous is None else previous.get(item.cache_key)
        result = cached if cached is not None else reader.probe(item)
        results.append(_as_extracted(item, result))
    return results


def _as_extracted(file: DiscoveredFile, result: ProbeResult) -> ExtractedFile:
    return ExtractedFile(
        file=file,
        tags=MappingProxyType(dict(result.tags)),
        duration=result.duration,
        embedded_artwork=result.artwork_bytes,
        artwork_suffix=_suffix_for(result.artwork_mime),
        error=result.error,
        tag_hash=tag_hash(result.tags),
    )


def tag_hash(tags: Mapping[str, str]) -> str:
    """Digest of the tag block, in the order the key sorts rather than the file.

    The fourth part of ADR-010's incremental key. Sorting makes it a property of
    the metadata rather than of the container's write order, which is what lets a
    tag edit invalidate a cache entry and a re-save with identical tags not.
    """

    digest = hashlib.sha256()
    for name in sorted(tags):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(tags[name].encode())
        digest.update(b"\0")
    return digest.hexdigest()[:32]


def _duration(audio: object) -> timedelta:
    info = getattr(audio, "info", None)
    seconds = getattr(info, "length", None)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return timedelta(0)
    return timedelta(seconds=float(seconds)) if seconds > 0 else timedelta(0)


def _translate(audio: object, format: AudioFormat, *, include_untagged: bool) -> Mapping[str, str]:  # noqa: A002
    """Read a container's tags into canonical names.

    Unknown tags are dropped unless asked for, and a tag whose value cannot be
    rendered as text is skipped rather than guessed at. A file with a tag block
    of nothing but `MOOD: sombre` therefore produces no canonical tags at all,
    which sends it to ADR-010's path and filename levels — the honest answer, and
    the one the report will show the reason for.
    """

    tags = getattr(audio, "tags", None)
    if not tags:
        return MappingProxyType({})
    raw = _flatten(tags)
    table = _TABLES[format]
    found: dict[str, str] = {}
    for canonical, names in table.items():
        for name in names:
            value = raw.get(name)
            if value:
                found.setdefault(canonical, value)
                break
    if include_untagged:
        used = {name for names in table.values() for name in names}
        found.update({f"untagged:{name}": value for name, value in raw.items() if name not in used})
    return MappingProxyType(found)


def _flatten(tags: object) -> dict[str, str]:
    """Reduce a Mutagen tag mapping to `{name: text}`.

    ID3's `TXXX` frames are keyed by their frame type alone, so the description
    is appended as `TXXX:MusicBrainz Track Id` — the name that identifies the
    value, and the one `_ID3_NAMES` looks up.
    """

    flat: dict[str, str] = {}
    for key, frame in cast("Mapping[str, object]", tags).items():
        name = str(key)
        text = _text(getattr(frame, "text", frame))
        if text is None:
            continue
        flat[name] = text
        description = getattr(frame, "desc", None)
        if name == "TXXX" and isinstance(description, str) and description.strip():
            flat[f"TXXX:{description.strip()}"] = text
    return flat


def _text(value: object) -> str | None:
    """One textual value for any shape a tag arrives in.

    A value is a list of strings, a list of bytes, an ID3 frame whose `.text` is a
    list, an MP4 `trkn` tuple of `(track, total)` — or bytes that turn out to be a
    picture, which is why `bytes` is decoded leniently and can still come back
    None. Joining multi-valued tags with `; ` keeps them readable and keeps the
    information; SAPRS 6.5 wants originals available, not originals as a
    data-structure puzzle.

    The last branch is the one that was missing: `mutagen.id3`'s `TDRC` holds an
    `ID3TimeStamp`, which prints as `"1999"`, compares as one and is *not* a `str`
    subclass. A reader that only understood `str` would silently drop every date in an
    ID3-tagged library, and the report would say the file had none.
    """

    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, bytes):
        decoded = value.decode("utf-8", "ignore").strip()
        return decoded or None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        parts = [part for part in (_text(item) for item in value) if part]
        return "; ".join(parts) if parts else None
    return _rendered(value)


def _rendered(value: object) -> str | None:
    """A value that is neither text nor a container, if it renders as text at all.

    Anything whose `str()` is an object representation — `<module 'x'>` — is a value
    this reader does not understand, and saying None keeps that visible in the report
    instead of storing a Python repr in a music library.
    """

    if value is None or callable(value):
        return None
    text = str(value).strip()
    if not text or (text.startswith("<") and text.endswith(">")):
        return None
    return text


def _artwork(audio: object) -> tuple[bytes | None, str]:
    """The first embedded image and its MIME type.

    The first, because that is every tag editor's album cover and the ones after
    it are band shots, icons or booklets that would be wrong on a track row.
    """

    pictures = getattr(audio, "pictures", None)
    if isinstance(pictures, Sequence) and len(pictures):
        data = getattr(pictures[0], "data", None)
        if isinstance(data, bytes) and data:
            return data, str(getattr(pictures[0], "mime", "") or "image/jpeg")

    tags = getattr(audio, "tags", None)
    if not tags:
        return None, "image/jpeg"
    try:
        frames = cast("Any", tags).getall("APIC")
    except Exception:
        frames = []
    for frame in frames:
        data = getattr(frame, "data", None)
        if isinstance(data, bytes) and data:
            return data, str(getattr(frame, "mime", "") or "image/jpeg")
    covers = cast("Any", tags).get("covr") if hasattr(tags, "get") else None
    if isinstance(covers, (list, tuple)) and covers and isinstance(covers[0], bytes):
        mime = "image/png" if covers[0][:4] == b"\x89PNG" else "image/jpeg"
        return covers[0], mime
    return None, "image/jpeg"


def _suffix_for(mime: str) -> str:
    return ".png" if mime.lower().endswith("png") else ".jpg"
