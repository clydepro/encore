"""Enrichment, artwork and duplicates: the stages that can fail and must not (6.6, 6.7).

Three stages, one shared requirement from ADR-010: an outcome rather than an exception.
MusicBrainz is down, a cover image is corrupt, a song appears twice — none of those is a
reason a jukebox cannot be built, and each test here is the assertion that the pipeline
kept going and said what happened.

Enrichment also carries the one rule in the Builder that is about honesty rather than
robustness: SAPRS 6.6's "shall not be degraded to un-enriched data". A file the previous
build resolved keeps its answer even when this build cannot reach the network, because
losing a corrected artist name to an outage is worse than a slow build.
"""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from PIL import Image

from apps.builder.artwork import ArtworkLimits, generate, store_one
from apps.builder.duplicates import analyse
from apps.builder.enrichment import PreviousEnrichment, enrich
from apps.builder.musicbrainz import (
    DisabledClient,
    MusicBrainzUnavailableError,
    Recording,
)
from apps.builder.normalization import NORMALIZATION_RULES_VERSION
from apps.builder.precedence import normalize_track, tree_map
from apps.builder.records import DiscoveredFile, ExtractedFile, Track
from encore.domain import AudioFormat
from encore.repositories.contract import MetadataField, MetadataSource


class FakeClient:
    """A MusicBrainz that answers what a test says it answers, and counts the asking."""

    def __init__(
        self,
        answers: dict[str, Recording | None] | None = None,
        *,
        by_id: dict[str, Recording] | None = None,
        outage: bool = False,
    ) -> None:
        self.answers = answers or {}
        self.by_id = by_id or {}
        self.outage = outage
        self.searches: list[tuple[str, str, str]] = []

    @property
    def available(self) -> bool:
        return not self.outage

    def recording(self, recording_id: str) -> Recording | None:
        if self.outage:
            raise MusicBrainzUnavailableError("simulated outage")
        return self.by_id.get(recording_id)

    def search(self, *, artist: str, release: str, track: str) -> Recording | None:
        self.searches.append((artist, release, track))
        if self.outage:
            raise MusicBrainzUnavailableError("simulated outage")
        return self.answers.get(track.lower())


def _track(
    root: Path,
    name: str = "a.mp3",
    *,
    artist: str = "Tagged Artist",
    title: str = "Tagged Title",
    album: str | None = "Tagged Album",
    size: int = 100,
    seconds: float = 1.0,
    artwork: bytes | None = None,
    date: str | None = "2001",
    **extra: str,
) -> Track:
    raw: dict[str, str | None] = {
        "artist": artist,
        "title": title,
        "album": album,
        "date": date,
        **extra,
    }
    tags = {key: value for key, value in raw.items() if value is not None}
    path = root / name
    extracted = ExtractedFile(
        file=DiscoveredFile(path=path, format=AudioFormat.MP3, size_bytes=size, mtime_ns=1),
        tags=tags,
        duration=timedelta(seconds=seconds),
        embedded_artwork=artwork,
    )
    return normalize_track(extracted, root=root, trees=tree_map([extracted], root))


# -- the client boundary --------------------------------------------------


def test_the_disabled_client_is_the_default_and_never_answers() -> None:
    client = DisabledClient()
    assert client.available is False
    assert client.search(artist="A", release="B", track="C") is None
    assert client.recording("mbid") is None


def test_a_recording_carries_the_fields_the_builder_uses() -> None:
    recording = Recording(recording_id="r", album="A", artist="B", year="2001")
    assert (recording.recording_id, recording.album, recording.artist, recording.year) == (
        "r",
        "A",
        "B",
        "2001",
    )


# -- enrichment -----------------------------------------------------------


def test_enrichment_fills_only_the_gap(tmp_path: Path) -> None:
    """SAPRS 6.6 says "fill gaps", so a tag that exists is never overruled here."""

    track = _track(tmp_path, artist="Real Artist", title="Needs Album", album=None)
    answer = Recording(album="Found Album", artist="Different Artist")
    outcome = enrich([track], FakeClient({"needs album": answer}))
    assert outcome.tracks[0].album == "Found Album"
    assert outcome.tracks[0].artist == "Real Artist", "an existing tag outranks a search hit"
    assert outcome.tracks[0].provenance["album"].source == MetadataSource.MUSICBRAINZ
    assert (outcome.repaired, outcome.looked_up) == (1, 1)


def test_a_date_is_never_a_reason_to_query(tmp_path: Path) -> None:
    """The rule that keeps an enriched build minutes rather than an hour.

    Measured: over a third of the corpus's MP3s have no date tag, so "date missing" is
    not a gap that distinguishes one file from another — and MusicBrainz is polled at
    one request per second by ADR-010. The year is filled whenever something else asks
    the question, and never on its own.
    """

    client = FakeClient({"Tagged Title": Recording(year="1999")})
    outcome = enrich([_track(tmp_path, date=None)], client)
    assert client.searches == []
    assert outcome.looked_up == 0

    with_a_gap = enrich(
        [_track(tmp_path, album=None, date=None)],
        FakeClient({"tagged title": Recording(year="1999", album="Found")}),
    )
    assert with_a_gap.tracks[0].date == "1999", "and it does come along for free"
    assert client.searches == []
    assert (outcome.repaired, outcome.looked_up) == (0, 0)


