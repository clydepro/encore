"""Regression: #59 — one refused MusicBrainz request ended the run's asking, and a build over a
real library published no covers and blamed the network.

Reproduced on the appliance, with the code of #57 in place and the flags left alone:

```console
$ sudo encore-builder
  Artwork            0  images written to the cache (222 in this library)
  Songs           3040  rows in library.db
  Artists          484   ·   Albums     703
  MusicBrainz: unavailable; what the last build knew was reused
  Cover art:   archive unavailable
  Build: full, 24.8s
```

24.8 seconds is the accusation. At the client's own one-request-per-second policy, 481 needy
albums are roughly 960 questions, which is sixteen minutes; the build asked two or three and then
stopped talking to the internet. Four things were wrong, and each had a sentence in the code
saying the opposite:

* `enrichment._lookup` answered "the service is down" for one refused request and `enrich`
  believed it, so the remaining tracks were never asked. `albumart.fetch` did the same with
  `break`. The client's own `available` — "five in a row, because a single dropped request in a
  20-minute build is the internet" — was never consulted, because each stage had a stricter policy
  of its own, and the stricter one wins.
* `HTTP 503`, the page that says *"The MusicBrainz web server is currently busy. Please try again
  later."*, was counted as a failure rather than as an invitation. Measured over this library's
  own unidentified albums: 19 of 24 release searches came back unusable, 16 of them that way, and
  the same 24 asked once every three seconds failed 16 times, with no `HTTP 429` in either sample.
  Somebody else's search service having a busy hour, then — not this client's arithmetic, not the
  published rate limit, and not an outage.
* `_failures` counted a run instead of a row, so `available`'s "in a row" was untrue and the
  patience that sentence describes could not be spent.
* The reason — `EnrichmentOutcome.notes`, "MusicBrainz unavailable: … timed out" — was written by
  the stage and read by nothing. #52's bug class, one layer up: a field with a producer and no
  consumer, on the one line an operator uses to decide whether to care.

The doubles here therefore refuse a *named question a stated number of times*. #43 is the
precedent for why that matters: a double that can only describe the states the code already
handles cannot test the state it gets wrong, and until this file nothing in the suite could say
"refused once, then answered".
"""

from __future__ import annotations

import http.client
import io
import json
import sqlite3
import urllib.error
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from apps.builder.albumart import fetch
from apps.builder.enrichment import enrich
from apps.builder.musicbrainz import (
    HttpMusicBrainzClient,
    MusicBrainzUnavailableError,
    Recording,
    Release,
)
from apps.builder.pipeline import BuildOptions, run
from apps.builder.precedence import normalize_track, tree_map
from apps.builder.records import DiscoveredFile, ExtractedFile
from apps.builder.report import BuildReport
from encore.domain import AudioFormat
from tests.support.media import AlbumSpec, MediaSpec, cover_image, generate_albums

pytestmark = pytest.mark.regression

_ALBUMS = ("Ambient", "Discovery", "Houses")


class Service:
    """MusicBrainz answering from dictionaries, and refusing in the two ways it really does.

    Attributes:
        recordings: Track title → answer, for the enrichment half.
        catalog / images: What an album is named by, and what its front looks like.
        refuse_search / refuse_releases / refuse_covers: Question → how many times it comes back
            "busy". A count of one is the blink that #59 died of; a count of two is the album the
            service will not answer this run, because an album gets a second chance and then stops
            being asked. An empty name list answers everything.
        gone_from: Request number from which the service has stopped answering at all. This is an
            outage, and it is the only thing allowed to end a build's asking: `available` goes
            false with it, which is what the stages now read instead of their own alarm.
    """

    def __init__(
        self,
        *,
        recordings: Mapping[str, Recording] | None = None,
        catalog: Mapping[str, Sequence[Release]] | None = None,
        images: Mapping[str, bytes] | None = None,
        refuse_search: Mapping[str, int] | None = None,
        refuse_releases: Mapping[str, int] | None = None,
        refuse_covers: Mapping[str, int] | None = None,
        gone_from: int | None = None,
    ) -> None:
        self.recordings = recordings or {}
        self.catalog = catalog or {}
        self.images = images or {}
        self.refuse: dict[str, dict[str, int]] = {
            "search": dict(refuse_search or {}),
            "releases": dict(refuse_releases or {}),
            "cover": dict(refuse_covers or {}),
        }
        self.gone_from = gone_from
        self.asked: list[str] = []

    @property
    def available(self) -> bool:
        return self.gone_from is None or len(self.asked) < self.gone_from

    def _question(self, kind: str, name: str) -> None:
        """Ask the service, or refuse this particular question once more if it has refusals left."""

        self.asked.append(kind)
        if not self.available:
            raise MusicBrainzUnavailableError("simulated outage")
        left = self.refuse[kind].get(name, 0)
        if left:
            self.refuse[kind][name] = left - 1
            raise MusicBrainzUnavailableError("HTTP 503 from https://musicbrainz.org/ws/2/release")

    def recording(self, recording_id: str) -> Recording | None:  # noqa: ARG002
        return None

    def search(self, *, artist: str, release: str, track: str) -> Recording | None:
        del artist, release
        self._question("search", track)
        return self.recordings.get(track.lower())

    def releases(self, *, artist: str, album: str) -> Sequence[Release]:
        del artist
        self._question("releases", album)
        return self.catalog.get(album, ())

    def cover(self, release_id: str) -> bytes | None:
        self._question("cover", release_id)
        return self.images.get(release_id)


