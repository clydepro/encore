"""Core domain model (SAPRS Chapter 4, AIG 21 step 2).

Three things these tests hold still: the model states the spec, the types are
immutable, and no rule lives anywhere but here.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import pathlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from encore import domain
from encore.domain import (
    ACTIVE_STATUSES,
    ALLOWED_TRANSITIONS,
    Album,
    AlbumId,
    Artist,
    ArtistId,
    Artwork,
    ArtworkId,
    ArtworkKind,
    AudioFormat,
    HealthStatus,
    Metadata,
    MusicFile,
    MusicFileId,
    PlaybackProgress,
    PlaybackState,
    QueueItem,
    QueueItemId,
    QueueItemStatus,
    Song,
    SongId,
    can_transition,
)


def song(**overrides: object) -> Song:
    """A valid song with one field changed.

    Keyword-only everywhere in this suite: an entity with six positional fields
    in a test is a test that silently means something else when the model grows.
    """

    values: dict[str, object] = {
        "id": SongId(1),
        "title": "Tomorrow",
        "artist_id": ArtistId(2),
        "album_id": AlbumId(3),
        "file_path": Path("/var/lib/encore/music/a/b/c.mp3"),
        "file_format": AudioFormat.MP3,
        "duration": timedelta(seconds=212),
    }
    values.update(overrides)
    return Song(**values)  # type: ignore[arg-type]


# -- shape of the model --------------------------------------------------


def test_every_core_entity_from_saprs_4_1_exists() -> None:
    """SAPRS 4.1 lists nine core things; five of them are types here."""

    for name in ("Artist", "Album", "Song", "Artwork", "MusicFile", "Metadata", "QueueItem"):
        assert hasattr(domain, name), f"SAPRS 4.1 lists {name} and the model has no such type"
    # "Playback State" is an enum, not an entity, because it has no identity.
    assert {state.value for state in PlaybackState} == {
        "idle",
        "loading",
        "playing",
        "paused",
        "stopping",
        "error",
        "recovering",
    } | {"finished"}, "SAPRS 4.6 lists seven states; 7.3 draws an eighth"


@pytest.mark.parametrize(
    ("entity", "expected"),
    [
        (Artist, {"id", "name", "sort_name", "normalized_name", "musicbrainz_id", "artwork_id"}),
        (Album, {"id", "artist_id", "title", "sort_title", "normalized_title"}),
        (Song, {"id", "title", "artist_id", "album_id", "track_number", "disc_number"}),
        (QueueItem, {"id", "song_id", "position", "status", "enqueued_at"}),
    ],
)
def test_entities_carry_the_properties_the_spec_names(entity: type, expected: set[str]) -> None:
    """4.2, 4.3, 4.4 and 4.5 each enumerate properties. Neither list drifts alone.

    Asserted as a subset rather than equality: an entity may gain a field for a
    later milestone, but it may not lose one Chapter 4 promised.
    """

    fields = {f.name for f in dataclasses.fields(entity)}
    assert expected <= fields, f"{entity.__name__} is missing {sorted(expected - fields)}"


def test_every_entity_is_immutable() -> None:
    """ADR-006's reasoning applies to the model too: nothing rewrites a fact."""

    entities: list[type[Any]] = [  # Any: `__dataclass_params__` is not on `type` itself
        Artist,
        Album,
        Song,
        Artwork,
        MusicFile,
        Metadata,
        QueueItem,
        PlaybackProgress,
    ]
    for entity in entities:
        assert entity.__dataclass_params__.frozen, f"{entity.__name__} is not frozen"


def test_entities_are_importable_from_the_package_not_only_modules() -> None:
    """The package is the public surface; modules may be reorganized."""

    assert domain.Song is Song
    assert set(domain.__all__) >= {
        "Artist",
        "Album",
        "Song",
        "Artwork",
        "MusicFile",
        "Metadata",
        "QueueItem",
        "PlaybackState",
    }


def test_every_exported_name_resolves() -> None:
    """`__all__` is the package's promise; a name in it must exist.

    Constants are listed first, then types alphabetically, so a diff of the
    export block reads as a changelog rather than a shuffle.
    """

    for name in domain.__all__:
        assert hasattr(domain, name), f"encore.domain exports {name}, which does not exist"
    assert list(domain.__all__)[:4] == [
        "ACTIVE_STATUSES",
        "ALLOWED_TRANSITIONS",
        "CANONICAL_TAGS",
        "HEALTHY",
    ]


# -- artist, album, song -------------------------------------------------


def test_artist_needs_a_display_name() -> None:
    with pytest.raises(ValueError, match="display name"):
        Artist(id=ArtistId(1), name="   ")


