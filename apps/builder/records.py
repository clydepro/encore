"""Value objects passed between Builder stages (SAPRS 6.2, ADR-010).

ADR-010 fixes the Builder's shape: "stages are functions over immutable value
objects; stage boundaries are the only checkpoint positions". This module is what
that sentence is about — the types a stage takes in and hands on, and nothing
else. No stage here opens a file, runs a query or calls MusicBrainz.

Immutability is load-bearing rather than tidy. A build of 15,000 files takes long
enough that a operator will want to ask "what did normalization decide before
enrichment changed it?", and the answer has to be the object, not a log of what
some earlier dict used to contain. Each stage therefore returns new values and
carries `FieldProvenance` alongside, which is also how ADR-010's per-field
`repaired` record gets written.

`Track` is the important one: it is a file's metadata *resolved* by ADR-010's
four-level precedence, before any deduplication or grouping decision. The artist,
album and song rows in `library.db` are derived from it later, in construction.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

from encore.domain.media import AudioFormat
from encore.repositories.contract import MetadataField

__all__ = [
    "ArtworkAsset",
    "ArtworkOutcome",
    "Catalogue",
    "DiscoveredFile",
    "ExtractedFile",
    "FieldProvenance",
    "SkipReason",
    "SkippedFile",
    "Track",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class DiscoveredFile:
    """One file discovery accepted (SAPRS 6.3).

    Attributes:
        path: Absolute path as found. Recorded, never rewritten: the Builder may
            not modify, move or delete user music (SAPRS 6.8).
        format: The container `AudioFormat.for_path` recognised. Discovery only
            produces accepted files, so this is never None.
        size_bytes / mtime_ns: Two of the four parts of the incremental key
            (ADR-010). `mtime_ns` rather than `mtime` because a copy that
            preserved whole seconds still changed.
    """

    path: Path
    format: AudioFormat
    size_bytes: int
    mtime_ns: int

    @property
    def cache_key(self) -> str:
        """The part of the incremental key this file supplies (SAPRS 6.9)."""

        return f"{self.path}|{self.size_bytes}|{self.mtime_ns}"


@dataclass(frozen=True, slots=True, kw_only=True)
class FieldProvenance:
    """Where one field's value came from, as ADR-010 requires it be recorded.

    Attributes:
        field: One of `contract.MetadataField`.
        original: The value the source carried, before normalization. None when
            the source said nothing at all.
        effective: The value the Builder will store.
        source: One of `contract.MetadataSource` — the precedence level that
            produced it.
        repaired: True when this value replaced something else: an empty or
            failing field filled from MusicBrainz, a path hint used because no
            tag existed, or a normalization rule that changed the text. The
            report's `metadata repaired` count (SAPRS 6.12) is a count of these.
    """

    field: str
    original: str | None
    effective: str | None
    source: str
    repaired: bool = False

    def __post_init__(self) -> None:
        if self.field not in _METADATA_FIELDS:
            raise ValueError(f"unknown metadata field {self.field!r} in provenance")


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractedFile:
    """Extraction's output: the file, and what its tags literally said (6.4).

    Values are stored as the file wrote them, with no cleanup at all — SAPRS 6.5
    puts normalization in its own stage, and 6.4 makes tags the *primary* source,
    which cannot be audited later if the read step already edited them.

    Attributes:
        file: What was read.
        tags: Tag values keyed by the names in `encore.domain.CANONICAL_TAGS`,
            plus anything else the file carried. Absent tags are absent keys,
            never empty strings: those are different facts (6.4).
        duration: Decoded length from the container, not a tag.
        embedded_artwork: Raw cover bytes when the file carries them, else None.
            98% of M4A and 0% of FLAC in the measured corpus, so None is normal
            (SAPRS 6.7, ADR-010).
        artwork_suffix: Container-appropriate extension for those bytes.
        error: Why extraction failed, if it did. A failure is a skip, never an
            exception out of the pipeline.
        tag_hash: Digest of the tag block, the fourth part of the incremental key.
    """

    file: DiscoveredFile
    tags: Mapping[str, str] = field(default_factory=dict)
    duration: timedelta = timedelta(0)
    embedded_artwork: bytes | None = None
    artwork_suffix: str = ".jpg"
    error: str | None = None
    tag_hash: str = ""

    @property
    def usable(self) -> bool:
        """True when there is something for normalization to work on."""

        return self.error is None and bool(self.tags)

    @property
    def cache_key(self) -> str:
        """`(path, size, mtime_ns, embedded-tag hash)` — ADR-010's key verbatim."""

        return f"{self.file.cache_key}|{self.tag_hash}"


