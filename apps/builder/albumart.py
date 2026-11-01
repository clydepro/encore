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

**A busy service is answered in the order it asked to be.** musicbrainz.org returns HTTP 503 with
the sentence "please try again later", and later is what this stage gives it: an album whose
question was refused goes to the back of the queue, which on a real library is several hundred
requests and therefore minutes away, and is asked once more there. Not retried on the spot, and
not retried forever. What comes back is four counts and a flag, because the four mean different
things: `unmatched` and `uncovered` are this library's own answers about an album, `unanswered` is
a fact about the afternoon, and `unavailable` means the asking stopped with albums still in the
list. A run that ended early used to report only the last of those and keep none of the others
(#59).

The network is not this module's to touch. `apps.builder.musicbrainz` is the one file the
architecture guardrail allows to open a socket, so everything here is asked of an injected client,
and every number returned is a count of what was *decided* — which is also what makes the stage
testable without a fake that pretends to have a DNS record.
"""

from __future__ import annotations

from collections import deque
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

#: How an album is named in this module: `Track.group_key`, which is how `construction` files the
#: album row too. Two spellings of that pair would disagree about exactly the albums that matter.
_AlbumKey = tuple[str, str]


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
        requested: Lookups made, counting both halves of an unidentified album and both attempts
            of a refused one.
        covered: Albums that got a picture.
        unmatched: Albums whose release could not be named exactly. A count rather than a warning
            — this is the policy working, and 400 lines of it would be the report burying the
            three numbers an operator acts on.
        uncovered: Albums identified, and found to have no image at the archive.
        unanswered: Albums the service never answered, twice. Neither unmatched nor uncovered:
            those are claims about the album, and this is a claim about the afternoon (#59).
        unavailable: Set when the run stopped before its list was empty, which is the client's
            judgement rather than this stage's. `DisabledClient` answers that way too, so a report
            that reads this as a network fault has to know whether the build was asking at all.
        not_reached: Albums this pass never got to, because the asking stopped. Always derivable,
            which is why it is a property and not a fifth counter somebody has to keep in step.
    """

    tracks: Sequence[Track] = ()
    payloads: Mapping[tuple[str, str], bytes] = field(default_factory=dict)
    albums: int = 0
    requested: int = 0
    covered: int = 0
    unmatched: int = 0
    uncovered: int = 0
    unanswered: int = 0
    unavailable: bool = False

    @property
    def not_reached(self) -> int:
        """Candidates this run did not ask about at all, because the asking stopped."""

        return self.albums - self.covered - self.unmatched - self.uncovered - self.unanswered


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

    payloads: dict[_AlbumKey, bytes] = {}
    identifiers: dict[_AlbumKey, str] = {}
    asked = covered = unmatched = uncovered = given_up = 0
    pending: deque[tuple[_AlbumKey, _Album]] = deque(
        sorted(needy.items(), key=lambda item: (item[1].artist, item[1].title))
    )
    later: set[_AlbumKey] = set()

    while pending:
        if not client.available:
            # The client is the only thing that knows how many failures it has counted, and it says
            # "in a row" — and since #59 a row costs it a minute of silence rather than the end of
            # this loop, so reaching here means the silence ran out three times, not that the pass
            # met an exception. This loop used to stop at the first one of its own, which is how a
            # build over 481 grey albums asked three questions and then blamed the network for the
            # other 478.
            break
        key, album = pending.popleft()
        release_id = identifiers.get(key) or album.release_id
        if not release_id:
            candidates, asked, refused = _candidates(client, album, asked)
            if refused:
                if _later(pending, later, key, album):
                    continue
                given_up += 1
                continue
            release_id = _exact(candidates, album)
            if not release_id:
                # Not `given_up`. An album the archive answered "not that one" is an album this
                # library could not name exactly, which is the policy working; an album it never
                # answered is a fact about this afternoon, and the two numbers mean different
                # things to whoever reads the report.
                unmatched += 1
                continue
        # The identifier is written before the picture is settled, because the search that found
        # it already happened: an album whose cover request failed is the next build's one-request
        # question rather than its two-request one, and this run's second attempt at it too.
        identifiers[key] = release_id
        image, asked, refused = _cover(client, release_id, asked)
        if refused:
            if _later(pending, later, key, album):
                continue
            given_up += 1
            continue
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
        unanswered=given_up,
        unavailable=bool(pending),
    )


def _later(
    pending: deque[tuple[_AlbumKey, _Album]],
    later: set[_AlbumKey],
    key: _AlbumKey,
    album: _Album,
) -> bool:
    """Put an album back for the end of this pass, or give up on it. True if it went back.

    "Try again later" is what a busy musicbrainz.org actually answers, and the later this run can
    offer is the rest of the pass: several hundred requests away on a real library, which is
    minutes — a sensible amount of time to wait for someone else's search server, and no time at
    all to add to the build. One second chance per album, because an album that fails twice has
    told this run everything it knows, and the next build asks again anyway: the identifier stuck
    to its row is what makes that question a single request rather than two.

    An album given up on is neither covered, unmatched nor uncovered. Those three are claims about
    the album; this one is a claim about the afternoon, and the report counts it separately.
    """

    if key in later:
        return False
    later.add(key)
    pending.append((key, album))
    return True


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
    """Search for the release. Returns `(candidates, requests, this question was refused)`."""

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
