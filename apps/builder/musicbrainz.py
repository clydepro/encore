"""The MusicBrainz client: enrichment's only network path (SAPRS 6.6, ADR-001).

Three rules from 6.6 shape this file. Enrichment is **builder-only**, so nothing
in `encore/` imports it and no Server code path can reach a socket here. It is
**optional**, so `DisabledClient` is a complete implementation of the protocol and
the pipeline runs unchanged with it. And it is **rate-limited**, because the
public servers will answer 15,000 lookups at the speed they choose, not the speed
a build would like.

An outage must not stop a build (6.6's last line), so every transport failure
raises `MusicBrainzUnavailableError` and the caller treats that as "use the tags". The
distinction that matters is between *nothing found* and *nothing reachable*: the
first is a fact about a recording and the second is a fact about this afternoon,
and only the first may be cached.

The identifiers Encore already has in tags are looked up directly; searching by
name is available but off unless asked for, because ADR-010 measured these tags as
better than a blind match and because a fuzzy match that files 200 songs under the
wrong artist is a worse outcome than 200 songs with no album artist at all.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from threading import Lock
from typing import Any, Final, Protocol

__all__ = [
    "REQUESTS_PER_SECOND",
    "DisabledClient",
    "HttpMusicBrainzClient",
    "MusicBrainzClient",
    "MusicBrainzUnavailableError",
    "Recording",
    "Release",
]

#: MusicBrainz's published policy for clients: no more than one request per
#: second, and identify yourself. A build that ignores this gets throttled or
#: blocked, and a blocked build that nobody noticed is the failure mode 6.6
#: forbids, so the limit is enforced here rather than entrusted to politeness.
REQUESTS_PER_SECOND: Final = 1.0

#: How many consecutive failures end this run's asking, and the status that is an answer
#: rather than a failure — a 404 means "not in the database", not "service down".
MAX_FAILURES: Final = 5
HTTP_NOT_FOUND: Final = 404
_YEAR_LENGTH: Final = 4

#: A JSON answer is small. The limit is a backstop against a redirect to something that is
#: not a MusicBrainz document, not a tuning knob.
_MAX_JSON_BYTES: Final = 4 * 1024 * 1024

#: Lookup paths. `inc` asks for the relations and release we need in one round
#: trip, which at one request per second is the difference between a two-minute
#: enrichment and a six-minute one.
_BASE: Final = "https://musicbrainz.org/ws/2"

#: The Cover Art Archive. A different host, and a different service: MusicBrainz answers "what
#: release is this" and this answers "what does its front look like". It is here rather than in
#: `albumart.py` because this module is the one the architecture guardrail allows to open a
#: socket, and moving the URL would mean moving the exception.
_COVER_BASE: Final = "https://coverartarchive.org"
_USER_AGENT: Final = "EncoreJukebox/0.1 ( https://github.com/clydepro/encore )"

#: How many release candidates one album may be offered. The archive is asked for the first
#: candidate whose artist and title match exactly, which is why a wider list costs a request and
#: buys nothing: a fuzzy fifth result is the wrong cover with more confidence.
_RELEASE_CANDIDATES: Final = 5

#: A cover is a picture, not a document. `front` serves the largest image the archive holds for
#: a release, and 1200 px JPEG is under a megabyte; anything arriving many times larger is a
#: booklet scan or a mistake, and the cache would normalize it to 640 px anyway. Read no further
#: rather than buffer a stranger's 40 MB.
_MAX_COVER_BYTES: Final = 8 * 1024 * 1024


class MusicBrainzUnavailableError(RuntimeError):
    """The service could not be reached. Not a miss: retry another day."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Recording:
    """What one MusicBrainz recording says about a track.

    Attributes:
        recording_id / release_id / artist_id: Identifiers, so a later build can
            look the same thing up without searching for it again.
        title / artist / album: The values, already disambiguated by the
            relationship graph — which is the whole reason to ask.
        year: Release year as text. `date` is deliberately not parsed further:
            tag dates are partial and 6.4 says so.
    """

    recording_id: str = ""
    release_id: str = ""
    artist_id: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    year: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class Release:
    """What MusicBrainz says about one release, for the question album art asks.

    Not the tracks: enrichment has `Recording` for that. This is the smaller claim — *which*
    release is this album — and the fields are the two an exact match can be tested against.

    Attributes:
        release_id: The MBID, which is what the Cover Art Archive is addressed by.
        title / artist: As the database spells them. `albumart` compares these against the
            album's own artist and title, folded, and asks for a picture only when both agree.
    """

    release_id: str = ""
    title: str = ""
    artist: str = ""


class MusicBrainzClient(Protocol):
    """The lookups the pipeline makes."""

    def recording(self, recording_id: str) -> Recording | None:
        """Fetch one recording by MBID, or None if the id is not in the database."""

    def search(self, *, artist: str, release: str, track: str) -> Recording | None:
        """Search for a recording by name, or None for no confident match."""

    def releases(self, *, artist: str, album: str) -> Sequence[Release]:
        """Candidate releases for an album, best first. Empty is a complete answer."""

    def cover(self, release_id: str) -> bytes | None:
        """The release's front image, or None for "there isn't one".

        None is also the answer when the service is unreachable, from this function only:
        a missing cover is a grey tile and an outage is a report line, and the difference
        belongs to `releases`, which is the call that has to succeed for the id to be new.
        """

    @property
    def available(self) -> bool:
        """False when the client is disabled or has already failed this run."""