@dataclass(frozen=True, slots=True, kw_only=True)
class Track:
    """One file's metadata after normalization and precedence resolution (6.5).

    Attributes:
        extracted: The evidence this conclusion rests on, kept whole so the
            original values stay available (SAPRS 6.5).
        artist: The *grouping* artist — album artist where present, else primary
            artist (ADR-010). This is what an album is filed under.
        album_artist / track_artist: The two artists a file may distinguish, kept
            separate because SAPRS 4.4 says a track's artist and a release's
            artist legitimately disagree on compilations.
        album: Release title, or None. `[Unknown Album]` is a decision
            construction makes, not a fact extraction found.
        title: Track title. Non-empty in a catalogable track.
        sort_* / normalized_*: The derived fields `encore.domain` carries.
        track_number / disc_number: Canonicalised positions (6.5).
        provenance: One entry per field the track has, keyed by field name.
    """

    extracted: ExtractedFile
    artist: str = ""
    album_artist: str | None = None
    track_artist: str | None = None
    album: str | None = None
    title: str = ""
    sort_artist: str = ""
    sort_album: str = ""
    sort_title: str = ""
    normalized_artist: str = ""
    normalized_album: str = ""
    normalized_title: str = ""
    track_number: int | None = None
    disc_number: int | None = None
    genre: str | None = None
    date: str | None = None
    musicbrainz_artist_id: str | None = None
    musicbrainz_release_id: str | None = None
    musicbrainz_recording_id: str | None = None
    provenance: Mapping[str, FieldProvenance] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))

    @property
    def path(self) -> Path:
        return self.extracted.file.path

    @property
    def duration(self) -> timedelta:
        return self.extracted.duration

    @property
    def missing_fields(self) -> tuple[str, ...]:
        """Which required fields survived none of ADR-010's four levels.

        SAPRS 6.5 makes artist and title required; the two are named together so
        the report can say "no artist and no title" rather than two lines about
        one file.
        """

        missing: list[str] = []
        if not self.artist.strip():
            missing.append(MetadataField.ARTIST)
        if not self.title.strip():
            missing.append(MetadataField.TITLE)
        return tuple(missing)

    @property
    def catalogable(self) -> bool:
        """True when this track can become a row (ADR-010: reported, not fatal)."""

        return not self.missing_fields

    def with_field(self, name: str, value: str, source: str, *, repaired: bool = True) -> Track:
        """Return a copy with one field replaced and its provenance updated.

        The only way any later stage changes a value. Enrichment uses it, and
        nothing else may, so "why is this song under this artist?" always has a
        chain of these entries behind the answer.

        Args:
            name: A `contract.MetadataField`, not an attribute name — most are
                the same word, and the two that are not (`track`, `disc`) are the
                reason the mapping is written down rather than trusted.
            value: The value to store.
            source: The precedence level it came from.
            repaired: True when this replaced something. Carried forward, so a
                value repaired and then normalized is still marked repaired.
        """

        existing = self.provenance.get(name)
        original = existing.original if existing is not None else value
        record = FieldProvenance(
            field=name,
            original=original,
            effective=value,
            source=source,
            repaired=repaired or (existing.repaired if existing else False),
        )
        updated = dict(self.provenance)
        updated[name] = record
        # `replace` cannot be given a field name at runtime in a way mypy can check, so
        # the mapping below is the one place that knows which attribute a metadata field
        # writes to; `_ATTRIBUTES` is declared `Final` and complete, and the test that
        # walks every `MetadataField` is what keeps it so.
        attributes: dict[str, Any] = {_ATTRIBUTES[name]: value}
        return replace(self, **attributes, provenance=updated)


class SkipReason:
    """Why a file is in the library's skip list rather than in `songs`.

    Class rather than enum so the report can print the string verbatim and
    `tests/` can cite it; the set is small and each value names an operator
    action, which is the point of the list.
    """

    UNSUPPORTED_FORMAT = "unsupported-format"
    UNREADABLE = "unreadable"
    NO_ARTIST = "no-usable-artist"
    NO_TITLE = "no-usable-title"
    NO_DURATION = "undecodable-duration"
    ALREADY_INDEXED = "duplicate-of-indexed-file"


