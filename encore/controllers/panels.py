"""HTMX fragments: the same views, without the shell (SAPRS 9.3, 9.8-9.10, 10.6).

Every region the live view redraws has a URL, for three reasons that are all about the
SSE stream being unreliable in the way networks are:

* A swap that arrives at a browser which has been asleep is a swap of a page that no
  longer exists; a browser that reconnects asks for the regions it can see instead.
* HTMX's `hx-get` is how a page lazy-loads a region it did not need on first paint —
  the album list on an artist screen, for instance, where SAPRS 9.5's "one interaction"
  is about the user's patience rather than the server's query count.
* And a fragment is the smallest thing worth measuring. SAPRS 1.8's 200 ms navigation
  budget is stated against "a typical HTMX navigation", and this file is where that
  sentence points.

They render the same context builders the page routes use (`encore/api/views.py`), so a
fragment and the page that contains it cannot show two different queues. That is the whole
of SAPRS 10.6's "HTML fragments designed for direct DOM replacement" — a fragment is a
page's region, not a second interface invented for the client that asked.
"""

from __future__ import annotations

from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status

from encore.api import views
from encore.api.deps import Jukebox, jukebox_of, read, renderer_of
from encore.api.html import Renderer
from encore.domain import AlbumId, ArtistId

__all__ = ["router"]

router = APIRouter(prefix="/fragments", tags=["fragments"])

Juke = Annotated[Jukebox, Depends(jukebox_of)]
Render = Annotated[Renderer, Depends(renderer_of)]


@router.get("/player", summary="Now Playing and Up Next together")
async def player(request: Request, jukebox: Juke, renderer: Render) -> Response:
    """The region a guest's phone spends most of its life looking at."""

    return await _render(request, jukebox, renderer, "panels/player.html")


@router.get("/alerts", summary="The health and status strip")
async def alerts(request: Request, jukebox: Juke, renderer: Render) -> Response:
    """The strip, freshly checked.

    `check()` rather than the last snapshot: a screen that asks "are we alright" is
    entitled to a current answer, and the poll is a property read on each component rather
    than a command. The publish-if-moved it performs belongs here for the reason it belongs
    in the tick — whoever reads health is the one who may have to announce it.
    """

    snapshot = await read(jukebox.appliance, jukebox.health.check)
    return await _render(request, jukebox, renderer, "panels/alerts.html", health=snapshot)


@router.get("/up-next", summary="The queue, waiting")
async def up_next(request: Request, jukebox: Juke, renderer: Render) -> Response:
    return await _render(request, jukebox, renderer, "panels/up_next.html")


@router.get("/now-playing", summary="The row on the speakers")
async def now_playing(request: Request, jukebox: Juke, renderer: Render) -> Response:
    return await _render(request, jukebox, renderer, "panels/now_playing.html")


@router.get("/search", summary="Search results, as a list")
async def search(
    request: Request,
    jukebox: Juke,
    renderer: Render,
    q: Annotated[str, Query(max_length=views.MAX_QUERY_LENGTH)],
) -> Response:
    if not q.strip():
        return renderer.response(
            "panels/search.html",
            {
                "request": request,
                "query": q,
                "results": None,
                "songs": [],
                "albums": [],
                "artists": [],
            },
        )
    context = await read(jukebox.appliance, partial(views.results_context, jukebox, q))
    return renderer.response("panels/search.html", {"request": request, **context})


@router.get("/artists/{artist_id}", summary="One artist's albums and tracks")
async def artist(
    request: Request, jukebox: Juke, renderer: Render, artist_id: Annotated[int, Path(ge=1)]
) -> Response:
    context = await read(
        jukebox.appliance, partial(views.artist_context, jukebox, ArtistId(artist_id))
    )
    if context["artist"] is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "that artist is not in the library")
    return renderer.response("panels/artist.html", {"request": request, **context})


@router.get("/albums/{album_id}", summary="One album and its tracks")
async def album(
    request: Request, jukebox: Juke, renderer: Render, album_id: Annotated[int, Path(ge=1)]
) -> Response:
    context = await read(
        jukebox.appliance, partial(views.album_context, jukebox, AlbumId(album_id))
    )
    if context["album"] is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "that album is not in the library")
    return renderer.response("panels/album.html", {"request": request, **context})


async def _render(
    request: Request,
    jukebox: Jukebox,
    renderer: Renderer,
    template: str,
    **extra: object,
) -> Response:
    view = await read(jukebox.appliance, partial(views.player, jukebox))
    return renderer.response(
        template,
        {
            "request": request,
            **view.context,
            "health": jukebox.health.snapshot,
            "domain": jukebox.config.server.domain,
            **extra,
        },
    )
