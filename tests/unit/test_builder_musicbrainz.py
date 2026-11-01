"""The MusicBrainz client: a network boundary that must stay boring (SAPRS 6.6, 12.4).

No test here reaches the internet. `HttpMusicBrainzClient` takes an `opener` for exactly
this reason — the shape of a response is the interesting part, and a live server would
make every assertion below a race with someone else's uptime.

What is asserted, in priority order:

* **The polite parts.** One request per second and a per-run cap, because enrichment is
  optional and being blocked by musicbrainz.org would make an optional feature a
  permanent one (12.4 names the rate limit explicitly).
* **The difference between "not found" and "unreachable".** A 404 is a fact about a tag;
  a 503 is a fact about the service. Confusing them turns one mistyped MBID into a build
  that stops enriching.
* **The payload reader.** The API answers a recording lookup with `relations`, and
  `--inc` is what fills them, so the reader is tested against the shape the service
  actually sends rather than the shape a search response has.

"""

from __future__ import annotations

import http.client
import io
import json
import time
import urllib.error
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from apps.builder import musicbrainz
from apps.builder.musicbrainz import (
    MAX_RETRY_WAIT,
    RETRY_WAIT,
    HttpMusicBrainzClient,
    MusicBrainzUnavailableError,
    Recording,
    Release,
    _lucene,
    _retry_after,
)


class FakeResponse(io.BytesIO):
    """A response object: readable, closable, usable as a context manager."""

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()


class FakeOpener:
    """A scripted musicbrainz.org.

    Running out of script is a mistake, not a success: an opener that answers `""` for every
    question after the last one scripted is a double that agrees with whatever it is asked, which
    is the #41 lesson. A repeat is now part of the protocol being tested, so a test that scripts
    one answer for two attempts would otherwise watch the second attempt *succeed*.
    """

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def __call__(self, request: Any, timeout: float = 0.0) -> Any:
        del timeout
        self.calls.append(str(request.full_url))
        if not self.responses:
            raise AssertionError(
                f"musicbrainz.org ran out of scripted answers after {len(self.calls)} "
                "requests; a repeat is a second request, not the end of one"
            )
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(json.dumps(outcome).encode())


def recording_payload(
    *,
    artist: str = "An Artist",
    title: str = "A Song",
    release: str = "An Album",
    date: str = "1999-05-06",
) -> dict[str, Any]:
    """What `/ws/2/recording/MBID?inc=recording-rels+release-rels` answers with."""

    return {
        "id": "recording-1",
        "title": title,
        "artist-credit": [{"name": artist, "joinphrase": ""}],
        "relations": [
            {
                "target-type": "release",
                "type": "part-of",
                "release": {"id": "release-1", "title": release, "date": date},
            },
            {
                "target-type": "artist",
                "type": "artist",
                "artist": {"id": "artist-1", "name": artist},
            },
        ],
    }


def search_payload(**kwargs: Any) -> dict[str, Any]:
    return {"recordings": [recording_payload(**kwargs)]}


def _http_error(code: int, headers: Mapping[str, str] | None = None) -> urllib.error.HTTPError:
    """An HTTPError with the headers argument its stubs insist on being."""

    message = http.client.HTTPMessage()
    for name, value in (headers or {}).items():
        message.add_header(name, value)
    return urllib.error.HTTPError("https://musicbrainz.org", code, "status", message, None)


def _refused(reason: str) -> urllib.error.URLError:
    """A transport failure, which is what `urllib` raises when there is no answer at all."""

    return urllib.error.URLError(reason)


def client(*responses: Any, minimum_interval: float = 0.0, **kwargs: Any) -> HttpMusicBrainzClient:
    return HttpMusicBrainzClient(
        opener=FakeOpener(*responses),
        minimum_interval=minimum_interval,
        retry_wait=0.0,
        **kwargs,
    )


# -- payloads -------------------------------------------------------------


def test_a_direct_lookup_reads_the_fields_the_builder_stores() -> None:
    service = client(recording_payload())
    recording = service.recording("recording-1")
    assert recording is not None
    assert (recording.recording_id, recording.artist, recording.album, recording.year) == (
        "recording-1",
        "An Artist",
        "An Album",
        "1999",
    )
    assert (recording.release_id, recording.artist_id) == ("release-1", "artist-1")