class DisabledClient:
    """The no-network implementation, and the default.

    Being the default is the point: a build on a machine without a route to
    musicbrainz.org, or a `--offline` run on one with a route, takes the same code
    path as every other build rather than a special one.
    """

    def recording(self, recording_id: str) -> Recording | None:  # noqa: ARG002 - the signature is the protocol
        return None

    def search(self, *, artist: str, release: str, track: str) -> Recording | None:  # noqa: ARG002 - the signature is the protocol
        return None

    def releases(self, *, artist: str, album: str) -> Sequence[Release]:  # noqa: ARG002
        return ()

    def cover(self, release_id: str) -> bytes | None:  # noqa: ARG002 - the signature is the protocol
        return None

    @property
    def available(self) -> bool:
        return False


class HttpMusicBrainzClient:
    """Rate-limited JSON lookups against the public MusicBrainz web service.

    Args:
        timeout: Seconds per request. Generous, because a slow answer is still an
            answer and a timeout is a rebuild.
        minimum_interval: Enforced spacing between requests.
        max_requests: Cap per run. A corpus with no identifiers in its tags would
            otherwise search 3,049 times at one request per second; the cap keeps
            a build's worst case bounded and puts the rest in the report as
            "not enriched", which is honest rather than complete.
    """

    def __init__(
        self,
        *,
        timeout: float = 8.0,
        minimum_interval: float = 1.0 / REQUESTS_PER_SECOND,
        max_requests: int = 2_000,
        opener: Any = None,
    ) -> None:
        self._timeout = timeout
        self._interval = minimum_interval
        self._cap = max_requests
        self._opener = opener
        self._lock = Lock()
        self._last_request = 0.0
        self._requests = 0
        self._failures = 0

    @property
    def available(self) -> bool:
        """False once five requests have failed in a row.

        Five rather than one, because a single dropped request in a 20-minute build
        is the internet, and five in a row is musicbrainz.org deciding it has had
        enough. After that the pipeline stops asking and finishes from tags, which
        is 6.6's requirement rather than a fallback.
        """

        return self._requests < self._cap and self._failures < MAX_FAILURES

    def recording(self, recording_id: str) -> Recording | None:
        payload = self._get(
            f"{_BASE}/recording/{urllib.parse.quote(recording_id)}",
            {"inc": "recording-rels+release-rels"},
        )
        return _recording_from(payload)

    def search(self, *, artist: str, release: str, track: str) -> Recording | None:
        query = " AND ".join(
            part
            for part in (
                _lucene("recording", track),
                _lucene("artist", artist),
                _lucene("release", release),
            )
            if part
        )
        if not query:
            return None
        payload = self._get(f"{_BASE}/recording", {"query": query, "limit": "1"})
        recordings: Sequence[Any] = (payload or {}).get("recordings") or []
        return _recording_from(recordings[0]) if recordings else None

    def releases(self, *, artist: str, album: str) -> Sequence[Release]:
        """Candidate releases for an album, in the order MusicBrainz rates them.

        The query is quoted and exact (`_lucene`), because this answer is about to become a
        picture on a party screen: a search that finds the right artist's *other* album and
        illustrates this one with it is worse than finding nothing. `albumart` still checks the
        two names before asking for an image — this is the first of two guards, not the whole of
        one.
        """

        query = " AND ".join(
            part for part in (_lucene("release", album), _lucene("artist", artist)) if part
        )
        if not query:
            return ()
        payload = self._get(f"{_BASE}/release", {"query": query, "limit": str(_RELEASE_CANDIDATES)})
        documents: Sequence[Any] = (payload or {}).get("releases") or []
        return tuple(
            release
            for release in (_release_from(document) for document in documents)
            if release is not None
        )

    def cover(self, release_id: str) -> bytes | None:
        """The release's front image, or None.

        `front` is the archive's own "largest image on the front panel", which is the one answer
        that does not require choosing between a 250 px thumbnail and a 1400 px scan; the cache
        resizes whatever arrives. A 404 here is ordinary — a large share of MusicBrainz releases
        have no cover image at all — which is why this returns None rather than raising on the
        miss, and why an unreachable archive is reported by `available` rather than by a count of
        albums that got nothing.
        """

        if not release_id:
            return None
        return self._body(
            f"{_COVER_BASE}/release/{urllib.parse.quote(release_id, safe='')}/front",
            {},
            limit=_MAX_COVER_BYTES,
        )

    def _get(self, url: str, params: Mapping[str, str]) -> dict[str, Any] | None:
        """One JSON GET. See `_body` for the transport and the counting."""

        body = self._body(url, params)
        if body is None:
            return None
        try:
            parsed = json.loads(body)
        except (TypeError, ValueError) as error:
            self._failures += 1
            raise MusicBrainzUnavailableError("malformed JSON from MusicBrainz") from error
        return parsed if isinstance(parsed, dict) else None

    def _body(
        self, url: str, params: Mapping[str, str], *, limit: int = _MAX_JSON_BYTES
    ) -> bytes | None:
        """One GET, rate-limited, None on a miss, `MusicBrainzUnavailableError` on a failure.

        A 404 is a miss and is not counted as a failure: an identifier in a tag
        that MusicBrainz does not know is a fact about the tag, and treating it as
        an outage would switch enrichment off for the rest of the run because one
        file had a mistyped MBID.
        """

        if not self.available:
            return None
        self._wait()
        full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
        request = urllib.request.Request(  # noqa: S310 - `url` is one of the two constants above, https-only
            full,
            headers={
                "User-Agent": _USER_AGENT,
                # The archive serves a different kind of thing than the web service does, and an
                # `Accept: application/json` on an image request is how a client ends up being
                # served a JSON description of a picture it asked for.
                "Accept": "image/*" if url.startswith(_COVER_BASE) else "application/json",
            },
        )
        try:
            with (self._opener or urllib.request.urlopen)(
                request, timeout=self._timeout
            ) as response:
                # `limit + 1`, so "too big" is decided by a length rather than by trusting a
                # `Content-Length` header an image host is not obliged to send.
                body: bytes = response.read(limit + 1)
        except urllib.error.HTTPError as error:
            if error.code == HTTP_NOT_FOUND:
                return None
            self._failures += 1
            raise MusicBrainzUnavailableError(f"HTTP {error.code} from {url}") from error
        except (TimeoutError, urllib.error.URLError, OSError, ValueError) as error:
            self._failures += 1
            raise MusicBrainzUnavailableError(f"cannot reach {url}: {error}") from error
        self._requests += 1
        # An answer bigger than the promise is not a picture Encore meant to fetch: the cache
        # would reduce it to 640 px anyway, and the way to lose a party is to hold a 40 MB body
        # in memory to find out. None, which is "this album has no cover" as far as the report is
        # concerned — and honest, because a host that sends that is not this host's image store.
        return body if len(body) <= limit else None

    def _wait(self) -> None:
        """Space requests by at least `minimum_interval`, under the lock."""

        with self._lock:
            now = time.monotonic()
            delay = self._interval - (now - self._last_request)
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._last_request = now


