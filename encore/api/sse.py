"""`GET /events` — the stream a party's phones are held open on (SAPRS 9.10, 10.5).

Thin on purpose. The policy — which facts, whose queue, what happens when a client falls
behind — is `SSEPublisher`'s, and this module owns only the HTTP half: a response that
streams, headers that stop a proxy buffering it, and a connection that cannot leak.

Three details are worth the comments in place, because none is visible from the rest of
the tree:

* The `snapshot` frame is sent **before** this client's queue is registered. A connect
  that subscribed first could be handed a fact and then a snapshot taken after it, and the
  browser would render the older of the two.
* Nothing is sent for health at connect. The shell the browser already has was rendered
  with the aggregate in it, and a frame that repeated it would be a second source for a
  sentence the page has already said (SAPRS 11.7's step-8 state).
* The generator's `finally` is the only cleanup. A disconnect arrives as a cancellation,
  not as an exception a route can see, and a stream left registered would be a queue with
  no reader for the rest of the night.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends
from sse_starlette import EventSourceResponse

from encore.api import views
from encore.api.deps import Jukebox, jukebox_of, read
from encore.services.sse_publisher import Frame

__all__ = ["router"]

router = APIRouter(tags=["events"])

#: Seconds between transport pings. Under the idle timeout of any proxy an appliance is
#: likely to sit behind, and high enough that forty phones is not forty polls.
PING_SECONDS = 15

#: The frame this endpoint emits by itself. The facts come from `SSEPublisher` and the
#: panels from `encore/api/fragments.py`; this one is the transport's own.
SNAPSHOT = "snapshot"

_HEADERS = {
    # SAPRS 10.10: mutable state must not be cached, and an `EventSource` is mutable state
    # with a longer lifetime than any response.
    "Cache-Control": "no-store",
    "X-Accel-Buffering": "no",
}


@router.get("/events", summary="Live state, as Server-Sent Events")
async def events(jukebox: Annotated[Jukebox, Depends(jukebox_of)]) -> EventSourceResponse:
    """One connection, many frames: the facts, the panels they invalidate, the pings.

    `Last-Event-ID` is accepted and ignored. A reconnecting browser is handed a `snapshot`
    and redraws, which cannot drift; replay from a ring buffer would mean keeping every
    frame for every possible absence, and that memory is bounded by how long a phone
    spends in a lift.
    """

    view = await read(jukebox.appliance, partial(views.player, jukebox))
    stream = jukebox.live.connect(loop=asyncio.get_running_loop())

    async def frames() -> AsyncIterator[dict[str, str]]:
        yield _frame(SNAPSHOT, view.as_json())
        try:
            while True:
                frame = await stream.next()
                if frame is None:
                    return
                yield frame.as_sse()
        finally:
            # The only cleanup, and it runs on a cancellation rather than on a return:
            # `sse-starlette` owns disconnect detection (it listens for `http.disconnect`
            # in a task of its own), so a poll here would compete for that one message and
            # could eat the very event the generator needs to end.
            jukebox.live.disconnect(stream)

    return EventSourceResponse(frames(), ping=PING_SECONDS, headers=_HEADERS)


def _frame(event: str, data: object) -> dict[str, str]:
    return Frame(event=event, data=json.dumps(data, separators=(",", ":"), default=str)).as_sse()
