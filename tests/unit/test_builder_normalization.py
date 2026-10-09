"""Normalization and precedence: stages 3 and 4 (SAPRS 6.4, 6.5, ADR-010).

The corpus is the co-author of this file. Every rule asserted here was written because
3,049 tagged files in `/opt/music` needed it, and ADR-010 records the measurements —
including the ones that killed a rule, like the directory hint that supplies a usable
artist for exactly zero of the 15 files that need one.

The two ideas worth defending:

* **Normalized values compare, display values show** (SAPRS 6.5). A test that a title
  is stored as written and that its folded form groups duplicates is the same test from
  both sides.
* **A hint must earn its place.** The directory is trusted only when its own tags agree
  on an artist, which is a decision made from the files and not from a list of names.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from apps.builder.normalization import (
    collapse,
    fold,
    is_placeholder,
    parse_position,
    sort_key,
    strip_promo_markers,
    strip_tool_prefix,
)
from apps.builder.precedence import filename_hint, normalize_track, path_hint, tree_map
from apps.builder.records import DiscoveredFile, ExtractedFile, Track
from encore.domain import AudioFormat
from encore.repositories.contract import MetadataField, MetadataSource

# -- normalization --------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("  An   Artist  ", "An Artist"),
        ("Line\nBreak\tHere", "Line Break Here"),
        # A control character becomes a separator rather than vanishing: a tag with
        # `Santana\x00Feat` inside it holds two words, and gluing them would invent a
        # third artist.
        ("Zero\x00Width", "Zero Width"),
        ("", ""),
    ],
)
def test_collapse_flattens_whitespace_and_control_characters(given: str, expected: str) -> None:
    assert collapse(given) == expected


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("AC/DC", "ac dc"),
        ("The  BEATLES", "the beatles"),
        ("Björk", "björk"),
        ("Guns N' Roses", "guns n' roses"),
    ],
)
def test_fold_is_the_identity_key(given: str, expected: str) -> None:
    """Case and separators disappear; the letters and the apostrophe do not."""

    assert fold(given) == expected


def test_two_spellings_of_one_album_group_together() -> None:
    """The defect the corpus shows, not the one a rule invents.

    Measured over the first 400 M4A files in `/opt/music`: 97 albums, one of which is
    spelled two ways — `Taking The Long Way` and `Taking the Long Way`. Folding is what
    makes those one key, and an artist page that listed them separately would be the
    bug report nobody files because it "looks like data".
    """

    assert (
        fold("Taking The Long Way") == fold("Taking the Long Way") == fold("taking  the long way")
    )
    assert fold("(Art of the Album)") == fold("art of the album") == fold("Art of the Album")


def test_sort_key_moves_a_leading_article_to_the_end() -> None:
    assert sort_key("The Final Countdown") == "final countdown, the"
    assert sort_key("Hello") == "hello"
    assert sort_key("") == ""


def test_sort_key_folds_case_but_moves_nothing_else() -> None:
    """A number stays where it is: reordering it would be inventing a sort position.

    The corpus has `01. Enter` and `Enter` as the same track in different rips, and the
    browse order that results is a decision about presentation, not about identity, so
    it belongs to milestone 7 and not to a normalization rule.
    """

    assert sort_key("05. Wild Thing") == "05. wild thing"
    assert sort_key("AC/DC") == "ac/dc"


def test_tool_prefixes_are_removed_and_the_removal_is_reported() -> None:
    result = strip_tool_prefix("AlbumWrap - Kenny Chesney")
    assert result.value == "Kenny Chesney"
    assert result.changed is True
    assert strip_tool_prefix("Kenny Chesney").changed is False


def test_promo_markers_are_removed_wherever_they_appear() -> None:
    for given, expected in (
        ("Highway to Hell-ADVANCE", "Highway to Hell"),
        ("Bad Romance [Clean_Version]", "Bad Romance"),
        ("Paranoid (Explicit)", "Paranoid"),
        ("Normal Song", "Normal Song"),
    ):
        assert strip_promo_markers(given).value == expected, given


@pytest.mark.parametrize(
    ("value", "number", "total"),
    [
        ("5", 5, None),
        ("5/12", 5, 12),
        ("1/2", 1, 2),
        ("2 of 4", 2, 4),
        ("007", 7, None),
        ("", None, None),
        ("A", None, None),
        (None, None, None),
    ],
)
def test_positions_canonicalise(value: str | None, number: int | None, total: int | None) -> None:
    """`(number, total)`, which is what the second value means.

    Worth stating because it is the reading that survives contact with the corpus:
    `TRCK=5/12` is the fifth of twelve tracks, and only the fifth goes into `songs`. The
    total is a fact about the release, and SAPRS 5.3 puts it nowhere.
    """

    assert parse_position(value) == (number, total)


@pytest.mark.parametrize(
    "value", ["Unknown Artist", "unknown artist", "VARIOUS", "n/a", "None", "-", "  "]
)
def test_placeholder_artists_count_as_absent(value: str) -> None:
    """SAPRS 6.5 treats an empty value as absent; this is the same claim about words."""

    assert is_placeholder(value) is True


def test_a_real_artist_is_not_a_placeholder() -> None:
    assert is_placeholder("The Unknown") is False
    assert is_placeholder("Artist Unknown But Named") is False


def test_the_unknown_album_placeholder_is_its_own_kind() -> None:
    assert is_placeholder("[Unknown Album]", kind="album") is True
    assert is_placeholder("Unknown Artist", kind="album") is False


# -- precedence: level 1, the tags -----------------------------------------


def _extracted(path: Path, tags: dict[str, str], *, seconds: float = 1.0) -> ExtractedFile:
    return ExtractedFile(
        file=DiscoveredFile(
            path=path,
            format=AudioFormat.for_path(path) or AudioFormat.MP3,
            size_bytes=1024,
            mtime_ns=1,
        ),
        tags=tags,
        duration=timedelta(seconds=seconds),
    )


def _track(root: Path, path: Path, tags: dict[str, str]) -> Track:
    extracted = _extracted(path, tags)
    return normalize_track(extracted, root=root, trees=tree_map([extracted], root))


def test_tags_win_over_everything(tmp_path: Path) -> None:
    track = _track(
        tmp_path,
        tmp_path / "Someone Else" / "Album" / "01 - Filename Artist - Filename Title.mp3",
        {"artist": "Tagged Artist", "album": "Tagged Album", "title": "Tagged Title", "track": "3"},
    )
    assert (track.artist, track.album, track.title) == (
        "Tagged Artist",
        "Tagged Album",
        "Tagged Title",
    )
    assert track.provenance["artist"].source == MetadataSource.TAG
    assert track.track_number == 3


def test_a_tagged_empty_value_is_absence(tmp_path: Path) -> None:
    track = _track(
        tmp_path, tmp_path / "artist" / "03 - Filename Song.mp3", {"title": "", "artist": "  "}
    )
    assert track.title == "Filename Song"
    assert track.provenance["title"].source == MetadataSource.FILENAME
    assert track.artist == ""
    assert MetadataField.ARTIST in track.missing_fields


def test_a_placeholder_tag_does_not_win(tmp_path: Path) -> None:
    """`Various Artists` is present and means nothing, so the filename still speaks."""

    track = _track(
        tmp_path,
        tmp_path / "artist" / "04 - Real Artist - Real Song.mp3",
        {"artist": "Unknown Artist", "title": "Real Song"},
    )
    assert track.artist == "Real Artist"
    assert track.provenance["artist"].repaired is True


# -- precedence: level 3, the directory ------------------------------------


def test_an_artist_tree_is_decided_from_its_own_files(tmp_path: Path) -> None:
    """AC/DC's directory is a tree because 214 files agree, not because of a name."""

    files = [
        _extracted(
            tmp_path / "AC_DC" / f"Disk {index}.mp3", {"artist": "AC/DC", "title": f"Track {index}"}
        )
        for index in range(1, 6)
    ]
    trees = tree_map(files, tmp_path)
    assert trees.is_artist_tree("AC_DC")
    assert path_hint(files[0].file.path, tmp_path, trees) == "ac dc"


