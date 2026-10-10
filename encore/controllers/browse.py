"""The guest's pages: one shell, with a different middle (SAPRS 9.3-9.5).

Every route here answers the same question — "what is the appliance showing right now?" —
and each answer is the whole shell with its main region replaced. That is the shape
ADR-002 commits to: a page load is a template render, so a phone three rooms from the
access point gets something usable rather than a bundle to parse. Nothing waits on mpv or
a queue read that the screen does not need.

The live regions inside the shell are marked with `id`s that `GET /events` knows, which is
how "update without a full page reload" (SAPRS 9.8) is satisfied by a document that is
fully rendered the first time.

The one route that is not a page is `/artwork/{kind}/{id}`. It lives beside them because
it is the same read of the same immutable database, and because SAPRS 10.10's cache rule —
artwork cacheable, queue state not — is easiest to keep true when the two responses are
next to each other in one file.
"""

from __future__ import annotations

from functools import partial
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import FileResponse

from encore.api import views
from encore.api.deps import Jukebox, jukebox_of, read, renderer_of
from encore.api.html import (
    NO_COVER,
    NO_COVER_MEDIA_TYPE,
    PLACEHOLDER_HEADERS,
    Renderer,
    artwork_headers,
)
from encore.domain import AlbumId, ArtistId, SongId
from encore.services.library_service import ArtworkFile

__all__ = ["router"]

router = APIRouter(tags=["guest"])

Juke = Annotated[Jukebox, Depends(jukebox_of)]
Render = Annotated[Renderer, Depends(renderer_of)]

#: A shell embeds the current queue and player, so it is as mutable as a fragment.
_SHELL = {"Cache-Control": "no-store"}


@router.get("/", summary="Now Playing and Up Next")
async def home(request: Request, jukebox: Juke, renderer: Render) -> Response:
    return await _shell(request, jukebox, renderer, "pages/home.html")


@router.get("/search", summary="Search the library")
async def search_page(
    request: Request,
    jukebox: Juke,
    renderer: Render,
    q: Annotated[str, Query(max_length=views.MAX_QUERY_LENGTH)] = "",
) -> Response:
    context: dict[str, Any] = (
        await read(jukebox.appliance, partial(views.results_context, jukebox, q))
        if q.strip()
        else {"query": q, "results": None, "songs": [], "albums": [], "artists": []}
    )
    return await _shell(request, jukebox, renderer, "pages/search.html", **context)


@router.get("/artists", summary="Browse artists")
async def artists_page(
    request: Request,
    jukebox: Juke,
    renderer: Render,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    page = await read(
        jukebox.appliance,
        partial(jukebox.library.artists, offset=offset, limit=views.BROWSE_LIMIT),
    )
    return await _shell(request, jukebox, renderer, "pages/artists.html", listing=page)


@router.get("/artists/{artist_id}", summary="One artist's albums and tracks")
async def artist_page(
    request: Request,
    jukebox: Juke,
    renderer: Render,
    artist_id: Annotated[int, Path(ge=1)],
) -> Response:
    context = await read(
        jukebox.appliance, partial(views.artist_context, jukebox, ArtistId(artist_id))
    )
    if context["artist"] is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "that artist is not in the library")
    return await _shell(request, jukebox, renderer, "pages/artist.html", **context)


@router.get("/albums/{album_id}", summary="One album and its tracks")
async def album_page(
    request: Request,
    jukebox: Juke,
    renderer: Render,
    album_id: Annotated[int, Path(ge=1)],
) -> Response:
    context = await read(
        jukebox.appliance, partial(views.album_context, jukebox, AlbumId(album_id))
    )
    if context["album"] is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "that album is not in the library")
    return await _shell(request, jukebox, renderer, "pages/album.html", **context)


@router.get("/queue", summary="The whole waiting list")
async def queue_page(request: Request, jukebox: Juke, renderer: Render) -> Response:
    """Every queued request, not the twenty the panel shows (SAPRS 9.9).

    A separate URL rather than a "show more" on the panel because the panel is redrawn by
    facts and this page is asked for once, by a guest who wanted to know whether their song
    was in it at all. A "show more" would have to be re-fetched on every redraw.
    """

    context = await read(jukebox.appliance, partial(views.queue_page, jukebox))
    return await _shell(request, jukebox, renderer, "pages/queue.html", **context)


@router.get("/artwork/{kind}/{identifier}", summary="Album, artist or song artwork")
async def artwork(
    kind: Annotated[str, Path(pattern="^(album|artist|song)$")],
    identifier: Annotated[int, Path(ge=1)],
    jukebox: Juke,
) -> FileResponse:
    """Serve one cached image, with the caching a content-addressed file deserves.

    The artwork cache is written by the Builder and never touched at runtime (ADR-006), so
    `immutable` describes it rather than aspiring to.

    A miss comes in two kinds, and they are answered differently. *Nothing is depicted* — no
    reference anywhere in the song, album or artist chain — gets the placeholder, because
    SAPRS 6.7 is explicit that a missing picture is a missing picture and never an error, and
    a row's `<img>` has to resolve to something on a library built from files with no tags.
    *Something is depicted and the file is gone* gets a 404, because that is a cache deleted
    from under a database that still names it, and a placeholder that answered that case too
    would be a fault invisible to everyone.
    """

    resolve = {
        "album": partial(_album_artwork, jukebox, identifier),
        "artist": partial(_artist_artwork, jukebox, identifier),
        "song": partial(_song_artwork, jukebox, identifier),
    }[kind]
    found: ArtworkFile | None = await read(jukebox.appliance, resolve)
    if found is None:
        return FileResponse(NO_COVER, media_type=NO_COVER_MEDIA_TYPE, headers=PLACEHOLDER_HEADERS)
    if not found.exists:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "the library names artwork that the cache no longer holds",
        )
    return FileResponse(found.path, headers=artwork_headers(found.etag))


async def _shell(
    request: Request,
    jukebox: Jukebox,
    renderer: Render,
    template: str,
    **context: Any,
) -> Response:
    """The page every route returns: current player, current health, one region filled."""

    view = await read(jukebox.appliance, partial(views.player, jukebox))
    return renderer.response(
        template,
        {
            "request": request,
            **view.context,
            "view": view,
            "health": jukebox.health.snapshot,
            "domain": jukebox.config.server.domain,
            "version": jukebox.version,
            **context,
        },
        headers=_SHELL,
    )


def _album_artwork(jukebox: Jukebox, identifier: int) -> ArtworkFile | None:
    return jukebox.library.artwork_for_album(AlbumId(identifier))


def _artist_artwork(jukebox: Jukebox, identifier: int) -> ArtworkFile | None:
    return jukebox.library.artwork_for_artist(ArtistId(identifier))


def _song_artwork(jukebox: Jukebox, identifier: int) -> ArtworkFile | None:
    return jukebox.library.artwork_for_song(SongId(identifier))
