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
from collections.abc import Sequence
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
    Release,
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

    # #57 grew the protocol by two questions. This fake is never asked either — it is handed to
    # `enrich`, not to `albumart` — but a protocol is implemented, not mostly implemented, and a
    # fake that leaves methods out is how a test keeps passing after the stage under it starts
    # calling something nobody modelled.
    def releases(self, *, artist: str, album: str) -> Sequence[Release]:  # noqa: ARG002
        if self.outage:
            raise MusicBrainzUnavailableError("simulated outage")
        return ()

    def cover(self, release_id: str) -> bytes | None:  # noqa: ARG002 - the signature is the protocol
        if self.outage:
            raise MusicBrainzUnavailableError("simulated outage")
        return None


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
    """Every question the Builder can ask has a no-network answer, including #57's two.

    `albumart` asks about releases and covers through this same protocol, so the null object has
    to answer those too — a stage that switches itself off by testing a flag somewhere else is a
    stage that still opens a socket on a machine told not to.
    """

    client = DisabledClient()
    assert client.available is False
    assert client.search(artist="A", release="B", track="C") is None
    assert client.recording("mbid") is None
    assert client.releases(artist="A", album="B") == ()
    assert client.cover("mbid") is None


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


# -- present but implausible (SAPRS 6.6 level 2, #57) ----------------------


def test_a_bare_counter_is_a_gap_the_way_nothing_is(tmp_path: Path) -> None:
    """`artist: 01` came out of a tagger that numbered a folder, and it used to be trusted.

    SAPRS 6.6's second level is "fill gaps **or correct implausible values**", and the stage
    implemented only the first half: `_gaps` asked whether a field *existed*. An artist of `01`
    existed, so it was never corrected, never searched, and still produced a search request in
    the last build's report — MusicBrainz is polled for anything by an artist named `01`, and
    the answer files the album under whoever else has a track called that. 34 of this corpus's
    3,049 files carried a counter where an artist should be.
    """

    client = FakeClient({"tagged title": Recording(artist="Digitalism", album="Zinc")})
    outcome = enrich([_track(tmp_path, artist="01")], client)
    assert client.searches == [("", "Tagged Album", "Tagged Title")], "asked about, not asked with"
    assert outcome.tracks[0].artist == "Digitalism"
    assert outcome.tracks[0].provenance["artist"].source == MetadataSource.MUSICBRAINZ
    assert outcome.repaired == 1


def test_a_counter_with_nothing_else_to_ask_with_asks_nothing(tmp_path: Path) -> None:
    """A query naming only a track title matches every release with that title.

    So the file is reported as it is — an artist of "01" and no album to identify the record —
    rather than filed under whichever "Morning" the search returned first.
    """

    client = FakeClient({})
    outcome = enrich([_track(tmp_path, artist="01", album=None)], client)
    assert client.searches == []
    assert outcome.tracks[0].artist == "01"


def test_a_counter_that_is_not_a_counter_keeps_its_value(tmp_path: Path) -> None:
    """`02` is a track number in an artist field. `21 Savage` and `07th Expansion` are artists.

    The rule is the narrow one: a field that is *nothing but* a small integer. `collapse` is
    applied first because `01.` and `01 ` are one shape, and an artist whose name starts with
    digits is a name — level 1 evidence that level 2 has no business overruling.
    """

    for spelling in ("21 Savage", "07th Expansion", "01 AM", "No. 1"):
        client = FakeClient({})
        outcome = enrich([_track(tmp_path, artist=spelling)], client)
        assert outcome.tracks[0].artist == spelling
        assert client.searches == [], "not a gap, so not a request"
        assert outcome.repaired == 0


def test_a_band_named_by_a_year_is_asked_about_and_kept(tmp_path: Path) -> None:
    """The cost of the rule, stated where it is paid.

    `artist: 1776` is nothing but digits, so it counts as a gap and it is asked about. What does
    not happen is the value being thrown away: `_resolve`'s last resort is the tag itself, so a
    band genuinely named by a year keeps the name when MusicBrainz has no answer. An implausible
    value is questioned, never discarded, which is the asymmetry the rest of #57 is built on.
    """

    outcome = enrich([_track(tmp_path, artist="1776")], FakeClient({}))
    assert outcome.tracks[0].artist == "1776"
    assert outcome.tracks[0].provenance["artist"].source == MetadataSource.TAG


def test_an_unparseable_date_is_a_gap(tmp_path: Path) -> None:
    """`date: 24` is a two-digit year nobody typed on purpose.

    241 of this corpus's files have no usable date; 239 of them are simply empty and 2 are this.
    The distinction decides whether the artifact says "24" for the release year, which is a date
    no album was released on and which the UI prints as 24.
    """

    outcome = enrich([_track(tmp_path, date="24")], FakeClient({}))
    assert outcome.tracks[0].date is None, "not a year, so not a value"

    with_an_answer = enrich(
        [_track(tmp_path, album=None, date="24")],
        FakeClient({"tagged title": Recording(year="1974")}),
    )
    assert with_an_answer.tracks[0].date == "1974"


