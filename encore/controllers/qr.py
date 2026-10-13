"""The QR code a party reads off a wall (SAPRS 9.11, 1.3, AIG step 15).

Two routes, because there are two moments: an operator opening the page to print it, and a
browser — or a label tool, or a wiki — wanting the image on its own.

Nothing here decides what the code says. `encore.utilities.qr.target` and the configuration do;
this module's job is the response and its headers.

**Public.** The address of the jukebox is on the footer of every guest page and in
`/api/v1/info`, so printing it is not a disclosure and putting it behind the session cookie
would only make it harder for the person who has to stick it by the door. What is *not* here is
any way to make the appliance say something other than what its own configuration says: there is
no `?url=` parameter, and a QR endpoint that encoded a caller-supplied string would be an
appliance that prints phishing codes about itself.

**Cached, unlike everything else.** SAPRS 10.10's rule is that mutable state is not cached and an
artwork file is, and a QR code is neither — it is immutable until an operator edits
`server.domain` and restarts, which is the same lifetime as the static assets. `no-store` here
would mean every phone in the room re-requesting an SVG while somebody's printing a poster.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from encore.api.deps import Jukebox, jukebox_of, renderer_of
from encore.api.html import Renderer
from encore.utilities import qr

__all__ = ["router"]

router = APIRouter(tags=["guest"])

Juke = Annotated[Jukebox, Depends(jukebox_of)]
Render = Annotated[Renderer, Depends(renderer_of)]

#: A day, which is the interval over which "the configuration changed" is worth a re-render,
#: and the point at which a stale poster stops being the appliance's fault.
_IMAGE_HEADERS: Final = {"Cache-Control": "public, max-age=86400"}


@router.get("/qr", summary="Printable QR code for the guest address", response_class=HTMLResponse)
async def qr_page(request: Request, jukebox: Juke, renderer: Render) -> Response:
    """The page an operator opens, prints, and tapes next to the speakers."""

    server = jukebox.config.server
    context: dict[str, Any] = {
        "request": request,
        "url": qr.target(server.domain, server.port),
        "domain": server.domain,
        "hostname": server.hostname,
        "port": server.port,
        "version": jukebox.version,
    }
    return renderer.response("pages/qr.html", context, headers=_IMAGE_HEADERS)


@router.get(
    "/qr.svg",
    summary="The QR code itself, as SVG",
    response_class=Response,
    responses={200: {"content": {"image/svg+xml": {}}}},
)
async def qr_image(jukebox: Juke) -> Response:
    """The image alone, for anything that embeds rather than opens.

    Rendered server-side from the appliance's own configuration, so the code on the wall and the
    address in `/api/v1/info` cannot disagree — which is the entire failure this route exists to
    make impossible.
    """

    server = jukebox.config.server
    payload = qr.target(server.domain, server.port)
    return Response(
        content=qr.svg(payload, title="Encore", description=payload),
        media_type=qr.MEDIA_TYPE,
        headers=_IMAGE_HEADERS,
    )
