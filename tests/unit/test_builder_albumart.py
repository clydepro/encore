"""The Cover Art Archive stage: one question per album, answered only when it is exact (6.7, #57).

ADR-016 adds a second precedence level to SAPRS 6.7, and this file is the policy that level is
allowed to have. Every test here is about *when the stage asks* rather than about bytes: an image
arrives through the same cache as an embedded picture, and the interesting question is whether the
album it was chosen for is the album on the shelf.

The rule that shapes everything below is that a wrong cover is worse than a grey tile. A guest
scanning tiles for an album they cannot find is annoyed; a guest looking at artwork from someone
else's record has been lied to by the one part of the interface they trusted to be a picture. So
the stage matches two folded names and takes no advice from ranking, and so the counts it reports
distinguish "not asked", "asked and matched" and "asked and nobody had one".
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from apps.builder.albumart import fetch
from apps.builder.enrichment import PreviousEnrichment
from apps.builder.musicbrainz import (
    DisabledClient,
    MusicBrainzUnavailableError,
    Recording,
    Release,
)
from apps.builder.precedence import normalize_track, tree_map
from apps.builder.records import ArtworkAsset, DiscoveredFile, ExtractedFile, Track
from encore.domain import AudioFormat


class FakeArchive:
    """A MusicBrainz that answers about releases and pictures, and remembers being asked.

    Args:
        catalog: What a search for an album returns, in the order MusicBrainz ranks it. The key
            is the album title, because the tests name the albums they mean and a test that
            looks up a query string is a test of this module's spelling.
        images: Release id → bytes. A release missing from here is the archive's honest "we have
            no front image for that", which is most of them.
        fail_after: Raise an outage instead of the answer after this many requests.
    """

    def __init__(
        self,
        catalog: dict[str, Sequence[Release]] | None = None,
        *,
        images: dict[str, bytes] | None = None,
        fail_after: int | None = None,
    ) -> None:
        self.catalog = catalog or {}
        self.images = images or {}
        self.fail_after = fail_after
        self.queries: list[tuple[str, str]] = []
        self.covers: list[str] = []

    @property
    def available(self) -> bool:
        return True

    def _asked(self) -> bool:
        """Whether the run has spent its allowance. Outages arrive on their own schedule."""

        return (
            self.fail_after is not None and len(self.queries) + len(self.covers) > self.fail_after
        )

    def search(self, *, artist: str, release: str, track: str) -> Recording | None:  # noqa: ARG002
        return None

    def recording(self, recording_id: str) -> Recording | None:  # noqa: ARG002
        return None

    def releases(self, *, artist: str, album: str) -> Sequence[Release]:
        if self._asked():
            raise MusicBrainzUnavailableError("simulated outage")
        self.queries.append((artist, album))
        return self.catalog.get(album, ())

    def cover(self, release_id: str) -> bytes | None:
        if self._asked():
            raise MusicBrainzUnavailableError("simulated outage")
        self.covers.append(release_id)
        return self.images.get(release_id)


def _track(
    root: Path,
    name: str = "a.mp3",
    *,
    artist: str = "Daft Punk",
    title: str = "Harder",
    album: str | None = "Random Access",
    artwork: bytes | None = None,
    **extra: str,
) -> Track:
    """A track as the pipeline would hand it over: normalized, grouped, and honest about its art."""

    raw: dict[str, str | None] = {
        "artist": artist,
        "title": title,
        "album": album,
        **extra,
    }
    tags = {key: value for key, value in raw.items() if value is not None}
    extracted = ExtractedFile(
        file=DiscoveredFile(path=root / name, format=AudioFormat.MP3, size_bytes=100, mtime_ns=1),
        tags=tags,
        duration=timedelta(seconds=1),
        embedded_artwork=artwork,
    )
    return normalize_track(extracted, root=root, trees=tree_map([extracted], root))


def _release(
    release_id: str, *, artist: str = "Daft Punk", title: str = "Random Access"
) -> Release:
    return Release(release_id=release_id, artist=artist, title=title)


def _asset() -> ArtworkAsset:
    return ArtworkAsset(
        kind="jpeg", relative_path=Path("ab/cd"), width=640, height=640, sha256="abcd"
    )


#: Not a decodable image. Nothing in this file asks for one: the stage hands the bytes to
#: `artwork.generate` untouched, and whether they decode is that stage's test to write. Its job
#: here is to be *some* picture, so a test can tell "there is one" from "there isn't".
_PNG = b"\x89PNG\r\n\x1a\n" + bytes(56)


# -- when the stage asks ------------------------------------------------------------------


def test_a_disabled_client_asks_nothing_at_all() -> None:
    """`--no-musicbrainz` and `--offline` arrive here as a null object, not as a second flag.

    The pipeline hands this stage a `DisabledClient` when cover art is off, which is why turning
    the feature off cannot leak a request: there is no branch in `fetch` that a future edit can
    take the wrong way round. The tracks still come back, because an early return that returns
    nothing would drop every row in the library.
    """

    tracks = [_track(Path("/m"), "a.mp3")]
    outcome = fetch(tracks, DisabledClient())
    assert (outcome.albums, outcome.requested, outcome.payloads) == (1, 0, {})
    assert list(outcome.tracks) == tracks


def test_an_album_whose_files_carry_a_picture_is_not_asked_about(tmp_path: Path) -> None:
    """SAPRS 6.7's first level is untouched: the file's own art wins and nobody is consulted.

    A one-pixel PNG stands in for any embedded image, because this is about whether the stage
    *looks*, not about what it does with what it finds.
    """

    picture = _PNG
    tracks = [_track(tmp_path, "a.mp3", artwork=picture), _track(tmp_path, "b.mp3", title="Coda")]
    archive = FakeArchive({"Random Access": [_release("rel-1")]}, images={})
    outcome = fetch(tracks, archive)
    assert archive.queries == []
    assert (outcome.albums, outcome.covered, outcome.uncovered) == (0, 0, 0)


def test_an_album_covered_by_a_carried_reference_is_not_asked_about(tmp_path: Path) -> None:
    """#52 and #57 meet here: a carried reference *is* a picture.

    An incremental build that did not re-open a file still has its artwork row, so asking the
    archive about that album would spend two requests to rediscover a JPEG the cache already
    holds — and would be the same mistake #52 measured, wearing a network costume.
    """

    tracks = [_track(tmp_path, "a.mp3"), _track(tmp_path, "b.mp3", album="Discovery")]
    archive = FakeArchive({"Discovery": [_release("rel-2", title="Discovery")]})
    outcome = fetch(tracks, archive, carried={tracks[0].path: _asset()})
    assert archive.queries == [("Daft Punk", "Discovery")]
    assert outcome.albums == 1, "only the album with no picture anywhere"


def test_the_unit_of_asking_is_the_album_not_the_track(tmp_path: Path) -> None:
    """Forty tracks of one release are one question, which is the whole cost argument of #57.

    The measured corpus has 463 uncovered albums; per-track asking would have been 1,989. The
    grouping is `Track.group_key`, the same rule `construction` files albums by, because an album
    that is one album to the library and two albums to this stage gets asked about forever.
    """

    tracks = [_track(tmp_path, f"{index}.mp3", title=f"Track {index}") for index in range(40)]
    archive = FakeArchive({"Random Access": [_release("rel-1")]}, images={"rel-1": _PNG})
    outcome = fetch(tracks, archive)
    assert archive.queries == [("Daft Punk", "Random Access")]
    assert (outcome.albums, outcome.requested, outcome.covered) == (1, 2, 1)
    assert list(outcome.payloads) == [(tracks[0].group_key)]


def test_an_album_with_no_title_is_not_asked_about(tmp_path: Path) -> None:
    """`[Unknown Album]` after normalization is the empty string, and the archive is not an oracle.

    Searching for the word "unknown" against a real artist returns something, and that something
    becomes the cover of every album this library cannot name.
    """

    archive = FakeArchive({})
    outcome = fetch([_track(tmp_path, album=None)], archive)
    assert archive.queries == []
    assert (outcome.albums, outcome.unmatched) == (0, 0)


# -- when the stage declines --------------------------------------------------------------


def test_both_names_have_to_match(tmp_path: Path) -> None:
    """The exact-match policy, tested from the side it protects.

    This candidate is the right artist's other album, ranked first because it is newer. "Closest"
    and "highest-scored" both put someone else's sleeve on this one.
    """

    archive = FakeArchive(
        {"Random Access": [_release("rel-other", title="Discovery")]},
        images={"rel-other": _PNG},
    )
    outcome = fetch([_track(tmp_path)], archive)
    assert (outcome.covered, outcome.unmatched, outcome.payloads) == (0, 1, {})
    assert archive.covers == [], "an unmatched release is never asked for a picture"


def test_case_and_punctuation_are_the_only_differences_allowed(tmp_path: Path) -> None:
    """`fold` is applied to both sides, so Daft Punk/DAFT PUNK is one artist.

    Anything past that is a different record: no substring, no prefix, no threshold. The title
    test is deliberately the *same words* in a different order — a search engine calls those a
    match, and a search engine is what would put a live album on a studio release.
    """

    archive = FakeArchive(
        {"Random Access": [_release("rel-1", artist="daft-punk", title="RANDOM ACCESS")]},
        images={"rel-1": _PNG},
    )
    matched = fetch([_track(tmp_path)], archive)
    assert matched.covered == 1

    other = FakeArchive(
        {"Random Access": [_release("rel-2", title="Access Random")]},
        images={"rel-2": _PNG},
    )
    declined = fetch([_track(tmp_path)], other)
    assert (declined.covered, declined.unmatched) == (0, 1)


# -- when the stage pays less than expected ------------------------------------------------


def test_a_release_identifier_in_the_tags_skips_the_search(tmp_path: Path) -> None:
    """Level 1 evidence outranks a question, and here it also answers it.

    One request instead of two, and the request that remains cannot guess: a lookup by MBID
    either has the front image or does not.
    """

    archive = FakeArchive({}, images={"rel-9": _PNG})
    outcome = fetch([_track(tmp_path, musicbrainz_release_id="rel-9")], archive)
    assert archive.queries == []
    assert archive.covers == ["rel-9"]
    assert (outcome.requested, outcome.covered) == (1, 1)


def test_a_release_identifier_from_the_last_build_skips_the_search(tmp_path: Path) -> None:
    """The reason `albums.musicbrainz_release_id` is written at all (#57).

    An album whose *artist* was repaired in March found its release on the way. Before this the
    identifier was put on the song's album row and never read again, so October searched for it a
    second time — a documented cache with no reader, which is what #52 called a defect rather than
    a TODO.
    """

    track = _track(tmp_path)
    archive = FakeArchive({}, images={"rel-9": _PNG})
    outcome = fetch([track], archive, earlier={track.path: PreviousEnrichment(release_id="rel-9")})
    assert archive.queries == []
    assert archive.covers == ["rel-9"]
    assert outcome.tracks[0].musicbrainz_release_id == "rel-9"


def test_a_tag_wins_over_the_previous_build(tmp_path: Path) -> None:
    """Precedence keeps its shape even when the older value is the more useful one.

    Someone retagged the file with a different release; the library follows the tag, because a
    build that preferred its own memory would make the retag invisible.
    """

    track = _track(tmp_path, musicbrainz_release_id="rel-new")
    archive = FakeArchive({}, images={"rel-new": _PNG})
    fetch([track], archive, earlier={track.path: PreviousEnrichment(release_id="rel-old")})
    assert archive.covers == ["rel-new"]


# -- what the stage reports back -----------------------------------------------------------


def test_an_identified_album_with_no_picture_is_still_recorded(tmp_path: Path) -> None:
    """`uncovered` is the normal answer, and the identifier it keeps is the future discount.

    Most MusicBrainz releases have no front image. The requests were already spent to find that
    out, so writing the id means the next build asks one question instead of two about the same
    empty answer.
    """

    archive = FakeArchive({"Random Access": [_release("rel-1")]}, images={})
    outcome = fetch([_track(tmp_path)], archive)
    assert (outcome.covered, outcome.uncovered, outcome.unmatched) == (0, 1, 0)
    assert outcome.payloads == {}
    assert outcome.tracks[0].musicbrainz_release_id == "rel-1"


def test_an_outage_ends_the_asking_and_keeps_what_was_found(tmp_path: Path) -> None:
    """The stage degrades and reports rather than raising, and stops rather than retrying.

    Two albums, an archive that dies after the second request: the first album's cover is in the
    outcome, the second is never mentioned again, and the report says unavailable. An album the
    archive never got to is counted nowhere — not "unmatched", which says this library could not
    name it, and not "uncovered", which says it has no picture — because both of those are
    sentences about the album rather than about the network. This is ADR-010's stage contract, and
    it is also the reason enabling the feature by default cannot turn a bad network into a failed
    build.
    """

    first = _track(tmp_path, "a.mp3", album="Discovery")
    second = _track(tmp_path, "b.mp3", album="Random Access")
    archive = FakeArchive(
        {
            "Discovery": [_release("rel-1", title="Discovery")],
            "Random Access": [_release("rel-2")],
        },
        images={"rel-1": _PNG},
        fail_after=2,
    )
    outcome = fetch([first, second], archive)
    assert outcome.unavailable is True
    assert (outcome.covered, outcome.unmatched, outcome.uncovered) == (1, 0, 0)
    assert len(outcome.payloads) == 1


def test_the_answer_is_a_count_of_decisions_not_of_bytes(tmp_path: Path) -> None:
    """The report reads these numbers, so they have to mean what they say.

    `albums` counts albums with no picture anywhere; `requested` counts calls, which is two for an
    album newly identified and one for an album already named; `covered` counts images. A build
    where 463 albums cost 926 requests and produced 380 covers is a build an operator can reason
    about, and a build that reported "463 covers" is one they cannot.
    """

    covered = _track(tmp_path, "a.mp3", album="Random Access")
    empty = _track(tmp_path, "b.mp3", album="Discovery")
    named = _track(tmp_path, "c.mp3", album="Homework", musicbrainz_release_id="rel-3")
    archive = FakeArchive(
        {
            "Random Access": [_release("rel-1")],
            "Discovery": [_release("rel-2", title="Discovery")],
        },
        images={"rel-1": _PNG, "rel-3": _PNG},
    )
    outcome = fetch([covered, empty, named], archive)
    assert outcome.albums == 3
    assert outcome.requested == 5, "2 + 2 + 1"
    assert outcome.covered == 2
    assert outcome.uncovered == 1