def test_artist_sorts_by_name_when_no_sort_name_is_given() -> None:
    assert Artist(id=ArtistId(1), name="Bauhaus").sort_key == "Bauhaus"
    assert Artist(id=ArtistId(1), name="Bauhaus", sort_name="Bauhaus").sort_key == "Bauhaus"
    assert Artist(id=ArtistId(1), name="The Beat", sort_name="Beat, The").sort_key == "Beat, The", (
        "the article must not decide where an artist lands in a list (SAPRS 4.2)"
    )


def test_album_belongs_to_exactly_one_artist() -> None:
    """SAPRS 4.3: an album has an artist association, singular. Compilations are
    modelled as their own artist, which keeps 9.5 browsing to one lookup."""

    album = Album(id=AlbumId(1), artist_id=ArtistId(7), title="Press")
    assert album.artist_id == ArtistId(7)
    assert album.sort_key == "Press"


def test_song_requires_a_positive_duration() -> None:
    """A song with no known length cannot honour the playback-start target or the
    progress bar, so the model refuses it rather than passing it along."""

    for duration in (timedelta(0), timedelta(seconds=-1)):
        with pytest.raises(ValueError, match="duration"):
            song(duration=duration)


def test_song_paths_are_absolute() -> None:
    """SAPRS 13.3 fixes the layout; a relative path means an unresolved location."""

    with pytest.raises(ValueError, match="absolute"):
        song(file_path=Path("music/c.mp3"))


@pytest.mark.parametrize("number", [0, -3])
def test_track_and_disc_numbers_are_one_based_or_absent(number: int) -> None:
    with pytest.raises(ValueError, match="1-based"):
        song(track_number=number)
    with pytest.raises(ValueError, match="1-based"):
        song(disc_number=number)

    assert song(track_number=None, disc_number=None).track_number is None


def test_song_reports_duration_in_seconds_for_mpv() -> None:
    assert song(duration=timedelta(minutes=3, seconds=32)).duration_seconds == 212.0


def test_artwork_reference_is_relative_to_the_cache() -> None:
    """SAPRS 5.6: files plus stable references, not blobs in rows."""

    artwork = Artwork(
        id=ArtworkId(1),
        kind=ArtworkKind.ALBUM,
        relative_path=Path("albums/aa/aalbb.jpg"),
        width=600,
        height=600,
    )
    assert artwork.relative_path.is_relative_to(Path())
    with pytest.raises(ValueError, match="relative"):
        Artwork(
            id=ArtworkId(2),
            kind=ArtworkKind.ALBUM,
            relative_path=Path("/var/lib/encore/artwork/x.jpg"),
        )


def test_artwork_is_optional_everywhere_it_is_associated() -> None:
    """SAPRS 6.7: missing artwork must not make anything unplayable."""

    assert Artist(id=ArtistId(1), name="X").artwork_id is None
    assert Album(id=AlbumId(1), artist_id=ArtistId(1), title="Y").artwork_id is None
    assert song().artwork_id is None


# -- media ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [("a.mp3", AudioFormat.MP3), ("a.flac", AudioFormat.FLAC), ("a.aac", AudioFormat.AAC)],
)
def test_format_is_discovered_from_the_suffix(filename: str, expected: AudioFormat) -> None:
    assert AudioFormat.for_path(Path(filename)) is expected


def test_m4a_accepts_both_containers_and_unknown_returns_none() -> None:
    assert AudioFormat.for_path(Path("a.m4a")) is AudioFormat.M4A
    assert AudioFormat.for_path(Path("a.mp4")) is AudioFormat.M4A
    assert AudioFormat.for_path(Path("a.ogg")) is None, "unsupported files are ignored upstream"


def test_music_file_rejects_negative_size() -> None:
    with pytest.raises(ValueError, match="size_bytes"):
        MusicFile(
            id=MusicFileId(1),
            path=Path("/m/a.mp3"),
            format=AudioFormat.MP3,
            duration=timedelta(seconds=1),
            size_bytes=-1,
        )


# -- metadata ------------------------------------------------------------


def test_metadata_keeps_the_original_tags_readable() -> None:
    """SAPRS 6.5: normalization must not destroy what the file actually said."""

    record = Metadata(
        artist="The Beatles",
        title="Come Together",
        original={"artist": "BEATLES, THE", "title": "come together "},
    )
    assert record.tag("artist") == "BEATLES, THE"
    assert record.artist == "The Beatles"


def test_metadata_tag_views_are_read_only() -> None:
    record = Metadata(artist="x", title="y", original={"artist": "x"}, extra={"isrc": "USX"})
    for view in (record.original, record.extra):
        with pytest.raises(TypeError):
            view["injected"] = "value"  # type: ignore[index]


