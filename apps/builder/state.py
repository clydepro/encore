"""What a build can reuse, and where it can resume from (SAPRS 6.9; ADR-010).

Two mechanisms live here because they answer the same question — *what did we
already do?* — with two different lifetimes.

**The previous published library** is the durable answer. ADR-010 fixes its key as
`(path, size, mtime_ns, embedded-tag hash)` plus the normalization-rule version. The
first three say "this file has not been touched"; the tag hash says "…and what it
says has not changed", which is the difference between a copy that preserved
timestamps and a retag by a tool that did. The rule version is why a normalization
fix is a full rebuild: a conclusion drawn by an old rule must not survive as a fact
about the file.

**Stage checkpoints** are the short answer. ADR-010 makes stage boundaries the only
positions a build can resume from, so a run that dies at minute nine of a scan over
a WiFi share should not start the scan again. Each checkpoint is one JSON file under
`paths.temp_dir`, keyed by path and validated against size and `mtime_ns`.

What is deliberately not cached is the tag read itself: it is the one stage whose
input is the file's own words, and skipping it would let a retagged file look
unchanged forever. At roughly 3 ms a file locally it is also the cheapest thing in
the build by an order of magnitude. The expensive parts are MusicBrainz lookups and
artwork re-encoding, and both of those are reused.
"""
# ruff: noqa: S608 — the cache reads name columns from `schema` and `contract`; the
# values are bound (ADR-010's incremental key is data, not SQL).

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from apps.builder.enrichment import PreviousEnrichment
from apps.builder.extraction import ProbeResult, tag_hash
from apps.builder.normalization import NORMALIZATION_RULES_VERSION
from apps.builder.records import ArtworkAsset, DiscoveredFile, ExtractedFile
from encore.repositories.contract import LIBRARY_SCHEMA_VERSION, Table

__all__ = ["Checkpoints", "PreviousLibrary"]

#: Shape marker for a checkpoint file. Anything else is someone else's JSON, or an
#: older cache whose meaning we would be guessing at.
FORMAT: Final = "encore-build-cache-v1"

#: The canonical tag names a checkpoint may hold, and the row column each came from
#: when the cache is a previous library rather than a previous run.
_TAG_COLUMNS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "title": "original_title",
        "artist": "original_artist",
        "album": "original_album",
        "date": "date",
        "genre": "genre",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class Reusable:
    """Everything an earlier build concluded about one file.

    Attributes:
        cache_key: The stored `(path, size, mtime_ns, tag hash)` identity. Empty
            when the row could not say, which reads as "not reusable".
        probe: The tags and duration the file reported, ready to hand back instead of
            opening it again.
        enrichment: What MusicBrainz answered last time, so an incremental build
            with the network down is not degraded to un-enriched data (SAPRS 6.6).
        artwork: The cache entry the file's embedded image produced, so an unchanged
            build writes zero images *and keeps the references it already made*. Read
            by `apps.builder.artwork.generate`; #52 is the build that published zero
            artwork because nothing read this field.
    """

    cache_key: str = ""
    probe: ProbeResult | None = None
    enrichment: PreviousEnrichment | None = None
    artwork: ArtworkAsset | None = None

    def matches(self, file: DiscoveredFile, *, digest: str | None = None) -> bool:
        """True when this record describes the file as it is now.

        ADR-010's key is the whole of the comparison, including the rule version,
        which is why a rules bump reuses nothing and a `touch` reuses nothing it
        should not.

        Args:
            file: What discovery just saw — the path, and the two numbers a `stat()`
                answers.
            digest: The tag hash of the file *as it is now*, for a caller that has
                read it. None means "not read", which is the incremental case: the
                hash is the one part of the key that cannot be had without the read
                the key exists to avoid (`_extract`'s docstring says so, and
                `tag_hash` is not stored anywhere a `stat()` can reach). With None
                the comparison is path, size, `mtime_ns` and rule version — every
                part a directory walk can answer, and the four that make a retagged
                file a changed file, because rewriting a tag block moves the size,
                the `mtime`, or both.

        This is the reuse test itself, not a helper for it. #52 found `Reusable.artwork`
        loaded and never read; the same shape sat one line away in this method, which
        `PreviousLibrary` called from no path and `_extract` did not call at all — so
        an incremental build reused a file by *name*, and a library whose tags had
        been corrected since the last build published the old ones (issue #52's "the
        key rule", found while testing its artwork half).
        """

        parts = _split_key(self.cache_key)
        if parts is None:
            return False
        stored_path, size, mtime, tag_digest, rules = parts
        return (
            stored_path == str(file.path)
            and size == str(file.size_bytes)
            and mtime == str(file.mtime_ns)
            and rules == str(NORMALIZATION_RULES_VERSION)
            and (digest is None or tag_digest == digest)
        )


