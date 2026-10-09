"""Stage 4 — enrichment (SAPRS 6.6; ADR-010 level 2).

MusicBrainz fills a gap or corrects a value that normalization rejected. It does
not overrule a tag that answered: SAPRS 6.4 makes embedded tags primary, and
ADR-010's measurement of this corpus (title and artist 99%, album 97%) says these
tags are usually better than a blind match.

Three properties of 6.6 are implemented rather than discussed:

* **Optional.** An unavailable client changes a count in the report and nothing
  else. The pipeline never waits on the network to decide whether a track exists.
* **Outage-tolerant.** A raised `MusicBrainzUnavailableError` ends the enrichment stage
  for the run, not the run, and every remaining track keeps what its tags said.
* **Non-blocking where practical.** Only files with a gap are looked up, and a
  file already carrying a MusicBrainz identifier is a direct lookup rather than a
  search — a search that guesses wrong files a whole album under the wrong artist
  and nothing downstream can tell.

The part that is easy to leave out: when the service cannot be reached and a file
*does* have a gap, the previous build's enrichment is reused (6.6's "using tags
plus the previous build's enrichment"). Rebuilding an unchanged file as
un-enriched data because the network was down on a Tuesday would let the library
quietly get worse over time, which is the opposite of what an incremental build is
for.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Final

from apps.builder.musicbrainz import MusicBrainzClient, MusicBrainzUnavailableError, Recording
from apps.builder.normalization import fold, is_placeholder
from apps.builder.records import Track
from encore.repositories.contract import MetadataField, MetadataSource

__all__ = ["EnrichmentOutcome", "PreviousEnrichment", "enrich"]

#: Which field a MusicBrainz value may answer. Title is not in the list: a
#: recording's title is the release's track list, and when a file has no title tag
#: the filename (ADR-010 level 4) is closer evidence about *this* file than a
#: database of recordings is.
_ANSWERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        MetadataField.ARTIST: "artist",
        MetadataField.ALBUM: "album",
        MetadataField.ALBUM_ARTIST: "artist",
        MetadataField.DATE: "year",
    }
)

#: The same for the previous build's record, which stores values rather than a
#: document.
_REUSED: Final[Mapping[str, str]] = MappingProxyType(
    {
        MetadataField.ARTIST: "artist",
        MetadataField.ALBUM: "album",
        MetadataField.DATE: "date",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PreviousEnrichment:
    """What an earlier build decided about one file, keyed by path (SAPRS 6.6).

    Only the answers MusicBrainz supplied are kept, never the tags: tags are
    re-read every run, so a tag the operator fixed is noticed at once and a stale
    enrichment is not. The identifiers come along so a resumed build looks the same
    recording up instead of searching for it again.
    """

    artist: str = ""
    album: str = ""
    date: str = ""
    recording_id: str = ""
    release_id: str = ""
    artist_id: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class EnrichmentOutcome:
    """What the stage did, in the numbers the report needs.

    Attributes:
        tracks: The same tracks, with gaps filled where an answer was found.
        repaired: Fields changed by enrichment. This is SAPRS 6.12's "metadata
            repaired" count — ADR-010's per-field record summed — and the report
            reads it from here rather than recounting rows.
        looked_up: Recordings retrieved. A run with 3,000 lookups and 3 repairs is
            visibly the run it is, which is the only way a bad search policy gets
            noticed.
        unavailable: Set once the service proves unreachable. The report then says
            "MusicBrainz unavailable", rather than reporting a build that never
            asked as one that found nothing.
        reused: Tracks backfilled from the previous build instead.
    """

    tracks: Sequence[Track] = ()
    repaired: int = 0
    looked_up: int = 0
    unavailable: bool = False
    reused: int = 0
    notes: tuple[str, ...] = field(default=())


def enrich(
    tracks: Sequence[Track],
    client: MusicBrainzClient,
    *,
    previous: Mapping[Path, PreviousEnrichment] | None = None,
) -> EnrichmentOutcome:
    """Fill gaps from MusicBrainz, or from the last build if it cannot be reached.

    Args:
        tracks: Normalized tracks, in discovery order.
        client: Anything implementing `MusicBrainzClient`. `DisabledClient` is a
            complete answer and takes the reuse path for every gap.
        previous: The last build's enrichment, keyed by file path (SAPRS 6.6).

    Returns:
        An `EnrichmentOutcome`. Never raises — 6.6's "must not prevent processing
        otherwise valid music" is a requirement on this function rather than an
        attitude in the caller.
    """

    cache: Mapping[Path, PreviousEnrichment] = previous if previous is not None else {}
    results: list[Track] = []
    outcome = _Counter()
    notes: list[str] = []
    down = False

    for track in tracks:
        gaps = _gaps(track)
        if not gaps:
            results.append(track)
            continue
        recording: Recording | None = None
        if not down and _worth_asking(gaps):
            recording, down = _lookup(track, client, notes)
            if recording is not None:
                outcome.looked_up += 1
        if recording is not None:
            filled, changed = _apply(recording, track, gaps)
            outcome.repaired += changed
            results.append(filled)
            continue
        backfilled = _reuse(track, cache, gaps)
        if backfilled is not track:
            outcome.reused += 1
        results.append(backfilled)

    return EnrichmentOutcome(
        tracks=tuple(results),
        repaired=outcome.repaired,
        looked_up=outcome.looked_up,
        unavailable=down,
        reused=outcome.reused,
        notes=tuple(notes),
    )


@dataclass(slots=True)
class _Counter:
    """Mutable accumulator, so `enrich` reads as a loop rather than arithmetic."""

    repaired: int = 0
    looked_up: int = 0
    reused: int = 0


def _gaps(track: Track) -> tuple[str, ...]:
    """Which fields have nothing usable.

    A placeholder counts as a gap and a wrong-but-plausible value does not. That
    asymmetry is deliberate: overruling "Uncle Kracker" because MusicBrainz spells
    it "Uncle Kraker" is level 2 contradicting level 1, and ADR-010 lets level 2
    act only where level 1 failed. Spelling is not a failure.
    """

    gaps: list[str] = []
    if is_placeholder(track.artist):
        gaps.append(MetadataField.ARTIST)
    if is_placeholder(track.album, kind="album"):
        gaps.append(MetadataField.ALBUM)
    if not track.date:
        gaps.append(MetadataField.DATE)
    return tuple(gaps)


def _worth_asking(gaps: Sequence[str]) -> bool:
    """Whether these gaps justify a request.

    A missing date does not. `MetadataField.DATE` is a gap on most of the measured
    corpus — a third of the MP3s carry no date tag at all — and MusicBrainz is polled at
    one request per second, so treating a missing year as a question would turn a 3,000
    file build into a fifty minute one to learn something the interface barely shows.
    The year still gets filled whenever an answer arrives for a *real* gap, which is the
    only way a date has ever been enriched here.
    """

    return any(name != MetadataField.DATE for name in gaps)


def _lookup(
    track: Track, client: MusicBrainzClient, notes: list[str]
) -> tuple[Recording | None, bool]:
    """Ask once for one track. Returns `(answer, service_is_down)`."""

    try:
        if track.musicbrainz_recording_id:
            return client.recording(track.musicbrainz_recording_id), False
        if track.title and track.artist and not is_placeholder(track.artist):
            return client.search(
                artist=track.artist, release=track.album or "", track=track.title
            ), False
        return None, False
    except MusicBrainzUnavailableError as error:
        notes.append(f"MusicBrainz unavailable: {error}")
        return None, True


def _apply(recording: Recording, track: Track, gaps: Sequence[str]) -> tuple[Track, int]:
    """Copy the answers that fill a gap onto the track, with provenance."""

    updated = track
    changed = 0
    for name in gaps:
        attribute = _ANSWERS.get(name)
        if attribute is None:
            continue
        value = str(getattr(recording, attribute, "")).strip()
        if not value or is_placeholder(value, kind=_kind(name)):
            continue
        if fold(_current(updated, name)) == fold(value):
            continue
        updated = updated.with_field(name, value, MetadataSource.MUSICBRAINZ)
        changed += 1
    if changed:
        updated = _carry_identifiers(updated, recording)
    return updated, changed


def _current(track: Track, name: str) -> str:
    return str(getattr(track, _TRACK_FIELD[name], "") or "")


_TRACK_FIELD: Mapping[str, str] = MappingProxyType(
    {
        MetadataField.ARTIST: "artist",
        MetadataField.ALBUM: "album",
        MetadataField.ALBUM_ARTIST: "album_artist",
        MetadataField.DATE: "date",
    }
)


def _kind(field_name: str) -> str:
    return "album" if field_name in (MetadataField.ALBUM,) else "artist"


def _carry_identifiers(track: Track, recording: Recording) -> Track:
    """Store the identifiers found, so the next build looks up instead of searching."""

    return replace(
        track,
        musicbrainz_recording_id=track.musicbrainz_recording_id or recording.recording_id,
        musicbrainz_release_id=track.musicbrainz_release_id or recording.release_id,
        musicbrainz_artist_id=track.musicbrainz_artist_id or recording.artist_id,
    )


def _reuse(track: Track, cache: Mapping[Path, PreviousEnrichment], gaps: Sequence[str]) -> Track:
    """Fall back to what the previous build decided about this file (SAPRS 6.6)."""

    earlier = cache.get(track.path)
    if earlier is None:
        return track
    updated = track
    for name in gaps:
        attribute = _REUSED.get(name)
        if attribute is None:
            continue
        value = str(getattr(earlier, attribute, "")).strip()
        if not value or is_placeholder(value, kind=_kind(name)):
            continue
        updated = updated.with_field(name, value, MetadataSource.MUSICBRAINZ)
    return updated
