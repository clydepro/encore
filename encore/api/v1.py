"""`/api/v1` — the machine-readable guest interface (SAPRS 10.2, 10.3, 10.4).

Every route here is a service call with a status code on it. There is no rule in this
file to review: the rules live in `encore/services/` and `encore/search/`, and what a
reader should be able to check line by line is only that the right one is reached and the
right answer is shaped.

Three things are deliberate:

* **Reads happen on the appliance thread** (ADR-012) and are assembled from the same
  `PlayerView` the fragments render, so `/api/v1/queue` and `/fragments/queue` cannot
  describe two different queues — which is the failure SAPRS 10.1's "two complementary
  interfaces" invites.
* **Queueing is non-idempotent and says so.** SAPRS 10.4 requires two identical requests
  to create two items, so nothing here accepts an idempotency key; the answer is a 201
  naming the item that now exists.
* **The only write a guest has is to the queue.** Playback controls are administrative
  (SAPRS 8.8) and belong behind step 14's authentication, so they are absent rather than
  present and open.

Missing things, stated rather than discovered: `DELETE /api/v1/queue/{id}` and the
playback commands are the administrator's (step 14); `/api/v1/qr` is deferred with the
QR page to step 14 (issue #25); `StatisticsService` has no endpoint yet because it has no
caller yet.
"""

from __future__ import annotations

from functools import partial
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel

from encore.api import views
from encore.api.deps import Jukebox, jukebox_of, read
from encore.api.rows import SongRow
from encore.api.schemas import (
    AlbumModel,
    ArtistModel,
    HealthModel,
    NowPlayingModel,
    QueueAcceptedModel,
    QueueModel,
    SearchModel,
    SongModel,
)
from encore.domain import Album, AlbumId, Artist, ArtistId, QueueItem, Song, SongId

__all__ = ["router"]

router = APIRouter(prefix="/api/v1", tags=["v1"])

Juke = Annotated[Jukebox, Depends(jukebox_of)]

#: The widest `q=` Encore will parse. A guard against a megabyte of text reaching an
#: FTS5 expression, and the whole of SAPRS 10.9's "reasonable protection" for search in
#: v1 alongside the queue's own ceiling.
MAX_QUERY_LENGTH = 200


@router.get("/search", response_model=SearchModel, summary="Search artists, albums and songs")
async def search(
    jukebox: Juke,
    q: Annotated[str, Query(min_length=1, max_length=MAX_QUERY_LENGTH)],
    limit: Annotated[int | None, Query(ge=1, le=500, description="Per-list cap.")] = None,
) -> SearchModel:
    context = await read(jukebox.appliance, partial(views.results_context, jukebox, q, limit=limit))
    return SearchModel.of(context["results"], context["songs"])


@router.get("/artists", response_model=list[ArtistModel], summary="Browse artists")
async def artists(
    jukebox: Juke,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int | None, Query(ge=1, le=views.MAX_PAGE_LIMIT)] = None,
) -> list[ArtistModel]:
    page = await read(
        jukebox.appliance,
        partial(jukebox.library.artists, offset=offset, limit=limit or views.BROWSE_LIMIT),
    )
    return [model for model in (ArtistModel.of(artist) for artist in page.items) if model]


@router.get("/artists/{artist_id}", response_model=dict, summary="One artist's screen")
async def artist(jukebox: Juke, artist_id: Annotated[int, Path(ge=1)]) -> dict[str, Any]:
    context = await read(jukebox.appliance, partial(_artist, jukebox, ArtistId(artist_id)))
    found: Artist | None = context["artist"]
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "that artist is not in the library")
    return {
        "artist": _dump(ArtistModel.of(found)),
        "albums": [_dump(AlbumModel.of(album, found)) for album in context["albums"]],
        "tracks": [SongModel.of(row).model_dump() for row in context["tracks"]],
    }


@router.get("/albums/{album_id}", response_model=dict, summary="One album and its tracks")
async def album(jukebox: Juke, album_id: Annotated[int, Path(ge=1)]) -> dict[str, Any]:
    context = await read(jukebox.appliance, partial(_album, jukebox, AlbumId(album_id)))
    found: Album | None = context["album"]
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "that album is not in the library")
    return {
        "album": _dump(AlbumModel.of(found, context["artist"])),
        "tracks": [SongModel.of(row).model_dump() for row in context["tracks"]],
    }