def _recording_from(payload: Mapping[str, Any] | None) -> Recording | None:
    """Pull the fields Encore cares about out of one recording document.

    Artist and release come from the relationship arrays rather than the
    top-level fields, because a recording's `artist-credit` is an attribution
    ("Artist feat. Artist") and `artist-rels`/`release-rels` name the person the
    release is filed under — which is the question enrichment is being asked to
    answer.
    """

    if not payload:
        return None
    recording_id = str(payload.get("id", ""))
    title = str(payload.get("title", ""))
    artist = _credit(payload.get("artist-credit"))
    artist_id = ""
    release_id = ""
    release_title = ""
    year = ""
    for relation in payload.get("relations") or ():
        target = relation.get("target-type")
        entity = relation.get("release") or relation.get("artist") or {}
        if target == "release" and not release_id:
            release_id = str(entity.get("id", ""))
            release_title = str(entity.get("title", ""))
            year = _year(entity)
        elif target == "artist" and not artist_id:
            artist = str(entity.get("name", artist))
            artist_id = str(entity.get("id", ""))
    if not (recording_id or title):
        return None
    return Recording(
        recording_id=recording_id,
        release_id=release_id,
        artist_id=artist_id,
        title=title,
        artist=artist,
        album=release_title,
        year=year,
    )


def _release_from(payload: Mapping[str, Any]) -> Release | None:
    """One release document, as the two names and an identifier.

    `artist-credit` rather than a relation, because a release's credited artist is what the
    grouping in `library.db` was built from — the same flattening `_credit` does for a
    recording, for the same reason.
    """

    release_id = str(payload.get("id", ""))
    if not release_id:
        return None
    return Release(
        release_id=release_id,
        title=str(payload.get("title", "")),
        artist=_credit(payload.get("artist-credit")),
    )


def _credit(credit: object) -> str:
    """Flatten an `artist-credit` list into one join phrase."""

    if isinstance(credit, str):
        return credit
    if not isinstance(credit, Sequence):
        return ""
    parts: list[str] = []
    for item in credit:
        if isinstance(item, Mapping):
            artist = item.get("artist") or {}
            name = str(artist.get("name", "")) if isinstance(artist, Mapping) else ""
            parts.append(name or str(item.get("name", "")))
            join = str(item.get("joinphrase", ""))
            if join:
                parts.append(join)
        elif isinstance(item, str):
            parts.append(item)
    return "".join(parts).strip()


def _year(release: Mapping[str, Any]) -> str:
    date = str(release.get("date", ""))
    return (
        date[:_YEAR_LENGTH] if len(date) >= _YEAR_LENGTH and date[:_YEAR_LENGTH].isdigit() else ""
    )


def _lucene(field: str, value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'{field}:"{escaped}"'
