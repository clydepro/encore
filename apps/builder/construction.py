"""Stage 7 — database construction (SAPRS 5.3, 6.2; ADR-010's write path).

Turns tracks into rows, in a file inside `paths.temp_dir`. This is the only module
that writes `library.db`, and it never writes the published one: SAPRS 5.8 says a
partially constructed library must never become the active one, and ADR-010 puts
that consequence in `apps/builder/` rather than in the package the Server imports
for reads.

Three decisions are made here rather than in normalization, and the split is
deliberate. Normalization answers "what does this file say?"; construction answers
"what does the library contain?", which is a question about grouping.

* **Which spellings are one artist.** Grouped on the folded name and displayed with
  the majority spelling. `AC/DC`, `AC-DC` and `ac dc` are one artist, and inventing
  a canonical form that nobody wrote would give them a fourth name. Ties break
  alphabetically, because `Counter.most_common()` breaks ties by insertion order and
  insertion order is directory order, which would let two builds of one library name
  the same artist two ways.
* **What an album is.** `(grouping artist, folded album title)`. The album artist
  wins because a compilation is one album with many artists rather than forty
  albums with one each (SAPRS 4.3, ADR-010).
* **What a missing album becomes.** `[Unknown Album]`, recorded with
  `source = constant`, because SAPRS 6.4 says "no album tag" and `[Unknown Album]`
  are different facts and the difference has to survive into the database. That
  table of differences is the only answer available later to ADR-010's "why is this
  song under this artist?", which is why it is written unconditionally rather than
  when something looks wrong.

Foreign keys are on for the whole run: an id pointing at nothing fails here, in the
Builder, rather than on a guest's phone during their first navigation.
"""
# ruff: noqa: S608 — table and column names come from `contract.py` and from the
# module's own constants; no path, tag or user input reaches these strings, which are
# parameterised with `?` everywhere a value is involved (SAPRS 6.5).

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from apps.builder.normalization import collapse, fold, sort_key
from apps.builder.records import ArtworkOutcome, FieldProvenance, Track
from encore.repositories.contract import MetadataField, MetadataSource, Table

__all__ = ["UNKNOWN_ALBUM", "BuiltLibrary", "build"]

#: What a track with no album is filed under. Brackets because every tagger
#: already writes it that way, so the library and a phone agree.
UNKNOWN_ALBUM: Final = "[Unknown Album]"


@dataclass(frozen=True, slots=True, kw_only=True)
class BuiltLibrary:
    """What the write produced, as counts rather than as a query.

    Taken from the write rather than recounted afterwards: the disagreement between
    these numbers and the ones `validation.py` reads back is the finding that stage
    exists to report.
    """

    songs: int = 0
    artists: int = 0
    albums: int = 0
    files: int = 0
    artwork: int = 0
    provenance: int = 0
    repaired: int = 0
    grouping: _Grouping = field(default_factory=lambda: _Grouping(tracks=()))


def build(
    connection: sqlite3.Connection,
    tracks: Sequence[Track],
    *,
    artwork: ArtworkOutcome,
    rules_version: int,
) -> BuiltLibrary:
    """Write every row of the canonical library into an open connection.

    Args:
        connection: A connection to an empty file, schema already created. Owned by
            the pipeline, which decides where the transaction ends — this function
            must not commit, because an uncommitted half-build is a rollback rather
            than a broken artifact (SAPRS 6.11).
        tracks: Catalogable tracks only. A track with no artist or no title never
            gets here; ADR-010 makes it a reported skip.
        artwork: What the artwork stage wrote, so a reference has a file behind it.
        rules_version: `NORMALIZATION_RULES_VERSION`, stored per file so an
            incremental run can tell a changed file from a changed rule (ADR-010).

    Returns:
        The counts, plus the grouping map the search and validation stages read.
    """

    connection.execute("PRAGMA foreign_keys = ON")
    state = _Grouping(tracks=tuple(tracks), artwork=artwork)
    _write_artwork(connection, state)
    _write_artists(connection, state)
    _write_albums(connection, state)
    files, songs = _write_files_and_songs(connection, state, rules_version=rules_version)
    provenance, repaired = _write_provenance(connection, state)
    _associate_artwork(connection, state)
    return BuiltLibrary(
        songs=songs,
        artists=len(state.artist_ids),
        albums=len(state.album_ids),
        files=files,
        artwork=len(state.artwork_ids),
        provenance=provenance,
        repaired=repaired,
        grouping=state,
    )