def test_a_track_with_no_artist_is_not_searched(tmp_path: Path) -> None:
    """A search with a blank artist matches anything, and matching anything is wrong."""

    client = FakeClient({"orphan": Recording(album="Guessed")})
    outcome = enrich([_track(tmp_path, artist="", title="Orphan", album=None)], client)
    assert client.searches == []
    assert outcome.tracks[0].album is None


def test_an_outage_leaves_the_build_intact(tmp_path: Path) -> None:
    """The pipeline keeps going and the report says why nothing was filled in."""

    client = FakeClient(outage=True)
    outcome = enrich([_track(tmp_path, album=None)], client)
    assert outcome.unavailable is True
    assert outcome.tracks[0].title == "Tagged Title"
    assert any("unavailable" in note.lower() for note in outcome.notes)


def test_only_the_first_query_is_attempted_once_the_service_is_down(tmp_path: Path) -> None:
    """3,000 tracks x a timeout each is a build that never finishes.

    ADR-010's stage contract says a failing source degrades and reports; the report
    carries the note, and the remaining files take the reuse path instead of the
    network path.
    """

    client = FakeClient(outage=True)
    tracks = [
        _track(tmp_path, name=f"{index}.mp3", album=None, title=f"T{index}") for index in range(5)
    ]
    outcome = enrich(tracks, client)
    assert len(client.searches) == 1
    assert outcome.looked_up == 0


def test_enrichment_reuses_the_previous_build(tmp_path: Path) -> None:
    """SAPRS 6.6's "not degraded to un-enriched data", proven with the network off."""

    track = _track(tmp_path, artist="Known Artist", title="Known Song", album=None)
    cache = {track.path: PreviousEnrichment(album="Resolved Album", release_id="rel-9")}
    outcome = enrich([track], DisabledClient(), previous=cache)
    assert outcome.tracks[0].album == "Resolved Album"
    assert (outcome.reused, outcome.looked_up) == (1, 0)
    assert outcome.tracks[0].provenance["album"].source == MetadataSource.MUSICBRAINZ


def test_a_recording_id_is_looked_up_instead_of_searched(tmp_path: Path) -> None:
    """The reason identifiers are stored at all: next time, ask directly."""

    track = _track(tmp_path, album=None)
    track = replace(track, musicbrainz_recording_id="mbid-1")
    outcome = enrich([track], FakeClient(by_id={"mbid-1": Recording(album="By Identifier")}))
    assert outcome.tracks[0].album == "By Identifier"


def test_identifiers_come_along_with_the_values(tmp_path: Path) -> None:
    track = _track(tmp_path, artist="A Person", title="Found Song", album=None)
    recording = Recording(
        recording_id="rec-5",
        release_id="rel-5",
        artist_id="mbid-5",
        artist="An Artist",
        album="An Album",
    )
    outcome = enrich([track], FakeClient({"found song": recording}))
    result = outcome.tracks[0]
    assert result.musicbrainz_artist_id == "mbid-5"
    assert result.musicbrainz_release_id == "rel-5"


def test_a_lookup_with_no_answer_is_reported_and_not_invented(tmp_path: Path) -> None:
    outcome = enrich([_track(tmp_path, artist="A Person", album=None)], FakeClient({}))
    assert outcome.tracks[0].album is None
    assert (outcome.repaired, outcome.unavailable) == (0, False)


def test_a_placeholder_answer_is_not_an_answer(tmp_path: Path) -> None:
    """MusicBrainz says `[unknown]` too, and writing it into the library loses the gap."""

    track = _track(tmp_path, artist="A Person", album=None)
    outcome = enrich([track], FakeClient({"tagged title": Recording(album="Unknown Album")}))
    assert outcome.tracks[0].album is None
    assert outcome.repaired == 0


# -- artwork --------------------------------------------------------------


def test_artwork_is_content_addressed(tmp_path: Path) -> None:
    payload = _image(400, "red")
    first = _track(tmp_path, "a.mp3", artwork=payload)
    second = _track(tmp_path, "b.mp3", artwork=payload)
    outcome = generate([first, second], tmp_path / "cache")
    assert len(outcome.assets) == 1
    assert outcome.by_file[first.path] is outcome.by_file[second.path]
    assert outcome.generated == 1, "two files, one image, one write"


def test_generated_files_are_decodable_and_bounded(tmp_path: Path) -> None:
    outcome = generate([_track(tmp_path, artwork=_image(2000, "blue"))], tmp_path / "cache")
    stored = tmp_path / "cache" / str(outcome.assets[0].relative_path)
    assert stored.is_file()
    with Image.open(stored) as image:
        assert max(image.size) <= ArtworkLimits().max_edge
        assert image.format == "JPEG"