def test_a_search_reads_the_first_recording() -> None:
    service = client(search_payload())
    recording = service.search(artist="An Artist", release="An Album", track="A Song")
    assert recording is not None
    assert recording.title == "A Song"
    assert "limit=1" in service._opener.calls[0], "asking for one is the policy"


def test_an_empty_response_is_none_and_not_an_error() -> None:
    assert client({"recordings": []}).search(artist="Nobody", release="", track="Nothing") is None
    assert client({}).recording("unknown-mbid") is None


def test_a_release_without_a_date_leaves_the_year_absent() -> None:
    recording = client(recording_payload(date="")).recording("r")
    assert recording is not None
    assert recording.year == ""


def test_a_partial_date_is_read_as_a_year() -> None:
    """SAPRS 6.4 says dates are partial; MusicBrainz sends `2001-00-00` for a year only."""

    recording = client(recording_payload(date="2001-00-00")).recording("r")
    assert recording is not None
    assert recording.year == "2001"


def test_an_artist_credit_of_several_parts_keeps_the_join_phrase() -> None:
    """The attribution is the recording's own words, and enrichment must not invent a duet.

    Flattening `artist-credit` with its join phrases is what a "feat." credit is; the
    *grouping* artist comes from the artist relation, which is the one filed under.
    """

    payload = recording_payload()
    payload["artist-credit"] = [
        {"name": "Lead Artist", "joinphrase": " featuring "},
        {"name": "Guest", "joinphrase": ""},
    ]
    payload["relations"] = [
        {"target-type": "artist", "artist": {"id": "a-1", "name": "Lead Artist"}}
    ]
    recording = client(payload).recording("r")
    assert recording is not None
    assert recording.artist == "Lead Artist"
    assert recording.artist_id == "a-1"


def test_a_response_that_is_not_an_object_is_treated_as_malformed() -> None:
    def respond(request: Any, timeout: float = 0) -> FakeResponse:
        del request, timeout
        return FakeResponse(b"[1, 2, 3]")

    service = HttpMusicBrainzClient(opener=respond, minimum_interval=0)
    assert service.recording("r") is None


def test_a_payload_with_no_identifiable_recording_is_none() -> None:
    assert client({"title": ""}).recording("r") is None
    assert Recording().recording_id == ""


# -- failure vocabulary ---------------------------------------------------


def test_a_four_zero_four_is_a_miss_and_keeps_the_client_available() -> None:
    service = client(_http_error(404))
    assert service.recording("nope") is None
    assert service.available is True


def test_a_five_oh_three_is_asked_again_before_it_is_an_outage() -> None:
    # The busy page says "try again later", so this client does, and the answer arrives. #59's
    # whole story in one assertion: a refusal a repeat fixes used to be a failure, five failures
    # were an outage, and so a build over 481 albums stopped drawing covers after the third one.
    # The service was never unreachable. It was busy, which is a different sentence.
    service = client(_http_error(503), recording_payload())
    found = service.recording("r")
    assert found is not None
    assert found.recording_id == "recording-1"
    assert service.available is True, "a repeated question is not yet a failure"
    assert len(service._opener.calls) == 2, "one question, two attempts"


def test_a_busy_answer_that_refuses_twice_is_one_failure() -> None:
    service = client(_http_error(503), _http_error(503))
    with pytest.raises(MusicBrainzUnavailableError, match="503"):
        service.recording("r")
    assert service._failures == 1, "a repeated question costs the run one failure, not two"
    assert service.available is True, "one failure is not yet a pattern"


@pytest.mark.parametrize("code", [503, 429, 500, 502, 504])
def test_the_answers_that_mean_come_back_are_the_ones_repeated(code: int) -> None:
    service = client(_http_error(code), recording_payload())
    assert service.recording("r") is not None
    assert len(service._opener.calls) == 2