class _Grouping:
    """The grouping decisions, computed once from the tracks.

    An object rather than six returned dicts because three stages need four of them
    and a signature taking six positional mappings is one somebody will transpose.
    """

    def __init__(
        self, *, tracks: Sequence[Track] = (), artwork: ArtworkOutcome | None = None
    ) -> None:
        self.tracks: tuple[Track, ...] = tuple(tracks)
        self.artwork: ArtworkOutcome = artwork if artwork is not None else ArtworkOutcome()
        self.artist_ids: dict[str, int] = {}
        self.album_ids: dict[tuple[str, str], int] = {}
        self.artwork_ids: dict[str, int] = {}
        self.spelling: dict[str, Counter[str]] = defaultdict(Counter)
        self.album_spelling: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
        self.album_dates: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
        self.album_artwork: dict[tuple[str, str], int] = {}
        for track in self.tracks:
            artist, album = self.key_for(track)
            self.spelling[artist][track.artist] += 1
            self.album_spelling[(artist, album)][track.album or ""] += 1
            if track.date:
                self.album_dates[(artist, album)][collapse(track.date)] += 1

    @staticmethod
    def key_for(track: Track) -> tuple[str, str]:
        """`(artist key, album key)` — the folded identity this track files under."""

        return (fold(track.artist), fold(track.album or ""))

    def artist_id_for(self, key: str) -> int | None:
        return self.artist_ids.get(key)

    def album_id_for(self, key: tuple[str, str]) -> int | None:
        return self.album_ids.get(key)


def _write_artwork(connection: sqlite3.Connection, state: _Grouping) -> None:
    """Insert the images the artwork stage wrote.

    Artist images are not here: an artist's picture is a decision about a grouping,
    and the grouping does not have an id until the artists table does, so
    `_associate_artwork` derives it from the artist's albums instead.
    """

    for asset in state.artwork.assets:
        cursor = connection.execute(
            f"INSERT INTO {Table.ARTWORK} (kind, relative_path, width, height, sha256, source_path)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                asset.kind,
                asset.relative_path.as_posix(),
                asset.width,
                asset.height,
                asset.sha256,
                None if asset.source_path is None else str(asset.source_path),
            ),
        )
        state.artwork_ids[asset.sha256] = int(cursor.lastrowid or 0)


def _write_artists(connection: sqlite3.Connection, state: _Grouping) -> None:
    """One row per folded artist, in sorted order, so ids are reproducible."""

    for name in sorted(state.spelling):
        display = _display(state.spelling[name])
        if not display:
            continue
        cursor = connection.execute(
            f"INSERT INTO {Table.ARTISTS} (name, sort_name, normalized_name) VALUES (?, ?, ?)",
            (display, sort_key(display), name),
        )
        state.artist_ids[name] = int(cursor.lastrowid or 0)


def _write_albums(connection: sqlite3.Connection, state: _Grouping) -> None:
    """One row per `(grouping artist, folded album)`."""

    for key in sorted(state.album_spelling):
        artist_id = state.artist_id_for(key[0])
        if artist_id is None:
            continue
        display = _display(state.album_spelling[key]) or UNKNOWN_ALBUM
        date = _display(state.album_dates[key], allow_empty=True)
        cursor = connection.execute(
            f"INSERT INTO {Table.ALBUMS} (artist_id, title, sort_title, normalized_title, release_date)"
            " VALUES (?, ?, ?, ?, ?)",
            (artist_id, display, sort_key(display), key[1], date or None),
        )
        state.album_ids[key] = int(cursor.lastrowid or 0)