def test_metadata_absent_album_is_none_not_a_placeholder() -> None:
    """SAPRS 6.5 leaves the choice of placeholder to the Builder, so the model
    must not make it silently."""

    assert Metadata(artist="x", title="y").album is None


@pytest.mark.parametrize(
    ("artist", "title"),
    [("  ", "Come Together"), ("The Beatles", "  ")],
)
def test_metadata_requires_artist_and_title(artist: str, title: str) -> None:
    with pytest.raises(ValueError, match="cannot be catalogued"):
        Metadata(artist=artist, title=title)


def test_identifier_presence_is_reported() -> None:
    assert not Metadata(artist="a", title="t").has_identifiers
    assert Metadata(artist="a", title="t", musicbrainz_recording_id="x").has_identifiers


# -- queue ---------------------------------------------------------------


def test_two_queue_items_may_reference_one_song() -> None:
    """SAPRS 8.3, expressed as a type that cannot say otherwise (4.5)."""

    first = QueueItem(id=QueueItemId(1), song_id=SongId(9), position=1)
    second = QueueItem(id=QueueItemId(2), song_id=SongId(9), position=2)
    assert first.song_id == second.song_id
    assert first.id != second.id
    assert dataclasses.fields(QueueItem)
    assert not any(
        "guest" in f.name or "requester" in f.name for f in dataclasses.fields(QueueItem)
    ), "a field that distinguished guests would break ADR-007"


def test_queue_position_is_one_based() -> None:
    with pytest.raises(ValueError, match="1-based"):
        QueueItem(id=QueueItemId(1), song_id=SongId(1), position=0)


def test_active_statuses_are_the_ones_holding_a_place() -> None:
    assert frozenset({QueueItemStatus.PENDING, QueueItemStatus.PLAYING}) == ACTIVE_STATUSES
    assert QueueItem(id=QueueItemId(1), song_id=SongId(1), position=1).is_active
    assert not QueueItem(
        id=QueueItemId(1), song_id=SongId(1), position=1, status=QueueItemStatus.REMOVED
    ).is_active


def test_queue_item_timestamp_is_aware_and_comparable() -> None:
    """Ordering across a restart depends on subtraction (SAPRS 8.5)."""

    first = QueueItem(
        id=QueueItemId(1),
        song_id=SongId(1),
        position=1,
        enqueued_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    second = QueueItem(
        id=QueueItemId(2),
        song_id=SongId(2),
        position=2,
        enqueued_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
    )
    assert (second.enqueued_at - first.enqueued_at) == timedelta(minutes=1)

    with pytest.raises(ValueError, match="timezone-aware"):
        QueueItem(
            id=QueueItemId(3),
            song_id=SongId(3),
            position=1,
            enqueued_at=datetime(2026, 1, 1),
        )


# -- playback ------------------------------------------------------------


def test_playback_transitions_follow_the_saprs_7_3_diagram() -> None:
    assert can_transition(PlaybackState.IDLE, PlaybackState.LOADING)
    assert can_transition(PlaybackState.LOADING, PlaybackState.PLAYING)
    assert can_transition(PlaybackState.PLAYING, PlaybackState.PAUSED)
    assert can_transition(PlaybackState.PLAYING, PlaybackState.FINISHED)
    assert can_transition(PlaybackState.FINISHED, PlaybackState.IDLE)
    assert can_transition(PlaybackState.ERROR, PlaybackState.RECOVERING)
    assert can_transition(PlaybackState.RECOVERING, PlaybackState.PLAYING)


def test_no_state_is_reachable_from_nowhere() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(PlaybackState), "every state needs an entry list"
    for targets in ALLOWED_TRANSITIONS.values():
        assert targets, "a state that leads nowhere strands the appliance"
    assert not can_transition(PlaybackState.IDLE, PlaybackState.PLAYING), (
        "loading is not optional: playback start is measured from it (SAPRS 1.8)"
    )


def test_progress_fraction_survives_an_unknown_duration() -> None:
    """SAPRS 11.9: a display value must not be able to stop playback."""

    unknown = PlaybackProgress(state=PlaybackState.PLAYING, song_id=SongId(1))
    assert unknown.fraction == 0.0
    halfway = PlaybackProgress(
        state=PlaybackState.PLAYING,
        song_id=SongId(1),
        position=timedelta(seconds=30),
        duration=timedelta(seconds=60),
    )
    assert halfway.fraction == 0.5
    assert PlaybackProgress(state=PlaybackState.PAUSED).is_audible is False


# -- health --------------------------------------------------------------


def test_health_aggregates_to_the_worst_report() -> None:
    """SAPRS 11.6 gives aggregation to HealthService; the ordering is the model's."""

    reports = [
        domain.ComponentHealth(component="library"),
        domain.ComponentHealth(
            component="playback", status=HealthStatus.DEGRADED, detail="mpv gone"
        ),
    ]
    worst = max(report.status.severity for report in reports)
    assert worst == HealthStatus.DEGRADED.severity
    assert HealthStatus.UNAVAILABLE.severity > worst


def test_a_health_report_names_its_source() -> None:
    with pytest.raises(ValueError, match="name"):
        domain.ComponentHealth(component="  ")


def test_module_level_state_is_not_a_service() -> None:
    """AEP 9 forbids singletons; the one module-level value here is a constant."""

    assert domain.ComponentHealth(component="system") == domain.HEALTHY
    mutable = [
        name
        for name, value in inspect.getmembers(domain, lambda o: isinstance(o, dict))
        if not name.startswith("_")
    ]
    assert not mutable, f"mutable module-level state in the domain: {mutable}"


# -- invariants the happy path never reaches -----------------------------


def test_an_album_needs_a_title() -> None:
    with pytest.raises(ValueError, match="display title"):
        Album(id=AlbumId(1), artist_id=ArtistId(1), title="  ")


def test_a_song_needs_a_title() -> None:
    with pytest.raises(ValueError, match="display title"):
        song(title="")


@pytest.mark.parametrize("number", [0, -1])
def test_metadata_positions_are_one_based_or_absent(number: int) -> None:
    with pytest.raises(ValueError, match="1-based"):
        Metadata(artist="a", title="t", track_number=number)


def test_metadata_duration_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match="duration"):
        Metadata(artist="a", title="t", duration=timedelta(seconds=-1))