def test_a_stated_wait_is_honoured_and_bounded() -> None:
    # A rate limit is obeyed, and an unreasonable one is refused rather than slept through.
    # `retry-after: 0`, which is what musicbrainz.org's busy page actually sends, is a fact about
    # the gateway's window and not an invitation to fire the next request immediately.
    assert _retry_after(_http_error(503, {"Retry-After": "0"}), default=5.0) == 5.0
    assert _retry_after(_http_error(503, {"Retry-After": "12"}), default=5.0) == 12.0
    assert _retry_after(_http_error(503, {"Retry-After": "3600"}), default=5.0) == MAX_RETRY_WAIT
    assert _retry_after(_http_error(503), default=5.0) == 5.0
    assert _retry_after(_http_error(503, {"Retry-After": "not a number"}), default=5.0) == 5.0


def test_a_timeout_is_repeated_too_because_a_slow_answer_is_still_an_answer() -> None:
    service = client(TimeoutError("The read operation timed out"), recording_payload())
    assert service.recording("r") is not None
    assert len(service._opener.calls) == 2
    assert service._failures == 0


def test_a_connection_refused_is_an_outage_too() -> None:
    service = client(_refused("connection refused"), _refused("nope"))
    with pytest.raises(MusicBrainzUnavailableError, match="cannot reach"):
        service.search(artist="A", release="B", track="C")
    assert service._failures == 1


def test_a_success_resets_the_failure_count_to_zero() -> None:
    # `available` says "five in a row", and the counter had been counting a run (#59). Five blinks
    # spread over an hour-long build — exactly the thing the docstring was written to excuse —
    # switched enrichment off for the rest of the process, on a service that had answered nine
    # hundred questions in between.
    answers = [_refused("blink"), _refused("blink"), recording_payload()] * 4
    service = client(*answers)
    for _ in range(4):
        with pytest.raises(MusicBrainzUnavailableError):
            service.recording("r")
        assert service.recording("r") is not None
    assert service._failures == 0
    assert service.available is True


def test_malformed_json_is_an_outage_and_not_a_crash() -> None:
    def refuse(request: Any, timeout: float = 0) -> FakeResponse:
        del request, timeout
        return FakeResponse(b"<html>a proxy said no</html>")

    service = HttpMusicBrainzClient(opener=refuse, minimum_interval=0, retry_wait=0)
    with pytest.raises(MusicBrainzUnavailableError, match="malformed"):
        service.recording("r")


def test_five_failures_switch_enrichment_off_for_the_run() -> None:
    # `available` is the whole degradation policy, so it is worth its own test. Ten refusals,
    # five questions: the repeat is part of the policy now, and the sixth question is still
    # never asked.
    service = client(*[_refused("down") for _ in range(10)])
    for _ in range(5):
        with pytest.raises(MusicBrainzUnavailableError):
            service.recording("r")
    assert service.available is False
    assert len(service._opener.calls) == 10


def test_a_cap_stops_the_asking_without_an_error() -> None:
    service = client(*[search_payload() for _ in range(3)], max_requests=2)
    assert service.search(artist="A", release="B", track="C") is not None
    assert service.search(artist="A", release="B", track="C") is not None
    assert service.available is False
    assert service.search(artist="A", release="B", track="C") is None


def test_the_default_timeout_is_longer_than_the_slowest_answer_measured() -> None:
    """8 seconds was a guess, and the endpoints are not one speed.

    Measured on this library's own unanswered albums: a recording search at a 0.46 s median, a
    release search at 5.3 s with a tail past 45, a lookup by identifier at a 13.7 s median, and one
    Cover Art Archive `front` in 20.9 s that arrived perfectly well. A timeout shorter than the
    slowest of those is an outage the caller cannot distinguish from a real one, which is half of
    what #59's report labelled "unavailable" (#59).
    """

    assert HttpMusicBrainzClient()._timeout >= 45.0


def test_requests_are_spaced_at_a_second_apart() -> None:
    """SAPRS 12.4's rate limit, measured as elapsed time rather than as a constant."""

    service = client({"recordings": []}, {"recordings": []}, minimum_interval=0.05)
    started = time.monotonic()
    service.search(artist="A", release="B", track="one")
    service.search(artist="A", release="B", track="two")
    assert time.monotonic() - started >= 0.04


# -- the second host (#57) -------------------------------------------------


