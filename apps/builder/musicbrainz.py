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

There is a third answer, and #59 is the build that could not tell it apart from the second one.
`HTTP 503` from musicbrainz.org is a page that says *"The MusicBrainz web server is currently
busy. Please try again later."* — not unreachable, not a miss, an invitation to ask again later. It
is weather rather than arithmetic, and it was measured as weather: 16 of 24 release searches came
back that way at ten past ten, the same 24 asked once every three seconds failed 16 times too, and
a 189-request probe of the same endpoint at the ordinary pacing twenty minutes later returned no
failures at all. No `HTTP 429` appeared in any of the three, so it is not this client asking too
fast; it is somebody else's search service having an hour. Four things follow, and they are most of
this file's policy: a refused request is repeated once, after a wait the answer may name;
`available` counts failures *in a row* rather than in a run, and a row now costs a minute of silence
rather than the rest of the build; an album nobody answered is asked for again at the end of the pass
rather than written off; and the wait for an answer is a multiple of the slowest honest one this
client measures, because to everyone downstream a timeout and an outage are the same event.

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
    "ATTEMPTS_PER_REQUEST",
    "COOLDOWN",
    "COOLDOWNS_PER_RUN",
    "MAX_FAILURES",
    "MAX_RETRY_WAIT",
    "PATIENCE",
    "REQUESTS_PER_SECOND",
    "RETRYABLE_STATUS",
    "RETRY_WAIT",
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

#: How many consecutive failures cost this client a cooldown, and the status that is an answer
#: rather than a failure — a 404 means "not in the database", not "service down".
MAX_FAILURES: Final = 5

#: How long a client that has counted `MAX_FAILURES` of them stays quiet, and how many times it may
#: go quiet with nothing answered in between before the run stops asking. Together these are what
#: `available` means now, and they are the difference between an outage and a bad minute: the row
#: costs a minute of silence rather than the rest of the build, and an answer ends it. Three
#: consecutive silences is a service that is gone, and asking it a fourth time is this build taking
#: someone else's problem personally (#59).
COOLDOWN: Final = 60.0
COOLDOWNS_PER_RUN: Final = 3

#: The whole run's budget for being patient, in seconds. A row costs a minute, an answer ends the
#: row, and a service that refuses five, answers six, refuses five more and answers six again would
#: otherwise be waited out forever — a build that spends an hour asleep because the network is
#: healthy two thirds of the time is a build that looks hung and publishes nothing, which is the
#: failure this client exists to stop. Once the budget is gone a row still counts, so
#: `COOLDOWNS_PER_RUN` still ends a run that has learned nothing; what runs out is the sleeping.
PATIENCE: Final = 600.0

HTTP_NOT_FOUND: Final = 404
_YEAR_LENGTH: Final = 4

#: Answers that say "come back" rather than "gone" or "not here". 503 is musicbrainz.org's busy
#: page and 429 the same sentence in a newer dialect; 500, 502 and 504 come from the gateway in
#: front of the Cover Art Archive, which is a hosted service with its own bad afternoons. Each
#: is worth one more request. None of them is worth ending the run.
RETRYABLE_STATUS: Final = frozenset({429, 500, 502, 503, 504})

#: Attempts per question, the first one included. Two, not four: the condition a repeat cannot
#: fix is an outage, and an outage already has `MAX_FAILURES` to end it. A repeated request is
#: still one request per second at minimum, because being refused is not a licence to ask faster.
ATTEMPTS_PER_REQUEST: Final = 2

#: How long a repeat waits when the answer did not say, and the floor on a wait the service asked
#: for by name. Longer than the ordinary spacing, so that "later" means later: the 503s arrive in
#: stretches rather than one at a time, so a repeat one request-interval later is the same question
#: asked louder, and five questions in a row — two attempts each — is what `MAX_FAILURES` counts.
#: The *long* wait a needy album really gets is `albumart`'s end-of-pass deferral, several hundred
#: requests away; this is the short one that comes first.
RETRY_WAIT: Final = 5.0

