"""What the HTTP surface needs from the running appliance (SAPRS 11.5, AEP 9, ADR-012).

The web tier is handed a thing that is already wired, never a thing it wires itself:
`apps/server/` opens both stores and builds the services in SAPRS 11.7's order, and this
module says which of those the endpoints may touch. Declaring it as a protocol rather
than importing the composition root keeps the dependency pointing inward — `encore/api/`
may know `encore/services/`, and `apps/server/` may know both (SAPRS 15.2). The
assignment that proves the composition satisfies it is annotated in
`tests/integration/test_server_app.py`, which is where mypy can read it.

`read()` is the second thing this module owns, and it is the whole of ADR-012 at the
edge of the code: a handler says *what* it wants read, and the call runs on the one
thread allowed to reach mpv, the bus and `runtime.db`. A route that called
`jukebox.queue.enqueue()` directly would work in a unit test and stall every SSE client
in the room during a party, and nothing but this function and the guard inside it stands
between the two.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol, TypeVar

from fastapi import Request

from encore.api.html import Renderer
from encore.config.models import EncoreConfig
from encore.events.bus import EventBus
from encore.playback import PlaybackService
from encore.search import SearchService
from encore.services import HealthService, LibraryService, QueueService, SSEPublisher
from encore.utilities.appliance import ApplianceThread, call_async

__all__ = ["Jukebox", "call_async", "jukebox_of", "read", "renderer_of"]

T = TypeVar("T")


class Jukebox(Protocol):
    """The running appliance, as the HTTP layer sees it.

    Attributes:
        config: The validated configuration, for anything an operator may ask
            (`server.domain`, `queue.max_items`, whether search is even available).
        events: The bus. The HTTP layer subscribes to it (for fragment fan-out) and
            never publishes to it: an endpoint that published a fact would be a second
            publisher of something that already has one (ADR-004, ADR-011).
        appliance: The single thread that owns everything else (ADR-012).
        library: The immutable catalogue — browse, artwork, counts.
        search: The query side, which has its own service because it has its own
            failure mode (SAPRS 11.2).
        queue: The FIFO and the only write path a guest has.
        playback: The engine's last known state, for Now Playing.
        health: The aggregate, for `/api/v1/health` and the banner.
        live: The SSE fan-out, for `/events`.
        logger: The application's logger, so a failed render is reported in the
            appliance's journal rather than in a module-global one.
        version: The appliance's version, for `/api/v1/info` and the docs.
    """

    @property
    def config(self) -> EncoreConfig: ...

    @property
    def events(self) -> EventBus: ...

    @property
    def appliance(self) -> ApplianceThread: ...

    @property
    def library(self) -> LibraryService: ...

    @property
    def search(self) -> SearchService: ...

    @property
    def queue(self) -> QueueService: ...

    @property
    def playback(self) -> PlaybackService: ...

    @property
    def health(self) -> HealthService: ...

    @property
    def live(self) -> SSEPublisher: ...

    @property
    def logger(self) -> logging.Logger: ...

    @property
    def version(self) -> str: ...


def jukebox_of(request: Request) -> Jukebox:
    """The appliance this app is serving.

    One function so a route can say `Depends(jukebox_of)` and the dependency is visible
    in its signature, and so `app.state` remains a plain attribute bag rather than a
    service locator (AEP 9).
    """

    jukebox: Jukebox = request.app.state.jukebox
    return jukebox


def renderer_of(request: Request) -> Renderer:
    """The template environment this app renders with.

    A dependency rather than an import so a fragment handler and a unit test can be given
    different templates without either of them touching a module global.
    """

    renderer: Renderer = request.app.state.renderer
    return renderer


async def read[T](appliance: ApplianceThread, work: Callable[[], T]) -> T:
    """Run `work` on the appliance thread and await its result.

    Every handler in Encore reaches a service through this. Its body is one line, and
    that is the point: the boundary ADR-012 draws is narrow enough to be a function call,
    and wide enough that a request which plays a song cannot also be a request that
    stalls the stream.
    """

    return await call_async(appliance, work)