class PreviousLibrary:
    """The last published `library.db`, consulted as a cache rather than as data.

    Reading the artifact instead of keeping a separate index means there is one
    thing to be wrong. A `build-state.json` beside the database would be a second
    account of what was built, and the first disagreement between the two would be
    unresolvable — ADR-010's argument against a mirrored schema, applied to a smaller
    object.

    A missing or unreadable previous library is `empty()`, not an error. The first
    build on a machine has no history, and every later one should behave the same way
    about a history it cannot read: rebuild from scratch, publish, and say so in the
    report.
    """

    def __init__(
        self, entries: Mapping[str, Reusable], *, usable: bool = True, artwork_rows: int = 0
    ) -> None:
        self._entries = entries
        self._usable = usable
        self._artwork_rows = artwork_rows

    @classmethod
    def empty(cls) -> PreviousLibrary:
        return cls({}, usable=False)

    @classmethod
    def load(cls, path: Path) -> PreviousLibrary:
        """Read what the previous build decided, or report that there is none."""

        if not path.is_file():
            return cls.empty()
        try:
            connection = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)
        except sqlite3.Error:
            return cls.empty()
        try:
            return cls._read(connection)
        except sqlite3.Error:
            return cls.empty()
        finally:
            connection.close()

    @classmethod
    def _read(cls, connection: sqlite3.Connection) -> PreviousLibrary:
        from apps.builder import schema

        stamp = schema.meta_values(connection)
        if stamp.get(schema.META_SCHEMA_VERSION) != str(LIBRARY_SCHEMA_VERSION):
            # A library from another schema revision is not a cache with old
            # columns, it is a different set of conclusions. Reuse nothing, which is
            # also the answer for a database written before ADR-010 existed.
            return cls.empty()
        entries: dict[str, Reusable] = {}
        # One statement, and the artwork arrives with the file rather than in a second pass.
        # The earlier shape loaded `embedded_artwork_sha`, then "filled in" the rest from the
        # `artwork` table, leaving a placeholder (`relative_path=Path("pending")`, no width)
        # whenever the digest named no row. Nothing read the field, so nothing noticed (#52);
        # once something does, a half-built asset is a reference to a file called `pending`.
        # A `LEFT JOIN` cannot produce that state: either the row is there in full or the
        # field is None, which is what "this file has no cached picture" means.
        for row in connection.execute(
            f"SELECT f.path, f.size_bytes, f.mtime_ns, f.tag_fingerprint, f.rules_version,"
            f" f.duration_seconds, a.kind, a.relative_path, a.width, a.height, a.sha256,"
            f" a.source_path"
            f" FROM {Table.MUSIC_FILES} AS f"
            f" LEFT JOIN {Table.ARTWORK} AS a ON a.sha256 = f.embedded_artwork_sha"
        ):
            path, size, mtime, fingerprint, rules, seconds = row[:6]
            asset = _asset(row[6:])
            entries[str(path)] = Reusable(
                cache_key=_key(str(path), int(size), int(mtime), str(fingerprint), int(rules)),
                probe=ProbeResult(
                    tags=MappingProxyType({}), duration=timedelta(seconds=float(seconds or 0.0))
                ),
                artwork=asset,
            )
        _fill_tags(connection, entries)
        _fill_enrichment(connection, entries)
        return cls(
            MappingProxyType(entries),
            artwork_rows=int(
                connection.execute(f"SELECT COUNT(*) FROM {Table.ARTWORK}").fetchone()[0]
            ),
        )

    @property
    def usable(self) -> bool:
        """False when there was no previous library worth consulting."""

        return self._usable

    @property
    def artwork_rows(self) -> int:
        """How many images the previous artifact held.

        Kept for the report, because it is the only number that can tell an operator that a
        build *lost* artwork rather than never having any (#52). A library with no covers is
        normal (SAPRS 6.7); one that had 222 and now has none is not.
        """

        return self._artwork_rows

    def __len__(self) -> int:
        return len(self._entries)

    def for_path(self, path: Path) -> Reusable | None:
        return self._entries.get(str(path))


