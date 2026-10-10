"""One labelled track, rendered two ways (SAPRS 9.6, 9.7, 9.9, 10.3).

A fragment and a JSON response display the same thing: a song with the artist and
album that identify it, because "Song Titles" is a list nobody can choose from. This
module builds that row once, so the two interfaces cannot disagree about what a track
looks like — which is the failure SAPRS 10.1's "two complementary interfaces" invites.

The rule that keeps it honest: **a row costs zero extra reads per item.** Every
builder here takes a collection and does at most two batch loads (artists, albums),
the same trick `encore/search/` uses for its labels. A `row_for_song` that issued a
query per row would turn a 50-row search page into 150 queries and spend SAPRS 1.8's
100 ms budget on presentation.

Nothing here decides anything about music. It resolves references for display.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from encore.domain import Album, AlbumId, Artist, ArtistId, Song, SongId
from encore.search.results import MatchedField, SearchResults
from encore.services import LibraryService, QueueEntry

__all__ = [
    "SongRow",
    "row_for_song",
    "rows_for_album",
    "rows_for_artist",
    "rows_for_entries",
    "rows_for_results",
    "rows_for_songs",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class SongRow:
    """One track as a screen shows it.

    Attributes:
        song: The thing itself. `None` is representable on purpose: a queued song
            whose library row is gone (a rebuild between the request and the screen,
            ADR-009) still occupies a position in Up Next and must be *rendered* as
            unavailable rather than dropped from the list.
        artist, album: Labels, batch-loaded. `None` only when `song` is.
        item_id, position, status: The queue's own facts, present on a row that came
            from the queue. Kept on the row rather than in a parallel structure so a
            template that shows position next to title cannot get the pairing wrong.
        artwork_url: The appliance's path to a picture, or the placeholder. Song-addressed
            because the repository already falls back to the album cover (SAPRS 6.7).
        matched_on: Which fields a query hit, for search rows (SAPRS 9.6).
    """

    song: Song | None
    artist: Artist | None = None
    album: Album | None = None
    item_id: int | None = None
    position: int | None = None
    status: str | None = None
    enqueued_at: datetime | None = None
    matched_on: frozenset[MatchedField] = frozenset()

    @property
    def available(self) -> bool:
        return self.song is not None

    @property
    def title(self) -> str:
        """What the row's first line says, in both interfaces."""

        if self.song is None:
            return "A song that is no longer in the library"
        return self.song.title

    @property
    def subtitle(self) -> str:
        """Artist — album. The line SAPRS 9.6 asks for, assembled in one place."""

        if self.song is None:
            return ""
        artist = self.artist.name if self.artist else "Unknown artist"
        album = self.album.title if self.album else "Unknown album"
        return f"{artist} — {album}"

    @property
    def artwork_url(self) -> str:
        if self.song is None:
            return "/static/img/no-cover.svg"
        return f"/artwork/song/{self.song.id}"

    @property
    def key(self) -> str:
        """A stable DOM id for the row, used by the live region's swap target.

        Addressed by the queue item rather than the song, because the same song can be
        in the list twice (SAPRS 8.3) and two rows with one id is how a queue redraws
        the wrong line.
        """

        if self.item_id is not None:
            return f"item-{self.item_id}"
        return f"song-{0 if self.song is None else int(self.song.id)}"


def rows_for_entries(entries: Sequence[QueueEntry], library: LibraryService) -> list[SongRow]:
    """Up Next / Now Playing rows, with labels loaded in two queries (SAPRS 9.9)."""

    songs = [entry.song for entry in entries if entry.song is not None]
    artists = _artists(songs, library)
    albums = _albums(songs, library)
    return [
        SongRow(
            song=entry.song,
            artist=None if entry.song is None else artists.get(entry.song.artist_id),
            album=None if entry.song is None else albums.get(entry.song.album_id),
            item_id=int(entry.item.id),
            position=entry.item.position,
            status=entry.item.status.value,
            enqueued_at=entry.item.enqueued_at,
        )
        for entry in entries
    ]


def rows_for_results(results: SearchResults) -> list[SongRow]:
    """Search song hits. The labels are already on the result (SAPRS 9.6)."""

    return [
        SongRow(
            song=result.song,
            artist=result.artist,
            album=result.album,
            matched_on=result.matched_on,
        )
        for result in results.songs
    ]


def rows_for_songs(songs: Sequence[Song], library: LibraryService) -> list[SongRow]:
    """An album or artist's track list, labelled (SAPRS 9.5)."""

    artists = _artists(songs, library)
    albums = _albums(songs, library)
    return [
        SongRow(
            song=song,
            position=song.track_number,
            artist=artists.get(song.artist_id),
            album=albums.get(song.album_id),
        )
        for song in songs
    ]


def rows_for_album(album_id: AlbumId, library: LibraryService) -> list[SongRow]:
    return rows_for_songs(library.songs_for_album(album_id), library)


def rows_for_artist(artist_id: ArtistId, library: LibraryService) -> list[SongRow]:
    return rows_for_songs(library.songs_for_artist(artist_id), library)


def row_for_song(song_id: SongId, library: LibraryService) -> SongRow | None:
    song = library.song(song_id)
    if song is None:
        return None
    return SongRow(
        song=song, artist=library.artist(song.artist_id), album=library.album(song.album_id)
    )


def _artists(songs: Iterable[Song], library: LibraryService) -> dict[ArtistId, Artist]:
    ids = [ArtistId(value) for value in _unique(int(song.artist_id) for song in songs)]
    return {artist.id: artist for artist in library.artists_by_ids(ids)}


def _albums(songs: Iterable[Song], library: LibraryService) -> dict[AlbumId, Album]:
    ids = [AlbumId(value) for value in _unique(int(song.album_id) for song in songs)]
    return {album.id: album for album in library.albums_by_ids(ids)}


def _unique(values: Iterable[int]) -> frozenset[int]:
    """Distinct ids. `IN ()` is a syntax error on an empty list, which is why the
    batch loads are skipped rather than called with nothing."""

    return frozenset(values)