def test_a_mood_bucket_is_not_an_artist_tree(tmp_path: Path) -> None:
    """The measured corpus: `country_5/` holds six different artists."""

    files = [
        _extracted(
            tmp_path / "country_5" / f"track-{index}.mp3",
            {"artist": f"Artist {index}", "title": "T"},
        )
        for index in range(1, 7)
    ]
    trees = tree_map(files, tmp_path)
    assert not trees.is_artist_tree("country_5")
    assert path_hint(files[0].file.path, tmp_path, trees) is None


def test_an_untagged_directory_never_supplies_an_artist(tmp_path: Path) -> None:
    """ADR-010's ruling: a guessed artist is worse than an absent one."""

    files = [
        _extracted(tmp_path / "Unknown_Artist" / "a.mp3", {}),
        _extracted(tmp_path / "Unknown_Artist" / "b.mp3", {}),
    ]
    trees = tree_map(files, tmp_path)
    assert not trees.is_artist_tree("Unknown_Artist")
    assert path_hint(files[0].file.path, tmp_path, trees) is None


def test_one_file_is_not_evidence(tmp_path: Path) -> None:
    """A directory with a single tagged file agrees with itself, which proves nothing."""

    files = [_extracted(tmp_path / "Solo" / "a.mp3", {"artist": "Solo Artist", "title": "T"})]
    assert not tree_map(files, tmp_path).is_artist_tree("Solo")