def _archive(**overrides: Any) -> Service:
    """A service that knows all three of this suite's albums, and draws a different cover each.

    The colours matter: the cache is content-addressed (#51), so three identical images are one
    `artwork` row, and a test that counts rows would be counting deduplication rather than covers.
    """

    return Service(
        catalog={
            album: (Release(release_id=f"rel-{album}", title=album, artist="An Artist"),)
            for album in _ALBUMS
        },
        images={
            f"rel-{album}": cover_image(color=(20 + 60 * index, 120 - 20 * index, 60))
            for index, album in enumerate(_ALBUMS)
        },
        **overrides,
    )


def _track(root: Path, name: str, *, title: str, album: str | None = "An Album") -> Any:
    """A track as a stage receives it: normalized, grouped, with the album tag asked about."""

    tags: dict[str, str | None] = {"artist": "An Artist", "title": title, "album": album}
    extracted = ExtractedFile(
        file=DiscoveredFile(path=root / name, format=AudioFormat.MP3, size_bytes=100, mtime_ns=1),
        tags={key: value for key, value in tags.items() if value is not None},
        duration=timedelta(seconds=1),
    )
    return normalize_track(extracted, root=root, trees=tree_map([extracted], root))


def _tracks(root: Path) -> list[Any]:
    return [_track(root, f"{album}.mp3", title=f"{album} Song", album=album) for album in _ALBUMS]


def _options(home: Path, music: Path, **overrides: object) -> BuildOptions:
    return BuildOptions(
        music_dir=music,
        library_db=home / "library.db",
        artwork_dir=home / "artwork",
        temp_dir=home / "tmp",
        library_version="regression-59",
        **overrides,  # type: ignore[arg-type]
    )


def _build(home: Path, music: Path, **overrides: object) -> BuildReport:
    report = run(_options(home, music, **overrides))
    assert report.published_to is not None, report.text()
    return report


def _artwork_rows(report: BuildReport) -> int:
    assert report.published_to is not None
    with sqlite3.connect(f"file:{report.published_to}?mode=ro", uri=True) as connection:
        return int(connection.execute("select count(*) from artwork").fetchone()[0])


def _flac_library(root: Path, *, gap: bool = False) -> Path:
    """Three albums whose files carry no picture, which is what a FLAC library looks like.

    `gap` takes the album tag off the first track of each, which is the shape that makes the
    enrichment stage ask a question rather than read a tag.
    """

    music = root / "music"
    music.mkdir(parents=True, exist_ok=True)
    generate_albums(
        [
            AlbumSpec(
                artist="An Artist",
                album=album,
                tracks=tuple(
                    MediaSpec(
                        title=f"{album} Song {number}",
                        artist="An Artist",
                        album=None if gap and number == 1 else album,
                        track_number=number,
                        fmt="flac",
                    )
                    for number in (1, 2)
                ),
            )
            for album in _ALBUMS
        ],
        music,
    )
    return music


# -- the stages: a refusal is one question, not a verdict -------------------


