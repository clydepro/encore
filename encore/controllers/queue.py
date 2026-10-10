"""`POST /queue` — the guest's one action (SAPRS 9.7, 9.9, 10.4).

This is where the whole appliance is felt most: a tap, and forty phones change. Three
things about it are worth naming where they happen.

**The answer is a fragment, not a redirect.** An HTMX request gets back the panel it
asked about plus out-of-band swaps for the panels it also changed, so one round trip moves
the requester's screen and everyone else's. A browser without HTMX — a text browser, a
`curl`, a phone with JavaScript switched off — gets a 303 back to the page it came from,
which is the same answer with a round trip added. SAPRS 13.2's "no single point of
failure" includes the front end.

**Other clients hear about it over SSE, not from this response.** `SongQueued` is
published by `QueueService` inside the append, before this handler has a fragment to
return, so a guest who queued the song is redrawn by the same event as everyone else and
then handed their own confirmation on top. That ordering is why ADR-011 could insist the
queue is the only mutator: there is exactly one moment at which "who is waiting for what"
changes, and it is not in this file.

**A refusal is rendered, not flashed.** SAPRS 10.8 forbids an error that disappears on the
next action, and the queue-full message is the one a guest will actually see at 11 p.m., so
it is a panel rather than a toast: the reason, the count, and what to do, held on screen
until something else is true.
"""

from __future__ import annotations

from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import RedirectResponse

from encore.api import views
from encore.api.deps import Jukebox, jukebox_of, read, renderer_of
from encore.api.errors import INVALID_REQUEST, respond
from encore.api.html import FRAGMENT_HEADERS, Renderer
from encore.api.rows import SongRow
from encore.domain import QueueItem, QueueItemStatus, SongId

__all__ = ["router"]

router = APIRouter(tags=["queue"])

Juke = Annotated[Jukebox, Depends(jukebox_of)]
Render = Annotated[Renderer, Depends(renderer_of)]


@router.post("/queue", summary="Ask for a song (non-idempotent)")
async def enqueue(  # noqa: PLR0917 - a route's arguments are declared by FastAPI
    request: Request,
    jukebox: Juke,
    renderer: Render,
    song_id: Annotated[int | None, Query] = None,
    htmx: Annotated[str | None, Header(alias="HX-Request")] = None,
    referer: Annotated[str | None, Header(alias="Referer")] = None,
) -> Response:
    """Queue one song and say what it became.

    Accepts `?song_id=`, a form field, or a JSON body, because the guests who arrive at
    this URL are a browser, a `curl` in a script, and a phone on a bad connection, and the
    appliance has no interest in which of the three it is serving.
    """

    wanted = await _song_id(request, song_id)
    if wanted is None:
        return respond(
            request,
            INVALID_REQUEST,
            {"detail": "Send ?song_id=, a song_id form field, or a JSON body with song_id."},
        )

    item, row, view = await read(jukebox.appliance, partial(_enqueue, jukebox, SongId(wanted)))
    if not htmx and not _asked_for_panels(request):
        # A plain browser posted the form. Send it back where it came from; the shell it
        # reloads already shows the new queue, because the shell renders the state.
        return RedirectResponse(referer or "/", status_code=303)
    return _confirmation(renderer, request, jukebox, item=item, row=row, view=view)


async def _song_id(request: Request, query_value: int | None) -> int | None:
    """The song this request names, from whichever of the three shapes it used."""

    if query_value is not None:
        return query_value
    if request.method == "POST" and request.headers.get("content-type", "").startswith(
        "application/json"
    ):
        try:
            body = await request.json()
        except ValueError:
            return None
        value = body.get("song_id") if isinstance(body, dict) else None
        return None if value is None else int(value)
    if request.method == "POST" and request.headers.get("content-type", "").startswith(
        "application/x-www-form-urlencoded"
    ):
        fields = await request.form()
        value = fields.get("song_id")
        try:
            return None if value is None else int(str(value))
        except ValueError:
            return None
    return None


def _asked_for_panels(request: Request) -> bool:
    """Whether this caller asked, by name, for the panels as well as the confirmation.

    The `?panels=1` the live script appends. A plain browser that posted the form without
    JavaScript is not asking for fragments and should not be handed markup to render, so it
    gets the redirect instead (SAPRS 13.2's "no single point of failure", at the front end).
    """

    return request.query_params.get("panels") == "1"


def _confirmation(
    renderer: Renderer,
    request: Request,
    jukebox: Jukebox,
    *,
    item: QueueItem,
    row: SongRow | None,
    view: views.PlayerView,
) -> Response:
    """The requester's fragment, with the panels it also changed riding along.

    Three renders of four small templates, on the loop rather than the appliance thread:
    the state was read once, on the thread, and a template cannot reach a service (the
    guardrail that keeps SAPRS 4.8's domain purity true at the last mile).

    The *sentence* comes from the item rather than from the room, which is the one place
    SAPRS 9.7's two cases can be told apart. A second press while another song plays is
    "Added at 4" even though the appliance is playing, and `view.queue_state` — which
    describes what the speakers are doing — would say "Playing now" about a song that has not
    started. The queue knows which item became the head (ADR-011); this only asks it.
    """

    context = {
        "request": request,
        "item_id": int(item.id),
        "row": row,
        "domain": jukebox.config.server.domain,
        "health": jukebox.health.snapshot,
        **view.context,
        "queue_state": "playing" if item.status is QueueItemStatus.PLAYING else "queued",
        "state": item.status,
    }
    body = (
        renderer.render("partials/added.html", context)
        + renderer.render("panels/player.html", {**context, "oob": True})
        + renderer.render("panels/alerts.html", {**context, "oob": True})
    )
    return Response(body, headers=FRAGMENT_HEADERS)


def _enqueue(
    jukebox: Jukebox, song_id: SongId
) -> tuple[QueueItem, SongRow | None, views.PlayerView]:
    """Queue, then read the row back — on the appliance thread, in one visit (ADR-012).

    The read is not racy with the append for the reason `_enqueue`'s sibling in `api/v1.py`
    gives: both halves run on one thread, so the position and the count handed back belong
    to the queue this request just changed.
    """

    item = jukebox.queue.enqueue(song_id)
    view = views.player(jukebox)
    return item, views.row_for_item(view, int(item.id)), view