def test_a_file_with_no_artwork_is_counted_not_invented(tmp_path: Path) -> None:
    outcome = generate([_track(tmp_path)], tmp_path / "cache")
    assert outcome.assets == ()
    assert outcome.empty == 1


def test_corrupt_image_bytes_are_a_skip_not_a_crash(tmp_path: Path) -> None:
    outcome = generate(
        [_track(tmp_path, artwork=b"this is not a JPEG, it never was")], tmp_path / "cache"
    )
    assert outcome.assets == ()
    assert (outcome.empty, outcome.failed) == (0, 1), "a damaged cover is not a missing one"


def test_an_asset_cap_stops_the_write(tmp_path: Path) -> None:
    tracks = [
        _track(tmp_path, f"{index}.mp3", title=f"T{index}", artwork=_image(64, colour))
        for index, colour in enumerate(("red", "green", "blue", "white"))
    ]
    outcome = generate(tracks, tmp_path / "cache", ArtworkLimits(max_assets=2))
    assert len(outcome.assets) == 2
    assert len(outcome.by_file) == 2


def test_re_storing_an_asset_already_on_disk_does_not_rewrite_it(tmp_path: Path) -> None:
    payload = _image(120, "green")
    asset = store_one(payload, tmp_path / "cache")
    assert asset is not None
    destination = tmp_path / "cache" / str(asset.relative_path)
    before = destination.stat().st_mtime_ns
    again = store_one(payload, tmp_path / "cache")
    assert again is not None
    assert again.relative_path == asset.relative_path
    assert destination.stat().st_mtime_ns == before, (
        "an incremental build must not touch unchanged bytes"
    )


# -- duplicates -----------------------------------------------------------


def test_identical_files_are_reported_as_groups(tmp_path: Path) -> None:
    first = _track(tmp_path, "a.mp3", title="One Thing", size=500, seconds=60.0)
    second = _track(tmp_path, "b.mp3", title="One Thing", size=500, seconds=60.0)
    report = analyse([first, second])
    assert report.groups
    assert report.groups[0].count == 2
    assert report.groups[0].kind == "identical-file"
    assert {path.name for path in report.groups[0].paths} == {"a.mp3", "b.mp3"}


def test_the_same_song_in_two_encodings_is_a_different_kind_of_duplicate(tmp_path: Path) -> None:
    first = _track(tmp_path, "a.mp3", title="Shared", size=500, seconds=60.0)
    second = _track(tmp_path, "b.flac", title="Shared", size=9000, seconds=60.0)
    report = analyse([first, second])
    assert [group.kind for group in report.groups] == ["same-recording"]


def test_different_songs_are_not_duplicates(tmp_path: Path) -> None:
    report = analyse(
        [
            _track(tmp_path, "a.mp3", title="One", size=100),
            _track(tmp_path, "b.mp3", title="Two", size=200),
        ]
    )
    assert report.groups == ()


def test_a_single_file_is_never_a_group(tmp_path: Path) -> None:
    assert analyse([_track(tmp_path, "solo.mp3", size=100)]).groups == ()


def test_the_duplicate_list_is_bounded(tmp_path: Path) -> None:
    tracks = [
        _track(
            tmp_path,
            f"{index}.mp3",
            title=f"Song {index // 2}",
            album=f"Album {index // 2}",
            size=1000 + index,
        )
        for index in range(60)
    ]
    lines = analyse(tracks).lines(limit=5)
    assert len(lines) == 6
    assert lines[-1].startswith("... and")


def test_nothing_is_removed_because_it_was_called_a_duplicate(tmp_path: Path) -> None:
    """SAPRS 6.8: the Builder reports, it does not prune.

    Asserted here because the guarantee is the absence of an effect: `analyse` takes a
    sequence and returns a report, and there is no path from one to the other.
    """

    tracks = [
        _track(tmp_path, "a.mp3", title="Twin", size=700, seconds=30.0),
        _track(tmp_path, "b.mp3", title="Twin", size=700, seconds=30.0),
    ]
    report = analyse(tracks)
    assert report.total == 2
    assert [track.path for track in tracks] == [tmp_path / "a.mp3", tmp_path / "b.mp3"]


# -- provenance plumbing --------------------------------------------------


def test_the_rules_version_is_a_number_the_report_can_print() -> None:
    assert isinstance(NORMALIZATION_RULES_VERSION, int)
    assert NORMALIZATION_RULES_VERSION >= 1


def test_a_track_keeps_both_forms_of_every_field(tmp_path: Path) -> None:
    track = _track(tmp_path, artist="  The  Test ARTISTS ", title="Hello")
    assert track.artist == "The Test ARTISTS"
    assert track.normalized_artist == "the test artists"
    assert track.sort_artist == "test artists, the"
    assert track.provenance[MetadataField.ARTIST].effective == "The Test ARTISTS"


def _image(edge: int, colour: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (edge, max(1, edge // 2)), colour).save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()