def test_the_tree_yields_the_tagged_spelling_not_the_folder_name(tmp_path: Path) -> None:
    """`AC_DC/` must produce "AC/DC", or the folder becomes a second artist."""

    files = [
        _extracted(tmp_path / "AC_DC" / f"t{index}.mp3", {"artist": "AC/DC", "title": f"T{index}"})
        for index in range(4)
    ]
    trees = tree_map(files, tmp_path)
    track = normalize_track(
        _extracted(tmp_path / "AC_DC" / "untagged.mp3", {}), root=tmp_path, trees=trees
    )
    assert track.artist == "ac dc"
    assert track.provenance["artist"].source == MetadataSource.PATH


def test_a_file_in_the_root_has_no_directory_hint(tmp_path: Path) -> None:
    assert path_hint(tmp_path / "loose.mp3", tmp_path, tree_map([], tmp_path)) is None


# -- precedence: level 4, the filename -------------------------------------


@pytest.mark.parametrize(
    ("name", "artist", "title"),
    [
        ("05 - Artist Name - Song Title.mp3", "Artist Name", "Song Title"),
        ("Artist Name - Song Title.mp3", "Artist Name", "Song Title"),
        ("Song Title.mp3", None, "Song Title"),
        ("03 - Song Title.mp3", None, "Song Title"),
        ("A - B - C.mp3", None, None),
        ("01.mp3", None, None),
        ("", None, None),
    ],
)
def test_filename_patterns_are_read_only_when_unambiguous(
    name: str, artist: str | None, title: str | None
) -> None:
    """Three or more dash-separated segments are a coin toss, so they are left alone."""

    assert filename_hint(name) == (artist, title)


def test_a_directory_name_is_not_a_filename_hint(tmp_path: Path) -> None:
    """`Unknown_Artist/03 - Track.mp3` supplies a title and no artist (measured, 0 of 15)."""

    track = _track(tmp_path, tmp_path / "Unknown_Artist" / "03 - Track.mp3", {})
    assert track.title == "Track"
    assert track.artist == ""


# -- the derived fields ----------------------------------------------------


def test_a_track_carries_both_forms_of_every_field(tmp_path: Path) -> None:
    """SAPRS 6.5: originals for the report, normalized values for grouping."""

    track = _track(
        tmp_path,
        tmp_path / "a" / "01.mp3",
        {"artist": "  The  Test ARTISTS ", "album": "Beta [Clean_Version]", "title": "05. Hello"},
    )
    assert track.artist == "The Test ARTISTS"
    assert track.normalized_artist == "the test artists"
    assert track.sort_artist == "test artists, the"
    assert track.album == "Beta"
    assert track.title == "05. Hello"
    assert track.provenance["album"].repaired is True
    assert track.provenance["album"].original == "Beta [Clean_Version]"


def test_grouping_uses_the_album_artist_and_the_row_keeps_the_performer(tmp_path: Path) -> None:
    """SAPRS 4.4 puts the performing artist on a Song; ADR-010 groups on the release."""

    track = _track(
        tmp_path,
        tmp_path / "a" / "01.mp3",
        {
            "artist": "Featured Rapper",
            "album_artist": "Host Artist",
            "album": "Compilation",
            "title": "Track",
        },
    )
    assert track.artist == "Host Artist"
    assert track.track_artist == "Featured Rapper"
    assert track.provenance["album_artist"].field == "album_artist"


def test_a_track_with_nothing_usable_is_reportable_not_fatal(tmp_path: Path) -> None:
    track = _track(tmp_path, tmp_path / "music" / "07.mp3", {})
    assert track.catalogable is False
    assert set(track.missing_fields) == {"artist", "title"}
    assert track.with_field("artist", "Filled", MetadataSource.MUSICBRAINZ).artist == "Filled"


def test_a_constant_source_is_visible_in_the_provenance(tmp_path: Path) -> None:
    """A made-up value must never look like a read one."""

    track = _track(tmp_path, tmp_path / "a" / "01.mp3", {"artist": "A", "title": "T"})
    filled = track.with_field("album", "[Unknown Album]", MetadataSource.CONSTANT)
    assert filled.provenance["album"].source == MetadataSource.CONSTANT
    assert filled.provenance["album"].repaired is True
    assert filled.provenance["album"].original is None
