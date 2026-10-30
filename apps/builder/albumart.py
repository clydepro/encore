"""Stage 5b — the release's own cover, for an album whose files carry none (SAPRS 6.7, ADR-016).

SAPRS 6.7 states one rule for artwork and stops: *prefer the image embedded in the file*. That
rule is right for the 98% of M4A in this corpus that arrived from a rip with a picture in it, and
it used to be the whole of what the Builder did — which is why a library of FLAC files, whose
format carries no picture at all, published 463 grey tiles and reported nothing unusual. SAPRS 6.6
says metadata may be repaired from MusicBrainz; nothing said a *picture* may be, because 6.7 never
named a second source. ADR-016 names one, at the precedence the guest screen implies: the file's
own art first, then the release's, then nothing.

**Exact, or silent.** An album gets a picture only when the release can be named by the two things
this library already knows about it — the grouping artist and the album title, each equal after
folding. Not "closest", not "highest-ranked", not "the only result". A wrong cover is worse than a
grey tile: a guest scanning tiles for an album they cannot find is annoyed, and a guest looking at
artwork that belongs to someone else's record has been lied to about where something is, by the
one part of the interface they trusted to be a picture. That is ADR-010's argument about a guessed
artist applied to an image, and it is why the check below compares two names it was given instead
of asking a third question.

**One request per album, and fewer every time.** The unit of asking is an album rather than a
track — the 463 uncovered albums of the measured corpus cost 926 requests, not 2,000 — and an
identifier found on the way is written onto the album so the next build asks once instead of
twice. A build that is not enrichment-enabled never comes here: its client is a `DisabledClient`,
which is the same answer as an outage through a shorter road than a second flag.

The network is not this module's to touch. `apps.builder.musicbrainz` is the one file the
architecture guardrail allows to open a socket, so everything here is asked of an injected client,
and every number returned is a count of what was *decided* — which is also what makes the stage
testable without a fake that pretends to have a DNS record.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from apps.builder.enrichment import PreviousEnrichment
from apps.builder.musicbrainz import (
    MusicBrainzClient,
    MusicBrainzUnavailableError,
    Release,
)
from apps.builder.normalization import fold
from apps.builder.records import Track

__all__ = ["CoverOutcome", "fetch"]


@dataclass(frozen=True, slots=True, kw_only=True)
class CoverOutcome:
    """What the archive was asked for, and what came back.

    Attributes:
        tracks: The same tracks, with `musicbrainz_release_id` filled in wherever a release was
            identified — including the albums that turned out to have no picture, because that
            identifier is the reason the next build is cheaper.
        payloads: Image bytes to cache, keyed the way `construction` keys an album. Empty is the
            normal answer to an incremental build, not a failed one.
        albums: Albums with no embedded or carried picture, so: candidates.
        requested: Lookups made, counting both halves of an unidentified album.
        covered: Albums that got a picture.
        unmatched: Albums whose release could not be named exactly. A count rather than a warning
            — this is the policy working, and 400 lines of it would be the report burying the
            three numbers an operator acts on.
        uncovered: Albums identified, and found to have no image at the archive.
        unavailable: Set once the service stops answering, which ends the asking for this run.
    """

    tracks: Sequence[Track] = ()
    payloads: Mapping[tuple[str, str], bytes] = field(default_factory=dict)
    albums: int = 0
    requested: int = 0
    covered: int = 0
    unmatched: int = 0
    uncovered: int = 0
    unavailable: bool = False


def fetch(
    tracks: Sequence[Track],
    client: MusicBrainzClient,
    *,
    carried: Mapping[Path, object] | None = None,
    earlier: Mapping[Path, PreviousEnrichment] | None = None,
) -> CoverOutcome:
    """Ask for one picture per album that has none. Never raises.

    Args:
        tracks: The catalogable tracks. An album is a group of these, and the group is the unit
            of asking.
        client: Anything that speaks MusicBrainz. A `DisabledClient` ends this at the first line
            of the loop below, which is the whole of what "off" means here.
        carried: Files this incremental build did not re-read, with the picture the last build
            resolved for each. An album whose only artwork is a carried reference is covered —
            asking about it again would be #52 in a new costume, spending requests to rediscover
            a picture the library already holds.
        earlier: The previous build's enrichment, per file, which is where a release identifier
            comes from when it was found for some *other* reason. That is the common case: an
            album whose artist was repaired in March should not have to search for itself again
            in October to be drawn.
    """

    held = carried if carried is not None else {}
    before = earlier if earlier is not None else {}
    loaded = _preload(tracks, before)
    groups = _group(loaded, held)
    needy = {key: album for key, album in groups.items() if album.needs_picture}

    payloads: dict[tuple[str, str], bytes] = {}
    identifiers: dict[tuple[str, str], str] = {}
    asked = covered = unmatched = uncovered = 0
    down = not client.available

    for key, album in sorted(needy.items(), key=lambda item: (item[1].artist, item[1].title)):
        if down:
            break
        release_id = album.release_id
        if not release_id:
            candidates, asked, down = _candidates(client, album, asked)
            if down:
                # Not `unmatched`. An album the archive never answered is not an album this
                # library failed to name exactly, and the report line that says "N not named
                # exactly" is read as "N albums MusicBrainz does not have", which is a different
                # sentence about a different problem.
                break
            release_id = _exact(candidates, album)
            if not release_id:
                unmatched += 1
                continue
        image, asked, down = _cover(client, release_id, asked)
        # The identifier is written before the picture is settled, because the search that found
        # it already happened: an album whose cover request hit an outage is the next build's
        # one-request question rather than its two-request one.
        identifiers[key] = release_id
        if down:
            # Neither covered nor uncovered. The report says the service stopped answering, and a
            # count of "albums with no picture" inflated by the albums this run never got to is
            # the number an operator would act on for the wrong reason.
            break
        if image is None:
            uncovered += 1
            continue
        payloads[key] = image
        covered += 1

    return CoverOutcome(
        tracks=_apply_identifiers(loaded, identifiers),
        payloads=payloads,
        albums=len(needy),
        requested=asked,
        covered=covered,
        unmatched=unmatched,
        uncovered=uncovered,
        unavailable=down,
    )


def _preload(tracks: Sequence[Track], earlier: Mapping[Path, PreviousEnrichment]) -> list[Track]:
    """Give back the release ids an earlier build worked out, as if the tags had carried them.

    A tag still wins: `PreviousEnrichment` is only consulted where a track has no id of its own,
    which is the same precedence ADR-010 applies to every other value — and the reason the
    identifier is stored per file rather than in a table of its own.
    """

    loaded: list[Track] = []
    for track in tracks:
        found = earlier.get(track.path)
        if track.musicbrainz_release_id or found is None or not found.release_id:
            loaded.append(track)
        else:
            loaded.append(replace(track, musicbrainz_release_id=found.release_id))
    return loaded


@dataclass(slots=True, kw_only=True)
class _Album:
    """One album, as the question about its cover needs it: two names, one identifier, one flag.

    Mutable and private on purpose. The grouping pass is the only writer, and what it accumulates
    is a property of the group rather than of a track — "does this release have a picture
    anywhere" cannot be answered from one file.
    """

    artist: str
    title: str
    release_id: str = ""
    has_picture: bool = False

    @property
    def needs_picture(self) -> bool:
        """Whether to ask. An album with no name of its own is not a question anyone can answer.

        The empty-title case is `[Unknown Album]` after normalization, which construction files
        under its own placeholder; asking the archive for it would search for the word "unknown"
        against whoever the artist is, and return something.
        """

        return not self.has_picture and bool(self.artist.strip()) and bool(self.title.strip())


def _group(
    tracks: Sequence[Track], carried: Mapping[Path, object]
) -> dict[tuple[str, str], _Album]:
    """Group by `Track.group_key` — the same rule construction files albums by.

    An album counts as covered when *any* of its files carries an embedded image or arrived with a
    carried reference, which is the rule `_associate_artwork` applies when it points a row at a
    picture: one cover per release is what a jukebox means by having one.
    """

    groups: dict[tuple[str, str], _Album] = {}
    for track in tracks:
        key = track.group_key
        album = groups.setdefault(key, _Album(artist=track.artist, title=track.album or ""))
        if track.musicbrainz_release_id and not album.release_id:
            album.release_id = track.musicbrainz_release_id
        album.has_picture |= bool(track.extracted.embedded_artwork) or track.path in carried
    return groups


def _candidates(
    client: MusicBrainzClient, album: _Album, asked: int
) -> tuple[Sequence[Release], int, bool]:
    """Search for the release. Returns `(candidates, requests, service_is_down)`."""

    try:
        found = client.releases(artist=album.artist, album=album.title)
    except MusicBrainzUnavailableError:
        return (), asked + 1, True
    return tuple(found), asked + 1, False


def _cover(
    client: MusicBrainzClient, release_id: str, asked: int
) -> tuple[bytes | None, int, bool]:
    try:
        image = client.cover(release_id)
    except MusicBrainzUnavailableError:
        return None, asked + 1, True
    return image, asked + 1, False


def _exact(candidates: Sequence[Release], album: _Album) -> str:
    """The first candidate that is this album, by both names. Empty string for none of them.

    `fold` rather than `==`, because two spellings of one artist legitimately differ in case and
    punctuation and everything else here is exact: no substring, no prefix, no similarity
    threshold, and no second chance if the first hit is someone else's record.
    """

    artist, title = fold(album.artist), fold(album.title)
    if not artist or not title:
        return ""
    return next(
        (
            release.release_id
            for release in candidates
            if fold(release.artist) == artist and fold(release.title) == title
        ),
        "",
    )


def _apply_identifiers(
    tracks: Sequence[Track], identifiers: Mapping[tuple[str, str], str]
) -> tuple[Track, ...]:
    """Put each album's newly found release id onto the tracks of that album.

    One pass over the tracks rather than one pass per album, because a 700-album library would
    otherwise make this the most expensive line in a stage whose whole job is to be cheap.
    """

    return tuple(
        replace(track, musicbrainz_release_id=identifier)
        if (identifier := identifiers.get(track.group_key)) and not track.musicbrainz_release_id
        else track
        for track in tracks
    )