class RawOpener:
    """An opener that answers with bodies rather than JSON, which is what an image host sends.

    `FakeOpener` is the right fake for the web service and the wrong one for the archive: it
    serialises whatever it is given, so a test of `cover` would be handed `{}` and conclude that
    a JPEG can be an empty dict.
    """

    def __init__(self, *bodies: bytes | Mapping[str, Any] | Sequence[Any] | Exception) -> None:
        self.bodies = list(bodies)
        self.requests: list[Any] = []

    def __call__(self, request: Any, timeout: float = 0.0) -> FakeResponse:
        del timeout
        self.requests.append(request)
        if not self.bodies:
            raise AssertionError(
                f"the archive ran out of scripted bodies after {len(self.requests)} requests"
            )
        body = self.bodies.pop(0)
        if isinstance(body, Exception):
            raise body
        if isinstance(body, bytes):
            return FakeResponse(body)
        return FakeResponse(json.dumps(body).encode())


def raw_client(
    *bodies: bytes | Mapping[str, Any] | Sequence[Any] | Exception, **kwargs: Any
) -> tuple[HttpMusicBrainzClient, RawOpener]:
    opener = RawOpener(*bodies)
    return (
        HttpMusicBrainzClient(
            opener=opener, minimum_interval=0, retry_wait=kwargs.pop("retry_wait", 0.0), **kwargs
        ),
        opener,
    )


def release_payload(*releases: tuple[str, str, str]) -> dict[str, Any]:
    """`/ws/2/release?query=...` answered with `(id, title, artist)` per release."""

    return {
        "releases": [
            {
                "id": release_id,
                "title": title,
                "artist-credit": [{"name": artist, "joinphrase": ""}],
            }
            for release_id, title, artist in releases
        ]
    }


def test_a_release_search_reads_the_two_names_albumart_compares() -> None:
    service = client(
        release_payload(("rel-1", "Random Access Memories", "Daft Punk")),
        release_payload(),
    )
    found = service.releases(artist="Daft Punk", album="Random Access Memories")
    assert found == (
        Release(release_id="rel-1", title="Random Access Memories", artist="Daft Punk"),
    )
    url = service._opener.calls[0]
    assert "musicbrainz.org/ws/2/release" in url
    assert "query=release%3A%22Random" in url, "the album is quoted, not tokenised"


def test_a_release_search_asks_for_a_short_list_because_ranking_is_not_matching() -> None:
    """Five candidates, and the stage that reads them takes the first that is *this* album.

    One result is how a client ends up trusting a fuzzy hit: MusicBrainz returns a best guess for
    a query nobody meant, and with a limit of 1 the guess is the only candidate there is.
    """

    service = client(release_payload())
    service.releases(artist="A", album="B")
    assert "limit=5" in service._opener.calls[0]


def test_a_release_document_with_no_id_is_not_a_candidate() -> None:
    service = client({"releases": [{"title": "An Album", "artist-credit": "An Artist"}]})
    assert service.releases(artist="A", album="An Album") == ()


def test_the_cover_is_asked_of_the_archive_and_not_of_the_web_service() -> None:
    """A different host, with a different `Accept`, through the same client.

    The guardrail is that this module is the only thing in Encore that opens a socket, so the
    archive's URL lives here rather than in the stage that wants the picture — which is also why
    an architecture test can say so instead of a reviewer having to remember it (#57).
    """

    service, opener = raw_client(b"\xff\xd8jpeg")
    assert service.cover("rel-1") == b"\xff\xd8jpeg"
    request = opener.requests[0]
    assert str(request.full_url) == "https://coverartarchive.org/release/rel-1/front"
    assert request.get_header("Accept") == "image/*", "an image was asked for"

    search = client({"releases": []})
    search.releases(artist="A", album="B")
    assert "musicbrainz.org/ws/2/release" in search._opener.calls[0]
    assert "coverartarchive" not in search._opener.calls[0]


def test_an_empty_release_id_asks_the_archive_for_nothing() -> None:
    """`/release//front` is a 404 that costs a request and means "we never had an id".

    The stage calls this for an album it could not name; refusing quietly is what keeps a policy
    mistake in a caller from showing up as an outage in the report.
    """

    service, opener = raw_client()
    assert service.cover("") is None
    assert opener.requests == []