def test_a_music_file_path_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="absolute"):
        MusicFile(
            id=MusicFileId(1),
            path=Path("music/a.mp3"),
            format=AudioFormat.MP3,
            duration=timedelta(seconds=1),
        )


def test_music_file_seconds_are_reported_for_the_database() -> None:
    file = MusicFile(
        id=MusicFileId(1),
        path=Path("/music/a.flac"),
        format=AudioFormat.FLAC,
        duration=timedelta(seconds=95, milliseconds=400),
        size_bytes=1_024,
    )
    assert file.duration_seconds == 95.4


def test_artwork_dimensions_are_reported_or_unknown_but_never_negative() -> None:
    with pytest.raises(ValueError, match="dimensions"):
        Artwork(id=ArtworkId(1), kind=ArtworkKind.ALBUM, relative_path=Path("a.jpg"), width=-2)
    assert Artwork(id=ArtworkId(2), kind=ArtworkKind.ARTIST, relative_path=Path("b.jpg")).width == 0


@pytest.mark.parametrize("position", [timedelta(seconds=-1)])
def test_playback_position_cannot_be_before_the_start(position: timedelta) -> None:
    with pytest.raises(ValueError, match="position"):
        PlaybackProgress(state=PlaybackState.PLAYING, position=position)


def test_playback_duration_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match="duration"):
        PlaybackProgress(state=PlaybackState.PLAYING, duration=timedelta(seconds=-1))


def test_sort_keys_fall_back_to_display_names() -> None:
    """SAPRS 4.2-4.4 make sort values optional; browsing still has to order."""

    assert Artist(id=ArtistId(1), name="X").sort_key == "X"
    assert Album(id=AlbumId(1), artist_id=ArtistId(1), title="Y").sort_key == "Y"
    assert song(sort_title=None).sort_key == "Tomorrow"
    assert song(sort_title="Tomorrow (remix)").sort_key == "Tomorrow (remix)"


def test_the_domain_imports_nothing_from_the_runtime() -> None:
    """AIG 4: the domain is the innermost layer, so it imports less than it.

    Asserted over the parsed import statements of every module in the package -
    broader than the guardrail test's service-layer sweep and specific about the
    reason: an entity that reached for SQLite or a template could not be reused by
    the Builder, which is ADR-001's whole point.
    """

    forbidden = {"fastapi", "starlette", "jinja2", "uvicorn", "sqlalchemy", "sqlite3", "httpx"}
    package_dir = pathlib.Path(domain.__file__).parent
    offenders: dict[str, set[str]] = {}
    for source in sorted(package_dir.glob("*.py")):
        imported = {
            (node.module or "").split(".")[0]
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
            if isinstance(node, ast.ImportFrom)
        } | {
            alias.name.split(".")[0]
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        if hit := imported & forbidden:
            offenders[source.name] = hit

    assert offenders == {}, f"the domain reaches for the runtime: {offenders}"
    assert {path.name for path in package_dir.glob("*.py")} >= {"artist.py", "album.py", "song.py"}
