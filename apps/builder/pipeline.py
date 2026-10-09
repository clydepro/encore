"""The Builder's pipeline: ADR-010's stages, in order, in one call (SAPRS 6.2, 6.11).

ADR-010 fixes the shape — "one module per stage, stages as functions over immutable
value objects, stage boundaries as the only checkpoint positions" — and this module is
the smallest honest implementation of that sentence. Nothing here decides anything about
music; what it owns is ordering, the scratch file, and what happens when a stage reports
a problem instead of a result.

Two properties are written down because they are the two a later edit can silently
break:

* **The published library is never the one that failed.** Validation runs before the
  rename and a fatal finding means the rename does not happen, so the artifact an
  operator already trusts is still the artifact the Server opens (SAPRS 5.8, 6.11).
* **A stage that cannot finish does not abort the run.** An unreadable file, a
  MusicBrainz outage, a corrupt cover image and a track with no artist are outcomes
  reported in the ledger rather than exceptions. The two things that do stop a build
  are a music directory that cannot be scanned and a database that cannot be written,
  because in both cases there is nothing honest left to publish.

The scratch file is closed and removed on every exit path. A stale
`library.building.db` is the file a later run would overwrite and the file an operator
would find and then not trust.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from apps.builder import construction, schema, validation
from apps.builder.artwork import ArtworkLimits, ArtworkOutcome
from apps.builder.artwork import generate as generate_artwork
from apps.builder.discovery import Discovery, discover
from apps.builder.duplicates import DuplicateReport
from apps.builder.duplicates import analyse as analyse_duplicates
from apps.builder.enrichment import PreviousEnrichment, enrich
from apps.builder.extraction import MediaProbe, MutagenProbe, ProbeResult, extract
from apps.builder.musicbrainz import MusicBrainzClient
from apps.builder.normalization import NORMALIZATION_RULES_VERSION
from apps.builder.precedence import ArtistTrees, normalize_track, tree_map
from apps.builder.publication import Published, publish, stage
from apps.builder.records import (
    ExtractedFile,
    SkippedFile,
    SkipReason,
    Track,
)
from apps.builder.report import BuildReport, SkipLedger
from apps.builder.search_index import SearchIndex
from apps.builder.search_index import optimize as optimize_search
from apps.builder.state import Checkpoints, PreviousLibrary
from encore.events.bus import EventBus
from encore.events.library import BuildCompleted
from encore.repositories.contract import MetadataField
from encore.utilities.clock import utc_now

__all__ = ["BUILDER_VERSION", "BuildOptions", "run"]

#: What the report and `library_meta` say produced this artifact. A build that cannot
#: answer that question cannot be compared with the one before it.
BUILDER_VERSION: Final = "0.1.0"


@dataclass(frozen=True, slots=True, kw_only=True)
class BuildOptions:
    """Everything the pipeline reads about the outside world.

    Attributes:
        music_dir: `paths.music_dir`, the only filesystem scan either application
            performs (SAPRS 12.2, ADR-010).
        library_db: Where the published artifact goes. Read before the run for what the
            previous build decided, and written only by publication.
        artwork_dir: The image cache the references in `library.db` point into.
        temp_dir: Scratch space for the build file and the checkpoints.
        incremental: Reuse the previous build's work where the cache key matches
            (SAPRS 6.9). Off is also what a normalization-rule bump implies by itself.
        enrich: Ask MusicBrainz to fill gaps. Optional and rate-limited per SAPRS 6.6,
            and off by default: a build that quietly took forty minutes looking for
            3,000 recordings is a build nobody runs twice.
        probe / client: Collaborators, injected. `MutagenProbe` and the disabled client
            are the defaults, so an ordinary run opens no socket (ADR-010).
        publish: False for a dry run — build, validate, write nothing.
        now: The clock, so `built_at` is an argument rather than a surprise.
    """

    music_dir: Path
    library_db: Path
    artwork_dir: Path
    temp_dir: Path
    library_version: str = ""
    incremental: bool = True
    enrich: bool = False
    publish: bool = True
    probe: MediaProbe | None = None
    client: MusicBrainzClient | None = None
    artwork_limits: ArtworkLimits | None = None
    now: Callable[[], datetime] = utc_now

    @property
    def build_file(self) -> Path:
        """The scratch database. Never the destination, never in place (SAPRS 5.8)."""

        return self.temp_dir / "library.building.db"


def run(options: BuildOptions, *, bus: EventBus | None = None) -> BuildReport:
    """Build a library from `options`, publish it if it validated, and report.

    A `BuildReport` comes back in every case, including the ones where nothing was
    published: "there was nothing to do" and "it failed" are different sentences, and
    the caller should not have to guess which one it printed.

    Args:
        options: See `BuildOptions`.
        bus: Publishes `BuildCompleted` here when given. The Builder is a terminating
            offline process with no subscribers (ADR-001), so this exists for a caller
            that embeds the pipeline — but it is then the same vocabulary AIG 8 asks
            for rather than a second one.
    """

    started = time.monotonic()
    options.temp_dir.mkdir(parents=True, exist_ok=True)
    found = discover(options.music_dir)
    if found.scanned == 0:
        return _nothing_found(options, started)

    previous = (
        PreviousLibrary.load(options.library_db) if options.incremental else PreviousLibrary.empty()
    )
    checkpoints = Checkpoints(options.temp_dir, enabled=options.incremental)

    extracted, reused = _extract(found, options, previous=previous, checkpoints=checkpoints)
    checkpoints.record(extracted)
    trees = tree_map(extracted, options.music_dir)
    tracks, skips = _catalogue(
        extracted, options=options, trees=trees, previous=previous, reused=reused
    )
    duplicates = analyse_duplicates(tracks)
    artwork = generate_artwork(tracks, options.artwork_dir, options.artwork_limits)

    connection = _create(options.build_file)
    built: construction.BuiltLibrary | None = None
    indexes = SearchIndex()
    findings = validation.ValidationReport()
    published: Published | None = None
    try:
        built, indexes, findings = _write(connection, tracks, artwork=artwork, options=options)
        published = _publish(options, connection, findings)
    finally:
        with suppress(sqlite3.Error):
            connection.close()
        if published is None:
            options.build_file.unlink(missing_ok=True)

    if published is not None:
        checkpoints.clear()

    report = _assemble(
        options=options,
        found=found,
        extracted=extracted,
        skips=skips,
        built=built,
        indexes=indexes,
        findings=findings,
        duplicates=duplicates,
        artwork=artwork,
        published=published,
        started=started,
        reused=reused,
        previous=previous,
    )
    if bus is not None:
        bus.publish(_event(report))
    return report


# -- stage 2: extraction --------------------------------------------------


def _extract(
    found: Discovery,
    options: BuildOptions,
    *,
    previous: PreviousLibrary,
    checkpoints: Checkpoints,
) -> tuple[list[ExtractedFile], frozenset[str]]:
    """Stage 2, with the two caches that make an incremental run incremental.

    Cheapest first: a checkpoint from a run that was interrupted, then the tags the
    previous published library stored. Both are keyed by `DiscoveredFile.cache_key` —
    path, size, mtime — which is the key a caller can compute *before* opening the
    file, and the reason the tag hash is not part of it: a digest of the tags is only
    available after the read the caching exists to avoid.

    Returns the reused keys as well as the results, because enrichment needs to know
    which files it may take the previous answers for.
    """

    cached: dict[str, ProbeResult] = {}
    for item in found.files:
        record = checkpoints.probe_for(item)
        if record is not None:
            cached[item.cache_key] = record
            continue
        earlier = previous.for_path(item.path)
        if earlier is not None and earlier.probe is not None:
            cached[item.cache_key] = earlier.probe

    reader = options.probe if options.probe is not None else MutagenProbe()
    return extract(found.files, reader, previous=cached), frozenset(cached)


# -- stages 3 and 4: normalization, precedence, enrichment ----------------


def _catalogue(
    extracted: Sequence[ExtractedFile],
    *,
    options: BuildOptions,
    trees: ArtistTrees,
    previous: PreviousLibrary,
    reused: frozenset[str],
) -> tuple[list[Track], list[SkippedFile]]:
    """Stages 3 and 4, then the split between rows and the skip ledger.

    Enrichment runs before anything is dropped, which is the order ADR-010's precedence
    implies: a file whose tags say nothing is exactly the file level 2 exists for, and
    filtering first would mean MusicBrainz was never consulted about the files that
    needed it.
    """

    resolved: list[Track] = []
    skips: list[SkippedFile] = []
    for item in extracted:
        if item.error is not None:
            skips.append(
                SkippedFile(path=item.file.path, reason=SkipReason.UNREADABLE, detail=item.error)
            )
        elif item.duration.total_seconds() <= 0:
            skips.append(
                SkippedFile(
                    path=item.file.path,
                    reason=SkipReason.NO_DURATION,
                    detail=item.file.format.value,
                )
            )
        else:
            resolved.append(normalize_track(item, root=options.music_dir, trees=trees))

    if options.enrich:
        outcome = enrich(
            resolved, _client(options), previous=_earlier(previous, resolved, reused=reused)
        )
        resolved = list(outcome.tracks)

    tracks = [track for track in resolved if track.catalogable]
    skips.extend(_skip_for(track) for track in resolved if not track.catalogable)
    return tracks, skips


def _skip_for(track: Track) -> SkippedFile:
    """Why nothing could be said about this file, in the words the report uses."""

    reason = (
        SkipReason.NO_ARTIST
        if MetadataField.ARTIST in track.missing_fields
        else SkipReason.NO_TITLE
    )
    return SkippedFile(path=track.path, reason=reason, detail=_evidence(track))


def _evidence(track: Track) -> str:
    tags = track.extracted.tags
    return "no tags" if not tags else "tags: " + ",".join(sorted(tags))


def _client(options: BuildOptions) -> MusicBrainzClient:
    """The enrichment client, defaulted only when enrichment was actually asked for.

    `HttpMusicBrainzClient` holds a rate-limit clock and a cached connection, so
    building one for a run that will never use it is a small lie about what the run
    does. The import is local for the same reason: the module reads a configuration
    default at import time.
    """

    if options.client is not None:
        return options.client
    from apps.builder.musicbrainz import HttpMusicBrainzClient

    return HttpMusicBrainzClient()


def _earlier(
    previous: PreviousLibrary, tracks: Sequence[Track], *, reused: frozenset[str]
) -> dict[Path, PreviousEnrichment]:
    """Last build's enrichment, for the files that have not changed.

    Restricted to `reused` deliberately (SAPRS 6.6: enrichment "should not be degraded
    to un-enriched data"). A changed file deserves a fresh lookup; an unchanged one
    carrying a stale answer would be the pipeline lying about having verified it.
    """

    found: dict[Path, PreviousEnrichment] = {}
    for track in tracks:
        if track.extracted.cache_key not in reused:
            continue
        record = previous.for_path(track.path)
        if record is not None and record.enrichment is not None:
            found[track.path] = record.enrichment
    return found


# -- stages 7 to 10: construction, search, validation, publication --------


def _create(database: Path) -> sqlite3.Connection:
    """A fresh build file. Anything already there is from a run that did not finish.

    The journal and synchronous pragmas are switched off *for this file only*: it is
    disposable until it is renamed, so SQLite's durability work is pure cost, and
    `publication.stage()` is where the one durable write happens.
    """

    database.parent.mkdir(parents=True, exist_ok=True)
    for candidate in (database, Path(f"{database}-journal"), Path(f"{database}-wal")):
        with suppress(OSError):
            candidate.unlink()
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode = OFF")
    connection.execute("PRAGMA synchronous = OFF")
    connection.execute("PRAGMA temp_store = MEMORY")
    return connection


def _write(
    connection: sqlite3.Connection,
    tracks: Sequence[Track],
    *,
    artwork: ArtworkOutcome,
    options: BuildOptions,
) -> tuple[construction.BuiltLibrary, SearchIndex, validation.ValidationReport]:
    """Stages 7, 8 and 9 against one connection and in one transaction.

    One connection rather than three because the search structures are derived from rows
    that are not committed yet: a second handle would either see nothing or force a
    commit before validation had approved anything. `stage()` ends the transaction.
    """

    schema.create_schema(connection)
    built = construction.build(
        connection, tracks, artwork=artwork, rules_version=NORMALIZATION_RULES_VERSION
    )
    schema.stamp_meta(
        connection,
        library_version=options.library_version,
        rules_version=NORMALIZATION_RULES_VERSION,
        built_at=options.now().astimezone(UTC).isoformat(),
        song_count=built.songs,
        file_count=built.files,
        builder_version=BUILDER_VERSION,
    )
    indexes = optimize_search(connection)
    findings = validation.validate(
        connection,
        expected_songs=len(tracks),
        artwork_dir=options.artwork_dir,
        music_root=options.music_dir,
    )
    connection.commit()
    return built, indexes, findings


def _publish(
    options: BuildOptions, connection: sqlite3.Connection, findings: validation.ValidationReport
) -> Published | None:
    """Stage 10, gated on stage 9 (SAPRS 5.8, 6.11)."""

    if not options.publish or not findings.valid:
        return None
    stage(connection, options.build_file)
    return publish(options.build_file, options.library_db)


# -- the report -----------------------------------------------------------


def _assemble(
    *,
    options: BuildOptions,
    found: Discovery,
    extracted: Sequence[ExtractedFile],
    skips: Sequence[SkippedFile],
    built: construction.BuiltLibrary | None,
    indexes: SearchIndex,  # noqa: ARG001 - part of the write's outcome, reported below
    findings: validation.ValidationReport,
    duplicates: DuplicateReport,
    artwork: ArtworkOutcome,
    published: Published | None,
    started: float,
    reused: frozenset[str],
    previous: PreviousLibrary,
) -> BuildReport:
    """The ten numbers SAPRS 6.12 asks for, in one value."""

    reference = built if built is not None else construction.BuiltLibrary()
    # The unsupported files become ledger entries here rather than a tally bolted onto
    # the aggregate, and the difference is arithmetic an operator can check: a skip that
    # is only a count makes `scanned = songs + skipped` false, which is the one line in
    # the report that says whether the build lost anything (ADR-010).
    ledger = SkipLedger.of([*skips, *_unsupported(found)])
    return BuildReport(
        discovered=found.supported_count,
        scanned=found.scanned,
        processed=sum(1 for item in extracted if item.error is None),
        skipped=ledger,
        repaired=reference.repaired,
        artwork=_generated(artwork),
        songs=reference.songs,
        artists=reference.artists,
        albums=reference.albums,
        duplicates=duplicates.lines(),
        warnings=tuple(_warnings(found, findings, previous, artwork)),
        errors=tuple(finding.detail for finding in findings.failures),
        validation=tuple((f.name, f.label, f.detail) for f in findings.findings),
        duration_seconds=time.monotonic() - started,
        published_to=None if published is None else published.path,
        library_version=options.library_version,
        incremental=options.incremental and previous.usable,
        reused=len(reused),
        musicbrainz="enabled" if options.enrich else "disabled",
    )


def _event(report: BuildReport) -> BuildCompleted:
    """The report's numbers in the event's vocabulary.

    They are not quite the same vocabulary, and the difference is one word: the report's
    skip total includes the files that were never candidates — the 188 `.m4p` ADR-010
    insists on printing — while every invariant on `BuildCompleted` is arithmetic over
    *discovered* files, so counting a file the scan never accepted as music would break
    it. The ledger knows which entries are which, and this is where the two definitions
    meet.
    """

    never_candidates = sum(
        1 for entry in report.skipped.entries if entry.reason == SkipReason.UNSUPPORTED_FORMAT
    )
    return BuildCompleted(
        files_discovered=report.discovered,
        files_processed=report.processed,
        files_skipped=report.skip_count - never_candidates,
        metadata_repaired=report.repaired,
        artwork_generated=report.artwork,
        warnings=report.warning_count,
        errors=report.error_count,
        song_count=report.songs,
        validated=report.validated,
    )


def _unsupported(found: Discovery) -> list[SkippedFile]:
    """Unsupported containers as one entry each, aggregated by the ledger (ADR-010).

    ADR-010's rule is about the *printed* report — 188 `.m4p` files are one line, not 188
    — and `SkipLedger.lines()` is what implements that. Keeping the paths here is what
    lets the JSON detail file still name them, and it is what makes the count honest.
    """

    return [
        SkippedFile(path=path, reason=SkipReason.UNSUPPORTED_FORMAT, detail=path.suffix.lower())
        for path in found.unsupported_files
    ]


def _generated(artwork: ArtworkOutcome) -> int:
    return artwork.generated


def _empty(artwork: ArtworkOutcome) -> int:
    return artwork.empty


def _warnings(
    found: Discovery,
    findings: validation.ValidationReport,
    previous: PreviousLibrary,
    artwork: ArtworkOutcome,
) -> list[str]:
    notes = [finding.detail or finding.name for finding in findings.findings if not finding.passed]
    if found.unreadable:
        notes.append(f"{len(found.unreadable)} entries could not be read")
    if _empty(artwork):
        notes.append(f"{_empty(artwork)} files carried no embedded artwork (normal, SAPRS 6.7)")
    if not previous.usable:
        notes.append("no previous library to reuse from; full build")
    return notes


def _nothing_found(options: BuildOptions, started: float) -> BuildReport:
    """A scan that found nothing is a report, not a traceback (SAPRS 6.8, 12.5)."""

    return BuildReport(
        scanned=0,
        errors=(f"nothing to build: no supported audio files under {options.music_dir}",),
        warnings=("check paths.music_dir, and that the share is mounted",),
        duration_seconds=time.monotonic() - started,
    )


def _limits(options: BuildOptions, limits: ArtworkLimits) -> BuildOptions:
    """Attach an artwork cap without mutating the caller's options."""

    return replace(options, artwork_limits=limits)
