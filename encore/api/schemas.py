"""The JSON API's response models (SAPRS 10.2, 10.3, AEP 21).

These are wire shapes, not the domain: a `SongModel` exists so `duration_ms` can be an
integer (the convention `docs/api/README.md` fixes) where `domain.Song` carries a
`timedelta`, and so a field can be *added* to the wire without touching anything that
plays music. Within a major version the models are additive only (AEP 21), which is why
each declares `extra="forbid"`: a model that silently absorbs a typo'd field is a model
that will one day absorb a rename.

Every converter takes a `SongRow` — the labelled track `encore/api/rows.py` builds — so
the JSON API and the HTMX fragments display the same string for the same song. A route
that assembled its own labels would be a second vocabulary for one thing, and the two
would disagree the first week a track appeared on two albums.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from encore.api.rows import SongRow
from encore.api.views import PlayerView
from encore.domain import Album, Artist
from encore.search import SearchResults
from encore.search.results import AlbumResult
from encore.services.health_service import HealthSnapshot

__all__ = [
    "AlbumModel",
    "ArtistModel",
    "HealthModel",
    "NowPlayingModel",
    "QueueAcceptedModel",
    "QueueModel",
    "SearchModel",
    "SongModel",
]

_WIRE = ConfigDict(extra="forbid")


class ArtistModel(BaseModel):
    """An artist, with what a screen needs beside the name (SAPRS 4.2)."""

    model_config = _WIRE

    id: int
    name: str
    artwork_url: str | None = None

    @classmethod
    def of(cls, artist: Artist | None) -> ArtistModel | None:
        if artist is None:
            return None
        return cls(
            id=int(artist.id),
            name=artist.name,
            artwork_url=(
                None if artist.artwork_id is None else f"/artwork/artist/{int(artist.id)}"
            ),
        )


class AlbumModel(BaseModel):
    """An album, labelled with its artist so a row can be read (SAPRS 4.3, 9.6)."""

    model_config = _WIRE

    id: int
    title: str
    artist: ArtistModel | None = None
    artwork_url: str | None = None

    @classmethod
    def of(cls, album: Album | None, artist: Artist | None = None) -> AlbumModel | None:
        if album is None:
            return None
        return cls(
            id=int(album.id),
            title=album.title,
            artist=ArtistModel.of(artist),
            artwork_url=None if album.artwork_id is None else f"/artwork/album/{int(album.id)}",
        )

    @classmethod
    def of_result(cls, result: AlbumResult) -> AlbumModel | None:
        return cls.of(result.album, result.artist)


class SongModel(BaseModel):
    """A track, labelled the way a jukebox labels a track (SAPRS 4.4, 9.6).

    `artist` and `album` are embedded rather than addressed because a guest reads
    "Artist — Song" and picks the wrong one of two same-titled tracks otherwise.
    """

    model_config = _WIRE

    id: int
    title: str
    artist: ArtistModel | None = None
    album: AlbumModel | None = None
    duration_ms: int
    track_number: int | None = None
    artwork_url: str
    position: int | None = Field(
        default=None, description="Place in a list that is not the queue: an album track, a page."
    )
    matched_on: list[str] = Field(
        default_factory=list, description="Which fields a query hit (SAPRS 9.6)."
    )
    available: bool = Field(
        default=True, description="False is a queued song the rebuilt library no longer has."
    )

    @classmethod
    def of(cls, row: SongRow) -> SongModel:
        return cls(
            id=0 if row.song is None else int(row.song.id),
            title=row.title,
            artist=ArtistModel.of(row.artist),
            album=AlbumModel.of(row.album, row.artist),
            duration_ms=0 if row.song is None else round(row.song.duration.total_seconds() * 1000),
            track_number=None if row.song is None else row.song.track_number,
            artwork_url=row.artwork_url,
            position=row.position,
            matched_on=sorted(field.value for field in row.matched_on),
            available=row.available,
        )


class QueueEntryModel(SongModel):
    """One FIFO row, with the item's own identity beside the song's (SAPRS 4.5)."""

    model_config = _WIRE

    item_id: int | None = None
    status: str | None = None
    enqueued_at: datetime | None = None

    @classmethod
    def of(cls, row: SongRow) -> QueueEntryModel:
        base = SongModel.of(row)
        return cls(
            **base.model_dump(),
            item_id=row.item_id,
            status=row.status,
            enqueued_at=row.enqueued_at,
        )


class QueueModel(BaseModel):
    """ "Up Next", with the numbers a screen says beside it (SAPRS 9.9)."""

    model_config = _WIRE

    now_playing: QueueEntryModel | None = None
    up_next: list[QueueEntryModel] = Field(default_factory=list)
    length: int = Field(description="Items in the FIFO, including the one on the speakers.")
    max_items: int = Field(description="`queue.max_items`, which SAPRS 8.4 requires be visible.")
    more: int = Field(default=0, description="Waiting rows past `up_next`, when it is capped.")
    wait_ms: int = Field(description="How long the front of the queue has been waiting.")

    @classmethod
    def of(cls, view: PlayerView) -> QueueModel:
        return cls(
            now_playing=None if view.now is None else QueueEntryModel.of(view.now),
            up_next=[QueueEntryModel.of(row) for row in view.up_next],
            length=view.length,
            max_items=view.max_items,
            more=view.more_waiting,
            wait_ms=round(view.wait.total_seconds() * 1000),
        )


class QueueAcceptedModel(BaseModel):
    """What a guest's tap cost, in the two words SAPRS 9.7 asks for."""

    model_config = _WIRE

    item_id: int
    position: int
    queue_length: int
    state: str = Field(pattern="^(playing|queued|idle)$")
    song: SongModel | None = None