def test_one_refused_question_does_not_end_the_enriching(tmp_path: Path) -> None:
    """The build asks three times and gets two answers. Before #59 it asked once, ever."""

    service = Service(
        recordings={
            "first": Recording(album="Found One"),
            "second": Recording(album="Found Two"),
            "third": Recording(album="Found Three"),
        },
        refuse_search={"Second": 1},
    )
    tracks = [
        _track(tmp_path, f"{name}.mp3", title=name, album=None)
        for name in ("First", "Second", "Third")
    ]
    outcome = enrich(tracks, service)
    assert outcome.unavailable is False, "a service that answered afterwards was not unavailable"
    assert outcome.failed == 1, "and the blink is still counted, because it happened"
    assert [track.album for track in outcome.tracks] == ["Found One", None, "Found Three"]


def test_a_service_that_has_stopped_answering_does_end_the_asking(tmp_path: Path) -> None:
    """The other half of #57's promise, unchanged: a dead service is not retried 3,000 times."""

    service = Service(recordings={"first": Recording(album="Never")}, gone_from=2)
    tracks = [
        _track(tmp_path, f"{index}.mp3", title=f"Song {index}", album=None) for index in range(6)
    ]
    outcome = enrich(tracks, service)
    assert outcome.unavailable is True
    assert len(service.asked) == 2, "asking a service that has stopped is not persistence"


def test_one_refused_album_does_not_end_the_covers(tmp_path: Path) -> None:
    """Three needy albums, one busy answer: three covers, not one."""

    outcome = fetch(_tracks(tmp_path), _archive(refuse_covers={"rel-Ambient": 1}))
    assert (outcome.covered, outcome.unanswered, outcome.unavailable) == (3, 0, False)
    assert len(outcome.payloads) == 3, "an album whose second question was refused is not grey"


def test_an_album_refused_twice_is_counted_as_refused_not_as_absent(tmp_path: Path) -> None:
    """The count that closes the arithmetic, and the bound that keeps the pass finite.

    "Not named exactly" and "with no image" are claims about the album; an album the service never
    answered is a claim about the afternoon. Before #59 the second kind was in none of the
    columns, which is how a run reported 481 candidates, 2 requests, and nothing to account for
    the difference between them.
    """

    outcome = fetch(_tracks(tmp_path), _archive(refuse_covers={"rel-Ambient": 2}))
    assert (outcome.covered, outcome.unanswered, outcome.unmatched, outcome.uncovered) == (
        2,
        1,
        0,
        0,
    )
    assert outcome.unavailable is False, "the service answered about the other albums, twice each"
    assert outcome.not_reached == 0
    assert outcome.albums == (
        outcome.covered
        + outcome.unmatched
        + outcome.uncovered
        + outcome.unanswered
        + outcome.not_reached
    ), "every candidate landed in exactly one column, which is the arithmetic #59 lost"


def test_a_run_that_stops_says_what_is_left(tmp_path: Path) -> None:
    """An outage mid-pass is an outage, and the albums after it are not silently absent."""

    outcome = fetch(_tracks(tmp_path), _archive(gone_from=3))
    assert outcome.unavailable is True
    assert outcome.covered == 1
    assert outcome.not_reached == 2, "two albums were never asked about, and the report says so"


def test_a_run_that_stops_early_still_reports_what_it_covered(tmp_path: Path) -> None:
    """The sentence has to be true of the build, not only of its last minute.

    #59's report said `Cover art: archive unavailable` over 307 covers it had fetched and written,
    because one flag carried both "nothing was answered" and "the asking stopped". The first is an
    outage and the second is a count, and an operator deciding whether to rebuild needs the
    difference. "Unavailable" alone now means nothing was asked at all (#57's case, which still
    holds).
    """

    report = _build(
        tmp_path / "home",
        _flac_library(tmp_path),
        enrich=True,
        album_art=True,
        client=_archive(gone_from=3),
    )
    assert "1 of 3 albums without embedded art" in report.album_art, report.album_art
    assert "2 left to ask" in report.album_art, report.album_art
    assert "unavailable" not in report.album_art.lower(), report.album_art
    assert _artwork_rows(report) == 1, "the cover that was answered was published"


# -- the transport: a busy answer is a second chance, and a row is a row -----