#: The ceiling on a wait the service asked for by name. `Retry-After` is honoured, and a service
#: that says an hour is a service this build will not obey, because a build asleep for an hour
#: publishes nothing and looks hung. Half the cooldown above, so a wait the service names is never
#: longer than the one this client imposes on itself after five in a row.
MAX_RETRY_WAIT: Final = 30.0

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
        timeout: Seconds per request, measured rather than assumed. Measured *raw* — one request at
            a time, no client, no retry, no queue — on this library's own unanswered albums: a
            recording search at a 0.68 s median, a release search at 0.66 s, a Cover Art Archive
            front image at 0.55 s, and 4.9 s for the slowest of forty answers. The numbers this
            docstring quoted before #59 (a 5.3 s median and a tail past 45 for the same search) were
            taken from a build asking in a sustained queue of a service already refusing it, and are
            a description of the argument rather than the service. 15 s is three times the slowest
            honest answer; the patience that has to be long is `COOLDOWN`'s, not this one's,
            because a hung socket costs two attempts and then a minute of not asking.
        minimum_interval: Enforced spacing between requests.
        max_requests: Cap per run. A corpus with no identifiers in its tags would
            otherwise search 3,049 times at one request per second; the cap keeps
            a build's worst case bounded and puts the rest in the report as
            "not enriched", which is honest rather than complete.
        retry_wait: How long before repeating a question the service refused. A parameter so a
            test can assert the policy without paying for the seconds.
        cooldown: How long the client stays quiet after a row of `MAX_FAILURES`. Same reason.
        patience: The run's whole budget for those silences, which is what makes the policy
            finishable from the outside: worst case, this many seconds of the duration is waiting.
    """

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        minimum_interval: float = 1.0 / REQUESTS_PER_SECOND,
        max_requests: int = 2_000,
        retry_wait: float = RETRY_WAIT,
        cooldown: float = COOLDOWN,
        patience: float = PATIENCE,
        opener: Any = None,
    ) -> None:
        self._timeout = timeout
        self._interval = minimum_interval
        self._cap = max_requests
        self._retry_wait = retry_wait
        self._cooldown = cooldown
        self._patience = patience
        self._opener = opener
        self._lock = Lock()
        self._last_request = 0.0
        self._requests = 0
        self._failures = 0
        self._silences = 0
        self._slept = 0.0
        self._quiet_until = 0.0

    @property
    def available(self) -> bool:
        """False once this run has asked the last question it is allowed to.

        Five failures *in a row* cost the client a minute of silence rather than the rest of the
        build; three silences with nothing answered between them end the asking, and `PATIENCE` caps
        what the whole run can spend doing either. That is the shape
        the weather arrives in — 16 of 24 release searches refused at ten past ten, none of 189
        refused at twenty past eleven, same tags, same machine, same pacing in between — and it is
        why the count that ends a run has to be a count of *rows*, and rows have to cost waiting
        rather than giving up.

        Two defects lived behind this one line before #59. The row counter was a run counter: a
        success cleared nothing, so five blinks spread over an hour were an outage on a service that
        had answered nine hundred other questions. And the stages did not consult this at all —
        `enrichment` and `albumart` each latched off on the first exception they personally saw,
        which is a stricter policy than the one the client advertises, and the stricter one wins.
        """

        return self._requests < self._cap and self._silences < COOLDOWNS_PER_RUN

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
            self._fail()
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

        A 503 is neither, and #59 is the build that read it as the second. A request the service
        refused with "try again later" is asked once more, later, and only becomes a failure if
        the repeat is refused too. Counting a refusal as a failure, and a failure as an outage,
        is what made one busy minute on someone else's search server the end of a whole build's
        artwork — on a run whose report then blamed the network.
        """

        if not self.available:
            return None
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
        attempt, wait = 1, 0.0
        while True:
            self._wait(after=wait)
            try:
                body = self._send(request, limit=limit)
            except urllib.error.HTTPError as error:
                if error.code == HTTP_NOT_FOUND:
                    # A 404 is an answer, and the only thing it says is "not in the database". It
                    # ends a row for the same reason a recording does: a service that can say no is
                    # not a service that has gone away.
                    self._succeed()
                    return None
                if error.code in RETRYABLE_STATUS and self._may_repeat(attempt):
                    attempt, wait = attempt + 1, _retry_after(error, default=self._retry_wait)
                    continue
                self._fail()
                raise MusicBrainzUnavailableError(f"HTTP {error.code} from {url}") from error
            except (TimeoutError, urllib.error.URLError, OSError, ValueError) as error:
                if self._may_repeat(attempt):
                    attempt, wait = attempt + 1, self._retry_wait
                    continue
                self._fail()
                raise MusicBrainzUnavailableError(f"cannot reach {url}: {error}") from error
            self._succeed()
            self._requests += 1
            # An answer bigger than the promise is not a picture Encore meant to fetch: the cache
            # would reduce it to 640 px anyway, and the way to lose a party is to hold a 40 MB body
            # in memory to find out. None, which is "this album has no cover" as far as the report is
            # concerned — and honest, because a host that sends that is not this host's image store.
            return body if len(body) <= limit else None

    def _send(self, request: urllib.request.Request, *, limit: int) -> bytes:
        """One attempt at one URL. What an attempt *means* is decided by `_body`, here and nowhere.

        `limit + 1` rather than `limit`, so "too big" is a length the client measured rather than
        a `Content-Length` header an image host is not obliged to send.
        """

        with (self._opener or urllib.request.urlopen)(request, timeout=self._timeout) as response:
            body: bytes = response.read(limit + 1)
            return body

    def _fail(self) -> None:
        """Count one question this run could not ask, and go quiet if that completes a row.

        One question is one failure however many attempts it took, which is the difference between
        a policy and a panic: were the repeat counted, three busy albums would be an outage of five,
        and #59's build would stop asking a third of the way into a queue it had been told to come
        back to. The reason is not kept here: it travels in the exception the caller raises, and the
        report prints it from `notes`, which is one home for the sentence rather than two.
        """

        with self._lock:
            self._failures += 1
            if self._failures >= MAX_FAILURES:
                self._failures = 0
                self._silences += 1
                left = max(0.0, self._patience - self._slept)
                self._quiet_until = time.monotonic() + min(self._cooldown, left)

    def _succeed(self) -> None:
        """Clear the row and the silences. An answer, however slow, ends a bad minute."""

        with self._lock:
            self._failures = 0
            self._silences = 0

    def _may_repeat(self, attempt: int) -> bool:
        """Whether this run may spend a request on repeating itself.

        Two bounds, both already this client's own: an attempt counter, and `available`, which
        stops a run that has hit five in a row or spent its allowance from turning one bad minute
        into two requests per question forever. Being refused is a reason to ask again once, not
        a reason to ask forever.
        """

        return attempt < ATTEMPTS_PER_REQUEST and self.available

    def _wait(self, *, after: float = 0.0) -> None:
        """Space requests by at least `minimum_interval`, `after`, or whatever silence is left.

        The third term is the cooldown arriving rather than a flag being set: a run that has counted
        a row of failures sleeps out the rest of it on the next request rather than reporting itself
        unavailable and finishing early, which is the difference between waiting for a service and
        giving up on it. With nothing owed the three terms are the ordinary one second.
        """

        with self._lock:
            quiet = self._quiet_until - time.monotonic()
            floor = max(self._interval, after, quiet)
            now = time.monotonic()
            delay = floor - (now - self._last_request)
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
                self._slept += max(0.0, min(delay, quiet))
            self._last_request = now


def _retry_after(error: urllib.error.HTTPError, *, default: float) -> float:
    """How long the service asked to be given, which is a number only when it said one.

    `Retry-After` is honoured because asking faster than a rate-limited client was told to is the
    one thing this client must not do. It is floored at `default` and capped, because
    musicbrainz.org's busy page sends `retry-after: 0` — a fact about the gateway's window, not
    an invitation to fire the next request now — and because a build that sleeps for an hour is
    a build that publishes nothing.
    """

    stated = error.headers.get("Retry-After") if error.headers is not None else None
    try:
        seconds = float(stated) if stated is not None else None
    except ValueError:
        return default
    if seconds is None:
        return default
    return min(max(seconds, default), MAX_RETRY_WAIT)


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