class NowPlayingModel(BaseModel):
    """SAPRS 9.8's live view as data: what, how far, in what state."""

    model_config = _WIRE

    state: str
    audible: bool
    song: SongModel | None = None
    position_ms: int
    duration_ms: int
    fraction: float
    queue_length: int = 0


class SearchModel(BaseModel):
    """One query, three lists, and an honest statement about the cap (SAPRS 9.6)."""

    model_config = _WIRE

    query: str
    songs: list[SongModel] = Field(default_factory=list)
    albums: list[AlbumModel] = Field(default_factory=list)
    artists: list[ArtistModel] = Field(default_factory=list)
    limit: int = Field(description="The per-list cap that was applied.")
    truncated: bool = Field(
        description="True when a list was cut at `limit`, so a client can say 'more matched'."
    )

    @classmethod
    def of(cls, results: SearchResults, songs: Sequence[SongRow]) -> SearchModel:
        """From the service's own answer, plus the rows that label its song hits."""

        return cls(
            query=results.query.text,
            songs=[SongModel.of(row) for row in songs],
            albums=[
                model
                for model in (
                    AlbumModel.of(result.album, result.artist) for result in results.albums
                )
                if model is not None
            ],
            artists=[
                model
                for model in (ArtistModel.of(result.artist) for result in results.artists)
                if model is not None
            ],
            limit=results.limit,
            truncated=results.truncated,
        )


class HealthComponentModel(BaseModel):
    """One component's report (SAPRS 1.5's "clear health information")."""

    model_config = _WIRE

    component: str
    status: str
    detail: str = ""


class HealthModel(BaseModel):
    """The aggregate, plus the reasons behind it."""

    model_config = _WIRE

    status: str
    ready: bool
    checked_at: datetime
    detail: str = ""
    components: list[HealthComponentModel] = Field(default_factory=list)

    @classmethod
    def of(cls, snapshot: HealthSnapshot) -> HealthModel:
        return cls(
            status=snapshot.status.value,
            ready=snapshot.is_ready,
            checked_at=snapshot.checked_at,
            detail=snapshot.detail,
            components=[
                HealthComponentModel(
                    component=report.component, status=report.status.value, detail=report.detail
                )
                for report in snapshot.components
            ],
        )