class Checkpoints:
    """Resumable stage boundaries inside `paths.temp_dir` (SAPRS 6.9).

    Args:
        directory: The scratch space; created on demand.
        enabled: Off for a `--full` run, where reading the checkpoint would be the
            opposite of what was asked for.
    """

    def __init__(self, directory: Path, *, enabled: bool = True) -> None:
        self._directory = directory
        self._enabled = enabled
        self._files: dict[str, dict[str, Any]] = {}
        if enabled:
            self._load()

    @property
    def path(self) -> Path:
        return self._directory / "extraction.json"

    @property
    def enabled(self) -> bool:
        return self._enabled

    def saved(self) -> int:
        return len(self._files)

    def probe_for(self, file: DiscoveredFile) -> ProbeResult | None:
        """A recorded probe result, if it still describes this file.

        Size and `mtime_ns` are re-checked against the disk: a checkpoint is a cache
        of work, not a claim about the filesystem, and a file that changed since the
        last run has to be re-read even though a record for it exists.
        """

        record = self._record_for(file)
        return None if record is None else _probe_from(record)

    def artwork_for(self, file: DiscoveredFile) -> ArtworkAsset | None:
        """The image this file's cover produced in the interrupted run, if it still fits.

        #52's second half. The checkpoint answers "what did the file say?" out of a JSON
        blob, and image bytes are not in it (SAPRS 5.6 forbids BLOBs in the artifact, and
        a checkpoint is not the place to break that) — so what it carries instead is the
        *reference* stage 5 resolved to, which is five fields and addresses a file already
        on disk. Without it a resumed build reuses a probe and loses the cover, which is
        worse than the incremental build #52 describes rather than the same thing.
        """

        record = self._record_for(file)
        if record is None:
            return None
        artwork = record.get("artwork")
        if not isinstance(artwork, dict):
            return None
        return _asset_from(artwork)

    def _record_for(self, file: DiscoveredFile) -> Mapping[str, Any] | None:
        """The checkpoint's answer for this file, after checking it is still this file."""

        if not self._enabled:
            return None
        record = self._files.get(str(file.path))
        if record is None:
            return None
        if int(record.get("size", -1)) != file.size_bytes:
            return None
        if int(record.get("mtime_ns", -1)) != file.mtime_ns:
            return None
        return record

    def record(self, extracted: list[ExtractedFile]) -> None:
        """Store this run's probes, then write them out if that is enabled."""

        for item in extracted:
            self._files[str(item.file.path)] = {
                "format": item.file.format.value,
                "size": item.file.size_bytes,
                "mtime_ns": item.file.mtime_ns,
                "duration": item.duration.total_seconds(),
                "tags": dict(item.tags),
                "tag_hash": item.tag_hash,
                "error": item.error,
            }
        if not self._enabled:
            return
        self._write()

    def record_artwork(self, by_file: Mapping[Path, ArtworkAsset]) -> None:
        """Attach stage 5's references to the probes already recorded (ADR-010's boundary).

        A second write of the same file rather than a second file, because the checkpoint is
        one answer to "what did we already do?" and two answers to that question is how a
        build comes to trust the stale one.
        """

        if not self._enabled:
            return
        for path, asset in by_file.items():
            record = self._files.get(str(path))
            if record is not None:
                record["artwork"] = _asset_dict(asset)
        self._write()

    def clear(self) -> None:
        """Drop the checkpoint file. A published build has nothing left to resume."""

        self._files.clear()
        self.path.unlink(missing_ok=True)

    def _load(self) -> None:
        try:
            payload: Any = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict) or payload.get("format") != FORMAT:
            return
        records = payload.get("files")
        if isinstance(records, dict):
            self._files = {
                str(key): dict(value) for key, value in records.items() if isinstance(value, dict)
            }

    def _write(self) -> None:
        """Write atomically. A checkpoint truncated by a power cut is worse than none."""

        self._directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"format": FORMAT, "files": self._files}, separators=(",", ":"), sort_keys=True
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)


def _key(path: str, size: int, mtime_ns: int, digest: str, rules: int) -> str:
    return f"{path}|{size}|{mtime_ns}|{digest}|{rules}"


#: How many fields `_key` writes, and how many `_split_key` must find to answer at all.
_KEY_FIELDS: Final = 5


def _split_key(key: str) -> tuple[str, str, str, str, str] | None:
    """`_key` backwards.

    Split from the right, because a path is the one field that may contain `|`: with
    `rsplit("|", 4)` the leftmost piece keeps whatever pipes the filename had, and the
    four that follow are unambiguous. Anything that does not split into five pieces is
    not a key this function wrote, and answers None — which reads as "not reusable".
    """

    if not key:
        return None
    parts = key.rsplit("|", _KEY_FIELDS - 1)
    if len(parts) != _KEY_FIELDS:
        return None
    path, size, mtime, digest, rules = parts
    return path, size, mtime, digest, rules


def cache_key(item: ExtractedFile) -> str:
    """The key for a file just read, in the same spelling `Reusable` compares."""

    return _key(
        str(item.file.path),
        item.file.size_bytes,
        item.file.mtime_ns,
        item.tag_hash,
        NORMALIZATION_RULES_VERSION,
    )