def test_a_busy_answer_is_asked_again_and_is_not_a_failure() -> None:
    service = _scripted(_http(503), {"releases": []})
    assert service.releases(artist="An Artist", album="An Album") == ()
    assert service.available is True
    assert len(_calls(service)) == 2, "one question, two attempts"


def test_a_busy_answer_that_refuses_twice_is_one_failure() -> None:
    service = _scripted(_http(503), _http(503))
    with pytest.raises(MusicBrainzUnavailableError, match="503"):
        service.releases(artist="An Artist", album="An Album")
    assert service._failures == 1, "a repeated question costs the run one failure, not two"


def test_five_busy_minutes_in_a_row_are_an_outage_but_a_hundred_spread_out_are_not() -> None:
    """`available` counts a row. It was counting a run, which is a different word."""

    service = _scripted(*([_http(503), _http(503), {"releases": []}] * 6))
    for _ in range(6):
        with pytest.raises(MusicBrainzUnavailableError):
            service.releases(artist="A", album="B")
        service.releases(artist="A", album="C")
    assert service._failures == 0
    assert service.available is True


# -- the report: the reason, and the two sentences that used to be one ------


def test_the_report_says_why_musicbrainz_was_unavailable(tmp_path: Path) -> None:
    """#52's bug class, one layer up: the stage recorded the sentence and nobody read it."""

    report = _build(
        tmp_path / "home",
        _flac_library(tmp_path, gap=True),
        enrich=True,
        album_art=False,
        client=Service(gone_from=1),
    )
    assert report.musicbrainz.startswith("unavailable"), report.musicbrainz
    assert any("simulated outage" in line for line in report.warnings), report.warnings


def test_an_offline_build_does_not_accuse_the_archive(tmp_path: Path) -> None:
    """`--no-musicbrainz` is not an outage, and the warnings used to say that it was.

    #57 put that rule in `_cover_sentence` — off is checked before the archive's own state — and
    left it out of `_warnings`. #44's shape: one rule stated in two of three places, and the place
    that missed it is the one an operator reads when something looks wrong.
    """

    report = _build(
        tmp_path / "home",
        _flac_library(tmp_path),
        enrich=False,
        album_art=True,
        client=Service(gone_from=1),
    )
    assert report.album_art == "off, with MusicBrainz", report.album_art
    assert not [line for line in report.warnings if "Archive" in line], report.warnings


def test_a_build_whose_service_blinked_still_publishes_the_covers(tmp_path: Path) -> None:
    """The appliance symptom in one test: three needy albums, one busy answer, three pictures.

    Before #59 this published zero `artwork` rows over a library that had every reason to hold
    three, and its report said the archive was unavailable.
    """

    report = _build(
        tmp_path / "home",
        _flac_library(tmp_path),
        enrich=True,
        album_art=True,
        client=_archive(refuse_covers={"rel-Ambient": 1}),
    )
    assert _artwork_rows(report) == 3, report.text()
    assert "unavailable" not in report.album_art, report.album_art
    assert report.album_art.startswith("3 of 3 albums"), report.album_art


# -- the scripted transport -------------------------------------------------


def _http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://musicbrainz.org", code, "status", http.client.HTTPMessage(), None
    )


class _Response(io.BytesIO):
    """A response object: readable, closable, usable as a context manager."""

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()


class _Script:
    """An opener that answers a list, and refuses to invent an answer of its own (#41)."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    def __call__(self, request: Any, timeout: float = 0.0) -> Any:
        del timeout
        self.calls.append(str(request.full_url))
        index = len(self.calls) - 1
        if index >= len(self.outcomes):
            raise AssertionError(
                f"the scripted service ran out of answers after {len(self.calls)} requests; "
                "a repeat is a second request, not the end of one"
            )
        outcome = self.outcomes[index]
        if isinstance(outcome, Exception):
            raise outcome
        return _Response(json.dumps(outcome).encode())


def _scripted(*outcomes: Any) -> HttpMusicBrainzClient:
    return HttpMusicBrainzClient(opener=_Script(*outcomes), minimum_interval=0.0, retry_wait=0.0)


def _calls(service: HttpMusicBrainzClient) -> Sequence[str]:
    opener: _Script = service._opener
    return opener.calls