@router.get("/songs/{song_id}", response_model=SongModel, summary="One track, labelled")
async def song(jukebox: Juke, song_id: Annotated[int, Path(ge=1)]) -> SongModel:
    row: SongRow | None = await read(
        jukebox.appliance, partial(views.song_row, jukebox, SongId(song_id))
    )
    if row is None or row.song is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "that song is not in the library")
    return SongModel.of(row)


@router.get("/queue", response_model=QueueModel, summary="Up Next, with Now Playing")
async def get_queue(jukebox: Juke) -> QueueModel:
    return QueueModel.of(await read(jukebox.appliance, partial(views.player, jukebox)))


@router.post(
    "/queue",
    response_model=QueueAcceptedModel,
    status_code=status.HTTP_201_CREATED,
    summary="Ask for a song (non-idempotent, SAPRS 10.4)",
)
async def enqueue(
    jukebox: Juke,
    song_id: Annotated[int, Query(ge=1, description="The track to queue.")],
) -> QueueAcceptedModel:
    """Queue one song.

    The `?song_id=` shape keeps the interface a single POST with no body to guess; a
    JSON body belongs with the batch endpoints in 1.1. What the specification cares about
    is that this is not idempotent (SAPRS 10.4), and it is not: every call appends.
    """

    item, view = await read(jukebox.appliance, partial(_enqueue, jukebox, SongId(song_id)))
    row = views.row_for_item(view, int(item.id))
    return QueueAcceptedModel(
        item_id=int(item.id),
        position=item.position,
        queue_length=view.length,
        state=view.queue_state,
        song=None if row is None else SongModel.of(row),
    )


@router.get("/playing", response_model=NowPlayingModel, summary="Now Playing, as data")
async def playing(jukebox: Juke) -> NowPlayingModel:
    view = await read(jukebox.appliance, partial(views.player, jukebox))
    return NowPlayingModel(
        state=view.state.value,
        audible=view.audible,
        song=None if view.now is None else SongModel.of(view.now),
        position_ms=round(view.position.total_seconds() * 1000),
        duration_ms=round(view.duration.total_seconds() * 1000),
        fraction=round(view.fraction, 4),
        queue_length=view.length,
    )


@router.get("/health", response_model=HealthModel, summary="Aggregate health")
async def health(jukebox: Juke) -> HealthModel:
    """The last computed aggregate, not a fresh probe.

    A readiness endpoint that polled every component would turn a liveness probe from a
    systemd timer into a stress test of the engine. The appliance's tick recomputes the
    aggregate every second (SAPRS 7.5), so the freshest answer is already there, and
    `checked_at` says how fresh.
    """

    return HealthModel.of(jukebox.health.snapshot)


@router.get("/info", response_model=dict, summary="What this appliance is")
async def info(jukebox: Juke) -> dict[str, Any]:
    counts = await read(jukebox.appliance, jukebox.library.counts)
    return {
        "name": "Encore",
        "version": jukebox.version,
        "url": f"http://{jukebox.config.server.domain}/",
        "songs": counts.get("songs", 0),
        "albums": counts.get("albums", 0),
        "artists": counts.get("artists", 0),
        "search_available": jukebox.library.search_available,
        "queue_max_items": jukebox.config.queue.max_items,
    }


# -- what runs on the appliance thread ----------------------------------------


def _dump(model: BaseModel | None) -> dict[str, Any]:
    """A model that is there, as JSON-ready data.

    `ArtistModel.of` and `AlbumModel.of` accept an optional entity — a search result set has
    rows whose label may be missing — and return `None` with it. The routes that call this
    have already 404ed on the missing case, so the assertion is for the checker rather than
    for the reader, and it is written as a raise so a future refactor that loses the 404 fails
    loudly instead of printing `null`.
    """

    if model is None:  # pragma: no cover - the route above already refused
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not in the library")
    data: dict[str, Any] = model.model_dump()
    return data


def _artist(jukebox: Jukebox, artist_id: ArtistId) -> dict[str, Any]:
    return views.artist_context(jukebox, artist_id)


def _album(jukebox: Jukebox, album_id: AlbumId) -> dict[str, Any]:
    return views.album_context(jukebox, album_id)


def _enqueue(jukebox: Jukebox, song_id: SongId) -> tuple[QueueItem, views.PlayerView]:
    """Queue, then read once for the answer.

    The second read is not a race with the first: both run on the appliance thread
    (ADR-012), so no other request can slip between the append and the report, and the
    `position` and `queue_length` a guest is handed describe the queue they just changed.
    """

    return jukebox.queue.enqueue(song_id), views.player(jukebox)


_ = Song
