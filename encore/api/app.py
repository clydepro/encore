"""The FastAPI application: routers, templates, static files, lifespan (SAPRS 10, 13.1).

The composition lives in `apps/server/` — this function receives a finished `Jukebox` and
turns it into something that answers HTTP. That split is what makes the whole interface
testable without an appliance: a test builds the same graph with a fake engine and hands it
here (`tests/integration/test_htmx_sse.py`).

Four decisions are taken once, in this file:

* **The lifespan owns the appliance's life and death** (ADR-012). Starting and stopping it
  here rather than in `main()` means `TestClient(app)` runs the same thread, the same
  subscriptions and the same tick loop as the process systemd supervises — which is what
  makes the integration tests evidence rather than theatre, and what stops a server that
  was handed a composed graph from leaking a worker it never asked for.
* **Static files are mounted, not served from disk by a route.** HTMX, CSS and the live
  script come from `encore/static/`, and SAPRS 12.3's offline requirement means nothing
  here reaches a CDN. There is no fallback to a CDN, so a site with no uplink is not a
  site with an unusable UI.
* **The error handlers are installed before the routers**, so a route added later cannot
  accidentally answer a `QueueFullError` with a stack trace.
* **`/docs` and `/openapi.json` are open.** SAPRS 10.12's "no security by obscurity" is
  about the guest interface; the specification of a guest interface is a guest document.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from encore.api import fragments, sse, v1
from encore.api.deps import Jukebox
from encore.api.errors import register_error_handlers
from encore.api.html import STATIC_DIR, Renderer, build_renderer
from encore.controllers import browse, panels, queue

__all__ = ["create_app"]

VERSION = "1.0.0"

#: How long shutdown waits for the tick to notice. Longer than a command's timeout and
#: shorter than systemd's `TimeoutStopSec`, so the unit file is never the thing that kills
#: a settling queue (SAPRS 13.1's ordering, stated in `docs/Developer/Architecture-Layers.md`).
_SHUTDOWN_GRACE_SECONDS = 5.0

_DESCRIPTION = """\
Encore, a headless jukebox for a room full of phones.

Two interfaces to one state, as SAPRS 10.1 asks: these JSON endpoints, and the HTMX
service at `/` that a guest actually opens. Both read the same queue through the same
services, and `GET /events` keeps a browser's copy of that state current without asking
it to poll.

Queueing is **not idempotent**: two `POST`s of one song create two queue items (SAPRS
10.4). Everything a guest can do is here; playback control is administrative and lives
behind `/admin` in milestone 5.
"""


def create_app(
    jukebox: Jukebox, *, renderer: Renderer | None = None, version: str = VERSION
) -> FastAPI:
    """Build the app around a composed appliance."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        jukebox = app.state.jukebox
        renderer = app.state.renderer
        # Registered before the appliance starts, because starting it re-adopts whatever
        # `runtime.db` says was still waiting and may publish a `SongStarted` on the way
        # (SAPRS 8.6). A handler subscribed after that would be watching a screen that had
        # already changed — ADR-004's argument for subscribing at startup rather than at
        # first use, applied to the presentation tier.
        subscriptions = fragments.subscribe(jukebox, renderer)
        jukebox.start()
        stop = asyncio.Event()
        ticking = asyncio.create_task(jukebox.run_ticks(stop=stop))
        app.state.log.info("appliance ready", extra={"port": jukebox.config.server.port})
        try:
            yield
        finally:
            stop.set()
            # A tick that is mid-command gets the shutdown's grace period rather than an
            # immediate cancel: `QueueService` is allowed to settle the song it started
            # (SAPRS 8.7's step 2), and an orphaned `PLAYING` row outlives the process.
            done, pending = await asyncio.wait({ticking}, timeout=_SHUTDOWN_GRACE_SECONDS)
            for task in pending:
                # A tick wedged on an mpv command that will never answer. Cancelling it is
                # safe: the appliance thread is where the work runs, and it finishes the
                # command's timeout on its own — this only stops shutdown waiting forever.
                task.cancel()
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    app.state.log.error("tick loop ended", exc_info=task.exception())
            for subscription in subscriptions:
                subscription.unsubscribe()
            jukebox.stop()

    app = FastAPI(
        title="Encore",
        description=_DESCRIPTION,
        version=version,
        lifespan=lifespan,
        contact={"name": "Encore project", "url": "https://github.com/mattiacu/encore"},
        license_info={"name": "MIT", "url": "https://opensource.org/licenses/MIT"},
        openapi_tags=_TAGS,
        # `/docs` rather than a versioned path: the document describes the whole
        # application, and the one version it happens to describe is in its `info` block.
        # The admin screens will not want it public (SAPRS 10.12), which is milestone 5's
        # decision to make, not this one's.
        docs_url="/docs",
        redoc_url=None,
    )

    app.state.jukebox = jukebox
    app.state.config = jukebox.config
    app.state.log = logging.getLogger("encore.http")

    register_error_handlers(app, logger=app.state.log)
    app.include_router(browse.router)
    app.include_router(panels.router)
    app.include_router(queue.router)
    app.include_router(v1.router)
    app.include_router(sse.router)

    app.state.renderer = renderer or build_renderer()
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


_TAGS = [
    {
        "name": "guest",
        "description": "The pages a guest opens: `/` for Now Playing and Up Next, "
        "`/search`, `/artists`, `/albums`. No authentication, nothing to remember.",
    },
    {
        "name": "fragments",
        "description": "HTMX regions — the same views as the pages, without the shell "
        "(SAPRS 10.6). Every live panel has one of these URLs so a reconnecting browser "
        "can ask for what it can see.",
    },
    {
        "name": "queue",
        "description": "`POST /queue`, the one write a guest has. Non-idempotent by "
        "design (SAPRS 10.4).",
    },
    {"name": "v1", "description": "The versioned JSON interface (SAPRS 10.2)."},
    {"name": "events", "description": "The Server-Sent Events stream (SAPRS 9.10, 10.5)."},
]
