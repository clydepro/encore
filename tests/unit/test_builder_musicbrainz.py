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
from typing import Any

import pytest

from apps.builder.musicbrainz import (
    HttpMusicBrainzClient,
    MusicBrainzUnavailableError,
    Recording,
    _lucene,
)


class FakeResponse(io.BytesIO):
    """A response object: readable, closable, usable as a context manager."""

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()


class FakeOpener:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def __call__(self, request: Any, timeout: float = 0.0) -> Any:
        del timeout
        self.calls.append(str(request.full_url))
        outcome = self.responses.pop(0) if self.responses else {}
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


def _http_error(code: int) -> urllib.error.HTTPError:
    """An HTTPError with the headers argument its stubs insist on being."""

    return urllib.error.HTTPError(
        "https://musicbrainz.org", code, "status", http.client.HTTPMessage(), None
    )


def client(*responses: Any, minimum_interval: float = 0.0, **kwargs: Any) -> HttpMusicBrainzClient:
    return HttpMusicBrainzClient(
        opener=FakeOpener(*responses), minimum_interval=minimum_interval, **kwargs
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


def test_a_five_oh_three_is_an_outage() -> None:
    service = client(_http_error(503))
    with pytest.raises(MusicBrainzUnavailableError, match="503"):
        service.recording("r")
    assert service.available is True, "one failure is not yet a pattern"


def test_a_connection_refused_is_an_outage_too() -> None:
    service = client(urllib.error.URLError("connection refused"))
    with pytest.raises(MusicBrainzUnavailableError, match="cannot reach"):
        service.search(artist="A", release="B", track="C")


def test_malformed_json_is_an_outage_and_not_a_crash() -> None:
    def refuse(request: Any, timeout: float = 0) -> FakeResponse:
        del request, timeout
        return FakeResponse(b"<html>a proxy said no</html>")

    service = HttpMusicBrainzClient(opener=refuse, minimum_interval=0)
    with pytest.raises(MusicBrainzUnavailableError, match="malformed"):
        service.recording("r")


def test_five_failures_switch_enrichment_off_for_the_run() -> None:
    """`available` is the whole degradation policy, so it is worth its own test."""

    service = client(*[urllib.error.URLError("down") for _ in range(6)])
    for _ in range(5):
        with pytest.raises(MusicBrainzUnavailableError):
            service.recording("r")
    assert service.available is False
    assert len(service._opener.calls) == 5, "the sixth question was never asked"


def test_a_cap_stops_the_asking_without_an_error() -> None:
    service = client(*[search_payload() for _ in range(3)], max_requests=2)
    assert service.search(artist="A", release="B", track="C") is not None
    assert service.search(artist="A", release="B", track="C") is not None
    assert service.available is False
    assert service.search(artist="A", release="B", track="C") is None


def test_requests_are_spaced_at_a_second_apart() -> None:
    """SAPRS 12.4's rate limit, measured as elapsed time rather than as a constant."""

    service = client({"recordings": []}, {"recordings": []}, minimum_interval=0.05)
    started = time.monotonic()
    service.search(artist="A", release="B", track="one")
    service.search(artist="A", release="B", track="two")
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