def test_a_date_in_any_shape_that_holds_a_real_year_is_kept(tmp_path: Path) -> None:
    """`02/03/2024` and `2024-03-02` are complete dates; the parser is not being asked to
    choose a locale, only to find four digits that could be a year.

    The window is fixed rather than "not in the future" because a test that passes until
    1 January 2101 is not a test. 1877 is the earliest year the recordings literature gives
    for a commercially released disc, and the only cost of rounding it down is that a
    transcription error becomes an accepted value.
    """

    for spelling in ("02/03/2024", "2024-03-02", "1974", "c1975", "2024 (remaster)"):
        outcome = enrich([_track(tmp_path, date=spelling)], FakeClient({}))
        assert outcome.tracks[0].date == spelling, f"{spelling} names a year"

    for impossible in ("24", "0", "999", "1876", "2101"):
        assert enrich([_track(tmp_path, date=impossible)], FakeClient({})).tracks[0].date is None


def test_an_album_called_a_number_is_an_album_name(tmp_path: Path) -> None:
    """The digit rule is about counters, and Adele's `21` is not one.

    Applied to albums, "all digits" would search 21, 25, 30, and 13 — four of this corpus's
    albums — on every build, and would replace a correct title with whichever release the
    search matched first. An album field gets no plausibility test at all, because there is no
    number that a title cannot be.
    """

    client = FakeClient({})
    outcome = enrich([_track(tmp_path, album="21")], client)
    assert outcome.tracks[0].album == "21"
    assert client.searches == []


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


# -- the second precedence level: the release's own cover (#57) -----------------------------


def test_an_album_cover_from_the_archive_fills_every_track_of_that_album(tmp_path: Path) -> None:
    """SAPRS 6.7's new level 2, and the reason 463 grey tiles stopped being the end of the story.

    Three files of one release, no picture in any of them: one image is written and three
    references point at it. The plural matters — an implementation that stored the cover against
    the first file it came to would leave two of these tracks grey, which is the same defect #52
    measured in the incremental case.
    """

    tracks = [_track(tmp_path, f"{index}.mp3", title=f"T{index}") for index in range(3)]
    outcome = generate(
        tracks, tmp_path / "cache", network={tracks[0].group_key: _image(300, "red")}
    )
    assert len(outcome.assets) == 1, "one image, not three copies of it"
    assert len(outcome.by_file) == 3
    assert (outcome.generated, outcome.empty) == (1, 0)


def test_the_file_s_own_picture_outranks_the_archive(tmp_path: Path) -> None:
    """Level 1 is untouched: a real picture in the file is never displaced by a picture of it.

    The archive's answer is a scan of whatever MusicBrainz thinks this release is. The file's own
    embedded image came from the copy in front of the listener, and a jukebox that preferred the
    guess would be re-labelling records it already had.
    """

    track = _track(tmp_path, artwork=_image(300, "blue"))
    mine = generate([track], tmp_path / "embedded")
    theirs = generate([track], tmp_path / "both", network={track.group_key: _image(300, "red")})
    assert theirs.by_file[track.path].sha256 == mine.by_file[track.path].sha256
    assert theirs.generated == 1, "one image written this run, and it is the file's own"
    written = [path for path in (tmp_path / "both").rglob("*") if path.is_file()]
    assert len(written) == 1, "the archive's bytes were normalized and then discarded"


def test_a_carried_reference_outranks_the_archive(tmp_path: Path) -> None:
    """The order the level is stated in, tested at the boundary between #52 and #57.

    An incremental build that did not re-open the file still holds its picture, and asking the
    archive anyway would write a second image of the same album and show the guest whichever one
    the cache fetched first.
    """

    held = store_one(_image(300, "blue"), tmp_path / "cache")
    assert held is not None
    track = _track(tmp_path)
    outcome = generate(
        [track],
        tmp_path / "cache",
        previous={track.path: held},
        network={track.group_key: _image(300, "red")},
    )
    assert outcome.by_file[track.path].sha256 == held.sha256
    assert (outcome.generated, outcome.reused) == (0, 1), "the archive was not asked to be right"


def test_an_undecodable_cover_grey_tiles_the_album_and_says_so_once(tmp_path: Path) -> None:
    """A body that is not an image is one failure and two missing pictures.

    The counts answer different questions: how many things arrived that could not be used, and how
    many tracks will look grey. Reporting the second as the first would make one bad JPEG on a
    40-track album look like 40 broken images; reporting the first as the second would hide the
    tracks. Either way the songs stay in the library (6.7).
    """

    tracks = [_track(tmp_path, f"{index}.mp3", title=f"T{index}") for index in range(2)]
    outcome = generate(tracks, tmp_path / "cache", network={tracks[0].group_key: b"not an image"})
    assert (outcome.failed, outcome.empty) == (1, 2)
    assert outcome.by_file == {}
    assert outcome.assets == ()


def test_the_cap_bounds_new_images_whatever_the_source(tmp_path: Path) -> None:
    """`--max-artwork` is a promise about disk, and a cover from the internet is disk too."""

    first = _track(tmp_path, "a.mp3", album="Discovery")
    second = _track(tmp_path, "b.mp3", album="Homework")
    outcome = generate(
        [first, second],
        tmp_path / "cache",
        ArtworkLimits(max_assets=1),
        network={first.group_key: _image(300, "red"), second.group_key: _image(300, "blue")},
    )
    assert len(outcome.assets) == 1
    assert len(outcome.by_file) == 1, "the album the cap could not fit is the one with no picture"


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