def _write_files_and_songs(
    connection: sqlite3.Connection, state: _Grouping, *, rules_version: int
) -> tuple[int, int]:
    """Insert one music file and one song per track, in path order."""

    files = 0
    songs = 0
    for track in sorted(state.tracks, key=lambda item: str(item.path)):
        artist_key, album_key = state.key_for(track)
        artist_id = state.artist_id_for(artist_key) or 0
        album_id = state.album_ids.get((artist_key, album_key)) or 0
        asset = state.artwork.by_file.get(track.path)
        artwork_id = None if asset is None else state.artwork_ids.get(asset.sha256)
        file = track.extracted.file
        cursor = connection.execute(
            f"INSERT INTO {Table.MUSIC_FILES} (path, format, duration_seconds, size_bytes, mtime_ns,"
            " tag_fingerprint, embedded_artwork_sha, rules_version, cache_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(file.path),
                file.format.value,
                track.duration.total_seconds(),
                file.size_bytes,
                file.mtime_ns,
                track.extracted.tag_hash,
                None if asset is None else asset.sha256,
                rules_version,
                track.extracted.cache_key,
            ),
        )
        file_id = int(cursor.lastrowid or 0)
        files += 1
        connection.execute(
            f"INSERT INTO {Table.SONGS} (title, sort_title, normalized_title, artist_id, album_id,"
            " music_file_id, track_number, disc_number, genre, date, musicbrainz_recording_id,"
            " artwork_id, original_artist, original_album, original_title) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                collapse(track.title),
                sort_key(track.title),
                fold(track.title),
                artist_id,
                album_id,
                file_id,
                track.track_number,
                track.disc_number,
                track.genre,
                track.date,
                track.musicbrainz_recording_id,
                artwork_id,
                track.extracted.tags.get(MetadataField.ARTIST),
                track.extracted.tags.get(MetadataField.ALBUM),
                track.extracted.tags.get(MetadataField.TITLE),
            ),
        )
        songs += 1
        if album_id and artwork_id:
            state.album_artwork.setdefault((artist_key, album_key), artwork_id)
    return files, songs


def _write_provenance(connection: sqlite3.Connection, state: _Grouping) -> tuple[int, int]:
    """Record where each field's value came from (ADR-010).

    Written from the tracks rather than from the rows, because the row holds the
    conclusion and the track holds the argument.
    """

    rows = connection.execute(
        f"SELECT s.id, f.path FROM {Table.SONGS} AS s"
        f" JOIN {Table.MUSIC_FILES} AS f ON f.id = s.music_file_id"
    ).fetchall()
    by_path = {str(path): int(song_id) for song_id, path in rows}
    batch: list[tuple[int, str, str | None, str | None, str, int]] = []
    for track in state.tracks:
        song_id = by_path.get(str(track.path))
        if song_id is None:
            continue
        records: dict[str, FieldProvenance] = dict(track.provenance)
        _add_constant_album(records, track)
        for name in sorted(records):
            record = records[name]
            batch.append(
                (
                    song_id,
                    name,
                    record.original,
                    record.effective,
                    record.source,
                    int(record.repaired),
                )
            )
    connection.executemany(
        f"INSERT OR IGNORE INTO {Table.METADATA} (song_id, field, original, effective, source, repaired)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        batch,
    )
    return len(batch), sum(1 for row in batch if row[4] == MetadataSource.MUSICBRAINZ)


def _add_constant_album(records: dict[str, FieldProvenance], track: Track) -> None:
    """Note the album the Builder supplied, when the file did not have one."""

    if fold(track.album or ""):
        return
    from apps.builder.records import FieldProvenance

    records[MetadataField.ALBUM] = FieldProvenance(
        field=MetadataField.ALBUM,
        original=track.extracted.tags.get(MetadataField.ALBUM),
        effective=UNKNOWN_ALBUM,
        source=MetadataSource.CONSTANT,
        repaired=False,
    )


def _associate_artwork(connection: sqlite3.Connection, state: _Grouping) -> None:
    """Point albums at their covers, and artists at the cover they are known by."""

    for (artist_key, album_key), artwork_id in sorted(state.album_artwork.items()):
        album_id = state.album_ids.get((artist_key, album_key))
        if album_id is not None:
            connection.execute(
                f"UPDATE {Table.ALBUMS} SET artwork_id = COALESCE(artwork_id, ?) WHERE id = ?",
                (artwork_id, album_id),
            )
    # The artist's picture is their earliest-named album's cover, which is what a
    # listener means by it and what every other service will show.
    connection.execute(
        f"UPDATE {Table.ARTISTS} SET artwork_id = ("
        f" SELECT a.artwork_id FROM {Table.ALBUMS} AS a WHERE a.artist_id = {Table.ARTISTS}.id"
        " AND a.artwork_id IS NOT NULL ORDER BY a.sort_title, a.title LIMIT 1)"
        f" WHERE artwork_id IS NULL AND id IN ("
        f" SELECT a2.artist_id FROM {Table.ALBUMS} AS a2 WHERE a2.artwork_id IS NOT NULL)"
    )


def _display(counts: Counter[str], *, allow_empty: bool = False) -> str:
    """The spelling a group is shown by: most frequent, ties broken alphabetically."""

    usable = {value: count for value, count in counts.items() if allow_empty or value}
    if not usable:
        return ""
    best = max(usable.values())
    return sorted(value for value, count in usable.items() if count == best)[0]
