"""Row → entity, with the coercion written down once (SAPRS 5.3, ADR-009).

This is the other half of ADR-009's mitigation. `queries.py` holds the statements,
`contract.py` holds the names, and this module is where a SQLite value becomes an
`encore.domain` object — which makes it the only place a type-affinity surprise can be
caught. SQLite returns `int` for a column declared `BOOLEAN`, `str` for a datetime it
stored as `TEXT`, and will hand back a `float` from a column declared `INTEGER` if that
is what was written into it.

Two rules are visible in the signatures:

* **A mapper names every column it reads.** An absent key raises
  `ContractViolationError` carrying the column name, rather than producing an entity
  with a wrong field in it.
* **A mapper never invents a value.** A missing `artwork_id` stays `None`. A path that
  is not absolute is passed through unresolved, because repairing the library on the
  fly is what ADR-006 forbids and `encore.domain.song.Song` already refuses.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from encore.domain import (
    Album,
    AlbumId,
    Artist,
    ArtistId,
    Artwork,
    ArtworkId,
    ArtworkKind,
    AudioFormat,
    Metadata,
    MusicFile,
    MusicFileId,
    Song,
    SongId,
)
from encore.repositories.coercion import RowView

__all__ = [
    "to_album",
    "to_artist",
    "to_artwork",
    "to_metadata",
    "to_music_file",
    "to_song",
]


def to_artist(row: sqlite3.Row) -> Artist:
    view = RowView(row, "artists")
    return Artist(
        id=ArtistId(view.integer("id")),
        name=view.text("name"),
        sort_name=view.text("sort_name"),
        normalized_name=view.text("normalized_name"),
        musicbrainz_id=view.optional_text("musicbrainz_id"),
        artwork_id=_artwork_id(view),
    )


def to_album(row: sqlite3.Row) -> Album:
    """An album as the catalogue states it.

    `release_date` is deliberately not read here. SAPRS 4.3 lists it on the domain
    entity and the library stores it on the song (SAPRS 4.4), which is a modelling
    disagreement the Builder resolves by writing both; a reader that asked for the
    album's copy would be asking a question this schema does not answer, so the
    display date comes from the song rows.
    """

    view = RowView(row, "albums")
    return Album(
        id=AlbumId(view.integer("id")),
        artist_id=ArtistId(view.integer("artist_id")),
        title=view.text("title"),
        sort_title=view.text("sort_title"),
        normalized_title=view.text("normalized_title"),
        musicbrainz_release_id=view.optional_text("musicbrainz_release_id"),
        artwork_id=_artwork_id(view),
    )


def to_song(row: sqlite3.Row) -> Song:
    """One row of the song read path, joined with its file."""

    view = RowView(row, "songs")
    return Song(
        id=SongId(view.integer("id")),
        title=view.text("title"),
        sort_title=view.text("sort_title"),
        artist_id=ArtistId(view.integer("artist_id")),
        album_id=AlbumId(view.integer("album_id")),
        file_path=view.path("path"),
        file_format=view.enumeration("format", AudioFormat),
        duration=view.duration("duration_seconds"),
        track_number=view.optional_integer("track_number"),
        disc_number=view.optional_integer("disc_number"),
        music_file_id=MusicFileId(view.integer("music_file_id")),
        artwork_id=_artwork_id(view),
    )


def to_music_file(row: sqlite3.Row) -> MusicFile:
    view = RowView(row, "music_files")
    return MusicFile(
        id=MusicFileId(view.integer("id")),
        path=view.path("path"),
        format=view.enumeration("format", AudioFormat),
        duration=view.duration("duration_seconds"),
        size_bytes=view.integer("size_bytes"),
    )


def to_artwork(row: sqlite3.Row) -> Artwork:
    """One cached image, addressed relative to `paths.artwork_dir`.

    The directory is not joined here on purpose: the domain type stores the reference
    (SAPRS 5.6), the caller owns the location, and so a library moved to another card
    with its cache beside it stays readable.
    """

    view = RowView(row, "artwork")
    return Artwork(
        id=ArtworkId(view.integer("id")),
        kind=view.enumeration("kind", ArtworkKind),
        relative_path=Path(view.text("relative_path")),
        width=view.integer("width"),
        height=view.integer("height"),
    )


def to_metadata(row: sqlite3.Row, provenance: Sequence[sqlite3.Row] = ()) -> Metadata:
    """Reassemble the tag evidence behind one song (SAPRS 4.1, 6.5).

    `original` is rebuilt from the provenance rows, which is what makes "what did this
    file actually say?" answerable after normalization changed the answer. `extra` is
    not: SAPRS 6.5 keeps originals available "where appropriate", and the Builder's
    position is that twelve columns of `BPM`, `MOOD` and `PLAYCOUNT` across 15,000
    songs that nothing reads does not meet that bar.
    """

    view = RowView(row, "songs")
    records = {str(item["field"]): item for item in provenance}
    return Metadata(
        artist=_effective(records, "artist", view, "original_artist"),
        title=_effective(records, "title", view, "original_title"),
        album=_optional_effective(records, "album", "original_album"),
        album_artist=_optional_effective(records, "album_artist", "original_album_artist"),
        track_number=view.optional_integer("track_number"),
        disc_number=view.optional_integer("disc_number"),
        genre=view.optional_text("genre"),
        date=view.optional_text("date"),
        duration=view.duration("duration_seconds"),
        musicbrainz_artist_id=view.optional_text("musicbrainz_id"),
        musicbrainz_release_id=view.optional_text("musicbrainz_release_id"),
        musicbrainz_recording_id=view.optional_text("musicbrainz_recording_id"),
        original={
            name: str(item["original"]) for name, item in records.items() if item["original"]
        },
    )


def to_provenance(row: sqlite3.Row) -> dict[str, object]:
    """A `metadata` row, for the admin page that shows where a value came from."""

    view = RowView(row, "metadata")
    return {
        "field": view.text("field"),
        "original": view.optional_text("original"),
        "effective": view.optional_text("effective"),
        "source": view.text("source"),
        "repaired": bool(view.integer("repaired")),
    }


def _artwork_id(view: RowView) -> ArtworkId | None:
    value = view.optional_integer("artwork_id")
    return None if value is None else ArtworkId(value)


def _effective(records: dict[str, sqlite3.Row], field: str, view: RowView, column: str) -> str:
    record = records.get(field)
    if record is not None and record["effective"]:
        return str(record["effective"])
    return view.text(column)


def _optional_effective(
    records: dict[str, sqlite3.Row], field: str, _original_column: str
) -> str | None:
    """The value that applied, then the value the file carried, then nothing.

    The order matters in the reporting direction: `Metadata` answers "what did the tag
    say", so a field normalization filled in from the path still has to be able to
    show what was there before.
    """

    record = records.get(field)
    if record is not None:
        if record["effective"]:
            return str(record["effective"])
        if record["original"]:
            return str(record["original"])
    return None