def test_a_cover_too_large_to_be_a_cover_is_not_a_cover(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same length-decides rule as a JSON body, because a `Content-Length` is a promise.

    Nine megabytes of something is not an album sleeve, and the cache would reduce it to 640 px
    anyway. The way to survive an image host that ignores the request is to read one byte past
    the limit and stop, which is what `limit + 1` is for.
    """

    monkeypatch.setattr(musicbrainz, "_MAX_COVER_BYTES", 16)
    service, _ = raw_client(bytes(17))
    assert service.cover("rel-1") is None


def test_a_missing_cover_is_a_miss_and_not_an_outage() -> None:
    """Most MusicBrainz releases have no front image.

    Treating that as a failure would switch cover art off for the whole run at the first plain
    album, and the report would say the archive was unreachable when all it did was answer.
    """

    service = client(_http_error(404))
    assert service.cover("rel-1") is None
    assert service.available is True


def test_the_wait_when_the_service_said_nothing_is_the_policy_not_the_spacing() -> None:
    """The floor is a number someone chose, so it is written where a reader can disagree.

    5 s, not the 1 s ordinary spacing: a repeat one interval later is the same question asked
    louder. The refusals arrive in stretches rather than one at a time — 16 of 24 release searches
    at ten past ten, none of 189 at twenty past eleven, at the same pacing both — so what a short
    floor buys is a second failure on the same busy minute, and five questions in a row is what
    `available` counts (#59).
    """

    assert _retry_after(_http_error(503), default=RETRY_WAIT) == RETRY_WAIT
    assert RETRY_WAIT > 1.0
    assert MAX_RETRY_WAIT > RETRY_WAIT


def test_the_two_hosts_share_one_rate_limit() -> None:
    """Pacing is per client, not per URL, because the courtesy is per client.

    musicbrainz.org's policy and the archive's are both stated against the user agent, and a
    build that interleaved two unmetered streams would be one burst of 926 requests. This is the
    test that makes default-on enrichment safe to enable on a shared connection: the same second
    between a release search and its cover fetch as between two searches.
    """

    opener = RawOpener(release_payload(), b"\xff\xd8jpeg")
    service = HttpMusicBrainzClient(opener=opener, minimum_interval=0.05)
    started = time.monotonic()
    service.releases(artist="A", album="B")
    service.cover("rel-1")
    assert time.monotonic() - started >= 0.04


# -- the query the service actually receives ------------------------------


def test_a_search_query_names_the_fields_it_knows() -> None:
    service = client({"recordings": []})
    service.search(artist="An Artist", release="An Album", track="A Song")
    query = service._opener.calls[0]
    assert "recording" in query
    assert "artist" in query
    assert "release" in query


def test_an_empty_query_is_not_sent() -> None:
    """A search with nothing in it matches everything, which would be a wrong answer."""

    service = client()
    assert service.search(artist="", release="", track="") is None
    assert service._opener.calls == []


@pytest.mark.parametrize(
    ("value", "quoted"),
    [
        ("Simple", True),
        ('With "quotes"', True),
        ("Back\\slash", True),
        ("A:B", True),
    ],
)
def test_lucene_metacharacters_are_quoted_not_interpreted(value: str, quoted: bool) -> None:
    """`recording:Love And Happiness` is fine; `recording:A:B` is a field name.

    Tag values are arbitrary text, and an unescaped colon or quote changes the query
    rather than the answer — the kind of bug that shows up as "one album cannot be found"
    and never as an error.
    """

    clause = _lucene("recording", value)
    assert clause.startswith("recording:")
    body = clause.removeprefix("recording:")
    assert body.startswith('"') is quoted


def test_a_recording_with_no_relations_still_carries_its_identifiers() -> None:
    payload = {"id": "r-9", "title": "Orphan", "artist-credit": [{"name": "Solo"}]}
    recording = client(payload).recording("r-9")
    assert recording is not None
    assert (recording.recording_id, recording.release_id, recording.album) == ("r-9", "", "")


def test_the_default_construction_is_the_disabled_one() -> None:
    """`DisabledClient` is the pipeline's default; nothing here may open a socket by accident.

    Asserted by importing the default and checking `available`, because the difference
    between a build that asks and a build that does not is a difference of minutes, and it
    should be a flag rather than an oversight.
    """

    from apps.builder.musicbrainz import DisabledClient

    assert DisabledClient().available is False