@dataclass(frozen=True, slots=True, kw_only=True)
class SkippedFile:
    """A file the Builder did not catalogue (ADR-010: a reported outcome).

    Attributes:
        path: Where the file is. Printed in the detail list, never in the
            aggregate count.
        reason: One of `SkipReason`.
        detail: Format name or tag problem, for the one line an operator reads.
    """

    path: Path
    reason: str
    detail: str = ""

    @property
    def aggregate_key(self) -> str:
        """How this skip is counted.

        ADR-010: unsupported containers are reported as `188 .m4p (DRM)` rather
        than 188 lines, because a 263-line warning block trains the operator to
        ignore the report. Everything else is specific enough to itemise.
        """

        if self.reason == SkipReason.UNSUPPORTED_FORMAT:
            return f"{self.reason}:{self.detail}"
        return self.reason


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtworkAsset:
    """One image written to the artwork cache (SAPRS 5.6, 6.7).

    `relative_path` is relative to `paths.artwork_dir`, which is what
    `encore.domain.Artwork` insists on and what makes a library copy portable:
    the cache moves, the references do not.
    """

    kind: str
    relative_path: Path
    width: int
    height: int
    sha256: str
    source_path: Path | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtworkOutcome:
    """What the artwork stage produced.

    Attributes:
        assets: Distinct images written this run, deduplicated by content hash —
            the same cover on 40 files of one album is one file.
        by_file: Which image each media file carried, so construction can
            associate the same asset with a song, its album and its artist.
        generated: Count of cache writes. Distinct from `len(assets)`: a rebuilt
            run finds the bytes already cached and writes nothing, which is a
            normal outcome rather than a failure (6.7).
        empty: Files that had no embedded artwork at all. Measured at 77% of MP3
            and 100% of FLAC in the corpus.
        failed: Files that carried bytes which would not decode as an image. Kept
            apart from `empty` because the two need different answers: one is a
            library without covers, the other is a library with damaged ones.
    """

    assets: tuple[ArtworkAsset, ...] = ()
    by_file: Mapping[Path, ArtworkAsset] = field(default_factory=dict)
    generated: int = 0
    empty: int = 0
    failed: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class Catalogue:
    """The Builder's conclusions about one music directory.

    Attributes:
        tracks: Catalogable tracks, in discovery order.
        skips: Everything that was found and not catalogued.
        found: Count of files `stat()`ed, including unsupported ones — the
            number ADR-010 requires beside the final song count, because a
            skipped song is otherwise indistinguishable from one that was never
            there.
    """

    tracks: tuple[Track, ...] = ()
    skips: tuple[SkippedFile, ...] = ()
    found: int = 0
    artwork: ArtworkOutcome = field(default_factory=ArtworkOutcome)
    duplicates: tuple[tuple[Path, ...], ...] = ()
    enriched: int = 0

    @property
    def song_count(self) -> int:
        return len(self.tracks)

    def skipped_with(self, reason: str) -> tuple[SkippedFile, ...]:
        return tuple(skip for skip in self.skips if skip.reason == reason)


def tracks(values: Iterable[Track]) -> tuple[Track, ...]:
    """Freeze an iterable of tracks. Stages use this to end a loop cleanly."""

    return tuple(values)


#: `MetadataField` name → `Track` attribute. Names diverge where the field is a
#: position and the attribute is a number, and where the album artist is stored
#: as the grouping artist as well.
_ATTRIBUTES: Mapping[str, str] = MappingProxyType(
    {
        MetadataField.ARTIST: "artist",
        MetadataField.ALBUM: "album",
        MetadataField.ALBUM_ARTIST: "album_artist",
        MetadataField.TITLE: "title",
        MetadataField.TRACK: "track_number",
        MetadataField.DISC: "disc_number",
        MetadataField.DATE: "date",
        MetadataField.GENRE: "genre",
    }
)

_METADATA_FIELDS: frozenset[str] = frozenset(
    {
        MetadataField.ARTIST,
        MetadataField.ALBUM,
        MetadataField.ALBUM_ARTIST,
        MetadataField.TITLE,
        MetadataField.TRACK,
        MetadataField.DISC,
        MetadataField.DATE,
        MetadataField.GENRE,
    }
)