def _probe_from(record: Mapping[str, Any]) -> ProbeResult | None:
    tags = record.get("tags")
    if not isinstance(tags, dict):
        return None
    error = record.get("error")
    return ProbeResult(
        tags=MappingProxyType({str(key): str(value) for key, value in tags.items()}),
        duration=timedelta(seconds=float(record.get("duration", 0.0))),
        error=None if error is None else str(error),
    )


def _asset_dict(asset: ArtworkAsset) -> dict[str, Any]:
    """One reference, in the shape the checkpoint file holds it in."""

    return {
        "kind": asset.kind,
        "relative_path": asset.relative_path.as_posix(),
        "width": asset.width,
        "height": asset.height,
        "sha256": asset.sha256,
    }


def _asset_from(record: Mapping[str, Any]) -> ArtworkAsset | None:
    relative = record.get("relative_path")
    digest = record.get("sha256")
    if not isinstance(relative, str) or not isinstance(digest, str) or not digest:
        return None
    return ArtworkAsset(
        kind=str(record.get("kind", "album")),
        relative_path=Path(relative),
        width=int(record.get("width", 0) or 0),
        height=int(record.get("height", 0) or 0),
        sha256=digest,
    )


def _fill_tags(connection: sqlite3.Connection, entries: dict[str, Reusable]) -> None:
    """Give each cached file back the tag values its song row kept."""

    rows = connection.execute(
        f"SELECT f.path, s.original_title, s.original_artist, s.original_album, s.date, s.genre,"
        f" s.track_number, s.disc_number FROM {Table.MUSIC_FILES} AS f"
        f" JOIN {Table.SONGS} AS s ON s.music_file_id = f.id"
    ).fetchall()
    for path, *values in rows:
        entry = entries.get(str(path))
        if entry is None or entry.probe is None:
            continue
        tags = dict(entry.probe.tags)
        tags.update(_canonical(values))
        # `replace`, not a fresh `Reusable(...)`: a construction that lists four fields is a
        # construction that quietly drops the fifth, and #52 is what that looks like.
        entries[str(path)] = replace(
            entry, probe=ProbeResult(tags=MappingProxyType(tags), duration=entry.probe.duration)
        )


def _canonical(values: list[Any]) -> Mapping[str, str]:
    names = ("title", "artist", "album", "date", "genre", "track", "disc")
    return MappingProxyType(
        {
            name: str(value)
            for name, value in zip(names, values, strict=True)
            if value is not None and str(value) != ""
        }
    )


def _fill_enrichment(connection: sqlite3.Connection, entries: dict[str, Reusable]) -> None:
    """Recover the MusicBrainz answers, which are provenance rows and nothing else."""

    rows = connection.execute(
        f"SELECT f.path, m.field, m.effective FROM {Table.METADATA} AS m"
        f" JOIN {Table.SONGS} AS s ON s.id = m.song_id"
        f" JOIN {Table.MUSIC_FILES} AS f ON f.id = s.music_file_id"
        " WHERE m.source = 'musicbrainz'"
    ).fetchall()
    for path, field, effective in rows:
        entry = entries.get(str(path))
        if entry is None or entry.probe is None or not effective:
            continue
        attribute = {
            "artist": "artist",
            "album_artist": "artist",
            "album": "album",
            "date": "date",
        }.get(str(field))
        if attribute is None:
            continue
        entries[str(path)] = replace(
            entry,
            enrichment=_with(
                entry.enrichment or PreviousEnrichment(), **{attribute: str(effective)}
            ),
        )


def _with(earlier: PreviousEnrichment, **values: str) -> PreviousEnrichment:
    return replace(earlier, **values)


def _asset(row: Sequence[Any]) -> ArtworkAsset | None:
    """One `artwork` row, as the stage that would have written it hands it back.

    None is the `LEFT JOIN`'s answer for a file whose stored digest names no row, which is
    the honest state: no cached picture. The alternative was a placeholder asset with a
    path of `pending` in it, and the only reason nobody noticed is that nothing read the
    field (#52).
    """

    kind, relative, width, height, digest, source = row
    if relative is None:
        return None
    return ArtworkAsset(
        kind=str(kind or "album"),
        relative_path=Path(str(relative)),
        width=int(width or 0),
        height=int(height or 0),
        sha256=str(digest or ""),
        source_path=None if source is None else Path(str(source)),
    )


def stored_tag_hash(tags: Mapping[str, str]) -> str:
    """`tag_hash` under the name the previous-build path uses. Re-exported for clarity."""

    return tag_hash(tags)
