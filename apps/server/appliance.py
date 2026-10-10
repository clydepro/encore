"""The composition root: one appliance, built in SAPRS 11.7's order.

Everything Encore is gets wired here and nowhere else. The reason is not tidiness: the
services take each other as constructor arguments (AIG 17), so a graph has a build order,
and a build order has a place. `encore/api/` would rather be handed a finished appliance;
`encore/services/` would rather not know it exists. This module is where those two
preferences are paid for.

Three things are decided here that no lower module can decide:

* **One thread owns the runtime** (ADR-012). Every service is constructed on the calling
  thread and then touched only by the appliance worker — including the stores, whose
  SQLAlchemy sessions are not portable across threads by accident but by design.
* **Health is polled, not watched.** The tick asks each component once a second
  (SAPRS 7.5) rather than waiting for a component to volunteer. A component that is stuck
  is exactly the one that will not say so, and `HealthService` publishes only when the
  aggregate actually moves, so a quiet hour produces no events.
* **Failure at startup is loud and immediate.** A missing `library.db` raises here, before
  a socket is bound, because an appliance that answers `200 OK` while playing nothing is
  the worse design (SAPRS 12.1).

`build()` takes an optional `launcher` and `clock` so a test can compose the identical
graph with a fake engine — the seam that makes `tests/integration/test_server_app.py`
exercise the real wiring rather than a sketch of it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import cast

from encore.config.models import EncoreConfig
from encore.domain.health import ComponentHealth, HealthStatus
from encore.events.bus import EventBus, Subscription
from encore.playback import (
    CommandChannel,
    EngineLauncher,
    MpvLauncher,
    MpvPlayer,
    PlaybackService,
    PlaybackSupervisor,
    TransitionPolicy,
)
from encore.repositories.library import LibraryStore, open_library
from encore.repositories.runtime import RuntimeStore, open_runtime_store
from encore.search import SearchService
from encore.services import HealthService, LibraryService, QueueService, SSEPublisher
from encore.services.health_service import HealthSource
from encore.utilities.appliance import ApplianceThread, call_async
from encore.utilities.clock import Clock, SystemClock

__all__ = ["SERVER_VERSION", "Appliance", "Probe", "build"]

SERVER_VERSION = "1.0.0"

#: The gap between one look at the engine and the next. SAPRS 7.5's "roughly once per
#: second", and `playback.progress_interval_seconds` is the configured answer.
DEFAULT_TICK_SECONDS = 1.0


class Probe:
    """A health source that is a function rather than a class.

    `HealthService` takes anything with a `.health()`; four tiny adapter classes saying so
    would be four files. A probe is named at the call site, which is where the reader
    already is.
    """

    def __init__(self, name: str, probe: Callable[[], ComponentHealth]) -> None:
        self._name = name
        self._probe = probe

    def health(self) -> ComponentHealth:
        report = self._probe()
        return ComponentHealth(component=self._name, status=report.status, detail=report.detail)


@dataclass(slots=True, kw_only=True)
class Appliance:
    """The running jukebox, as the HTTP layer needs it (see `encore.api.deps.Jukebox`).

    Attributes:
        config: Validated configuration.
        events: The bus. The HTTP half subscribes; only services publish.
        appliance: The one thread allowed to touch everything below this line (ADR-012).
        library: Immutable catalogue reads.
        search: The query side, with its own service because it has its own failure mode.
        queue: FIFO, and the only write path a guest has.
        playback: The engine's last known state.
        supervisor: mpv's process and its recovery, kept visible for shutdown and tests.
        health: The aggregate, polled by the tick.
        live: The SSE fan-out.
        stores: Both databases, for `close()` and for a test that wants to look at rows.
    """

    config: EncoreConfig
    events: EventBus
    appliance: ApplianceThread
    library: LibraryService
    search: SearchService
    queue: QueueService
    playback: PlaybackService
    supervisor: PlaybackSupervisor
    health: HealthService
    live: SSEPublisher
    logger: logging.Logger
    clock: Clock
    library_store: LibraryStore = field(repr=False)
    runtime_store: RuntimeStore = field(repr=False)
    subscriptions: list[Subscription] = field(default_factory=list, repr=False)
    version: str = SERVER_VERSION

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Begin the appliance: the worker, then everything it owns, on that worker.

        The order is deliberate twice over. `ApplianceThread.start()` first, because
        `become_ready` has to run on it; and inside `become_ready`, the queue subscribes
        before mpv launches, so the first `SongStarted` of a restored queue has a listener
        already registered (ADR-004's argument for subscribing at startup rather than at
        first use). Launching mpv last means a failed launch is reported to a graph that is
        otherwise listening, and the appliance comes up degraded rather than crashing on a
        guest's first request.
        """

        self.appliance.start()
        self.appliance.run(self.become_ready)

    def become_ready(self) -> None:
        """The service half of startup, on the appliance thread (ADR-012).

        `QueueService.start()` also re-adopts whatever `runtime.db` says was still waiting,
        which is a read of the queue and a command to the player: exactly the work that may
        not happen on the event loop.
        """

        self.appliance.assert_current("startup")
        # Before the queue's, so the first fact `start()` may publish already has a listener
        # on the way out to the room (SAPRS 8.6's restart is the one moment the appliance
        # produces events before a guest asks for anything).
        self.live.start()
        self.queue.start()
        self.supervisor.start()

    def stop(self) -> None:
        """Stop in the order that cannot deadlock: worker first, then what it touches.

        The worker is joined before the queue and the engine are closed because both
        `close()` and `stop()` touch mpv, and a task still running on the thread would be
        issuing commands to a process being torn down alongside it. Everything queued but
        unstarted is cancelled rather than run: nobody is waiting for it any more, and a
        request that arrived after the last guest left should not be the reason shutdown
        takes another three seconds (SAPRS 8.7's step 2 is about the song that *started*).
        """

        self.appliance.stop()
        self.queue.close()
        self.supervisor.stop()

    # -- the tick --------------------------------------------------------

    def tick_once(self) -> None:
        """One second of a party, on the appliance thread.

        Three looks, in the order a fact needs them: the supervisor notices a dead process,
        the service reports progress and end-of-file (which is what lets the queue advance
        inline, ADR-011), and health aggregates the result. Reverse the last two and a
        process that died this second would be reported healthy by the tick that noticed it.
        """

        self.appliance.assert_current("the tick")
        self.supervisor.tick()
        self.playback.tick()
        self.health.check()
        # Last, and only here: a second of progress is a fact the queue does not care about
        # and the screen does, so it goes no further than the fan-out (SAPRS 9.10).
        self.live.beat(self.playback.progress)

    async def run_ticks(self, *, stop: asyncio.Event) -> None:
        """Keep the appliance breathing until asked to stop.

        A coroutine on the event loop that does no work of its own: it sleeps, submits, and
        awaits (ADR-012). The interval is the configured one so a machine that needs slower
        progress reports gets them, and an exception from the tick is logged rather than
        allowed to end the loop — a party with no ticks is a silent room.
        """

        every = timedelta(seconds=self.config.playback.progress_interval_seconds)
        seconds = every.total_seconds() or DEFAULT_TICK_SECONDS
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=seconds)
                return
            except TimeoutError:
                pass
            try:
                await call_async(self.appliance, self.tick_once)
            except Exception:
                self.logger.exception("appliance tick failed")

    def close(self) -> None:
        """Release both databases. Idempotent, because `finally` runs twice in some tests."""

        self.library_store.close()
        self.runtime_store.close()


def build(
    config: EncoreConfig,
    *,
    launcher: EngineLauncher | None = None,
    clock: Clock | None = None,
    library_db: Path | None = None,
    runtime_db: Path | None = None,
    launch_engine: bool = True,
    logger: logging.Logger | None = None,
) -> Appliance:
    """Open the databases, construct the graph, hand back a stopped appliance.

    Args:
        config: Validated configuration — the paths, the queue ceiling, the crossfade.
        launcher: Something that produces an mpv channel. Production builds `MpvLauncher`;
            a test passes a fake, which is the whole of how the HTTP integration tests run
            without a sound card.
        clock: Time, for the queue's estimates and health's timestamps.
        library_db: Override for `paths.library_db`, for a test over two real files.
        runtime_db: Override for `paths.runtime_db`.
        launch_engine: False to leave mpv alone — a composition that must not spawn a
            process on a machine without one.
        logger: Where construction is reported.

    Raises:
        StoreNotFoundError: No built library. Publish one with `encore-builder`.
        Anything a corrupt or future-dated database raises (SAPRS 12.1).
    """

    log = logger or logging.getLogger("encore.server")
    time = clock or SystemClock()
    paths = config.paths

    library = open_library(library_db or paths.library_db)
    runtime = open_runtime_store(runtime_db or paths.runtime_db)
    events = EventBus(logger=logging.getLogger("encore.events"))

    catalogue = LibraryService(
        library, artwork_dir=paths.artwork_dir, logger=logging.getLogger("encore.library")
    )
    search = SearchService(
        index=library.search,
        catalogue=library,
        page_size=config.search.page_size,
    )
    engine = launcher or MpvLauncher(
        playback=config.playback, audio=config.audio, temp_dir=paths.temp_dir
    )
    supervisor = PlaybackSupervisor(
        launcher=engine,
        events=events,
        config=config.playback,
        clock=time,
        logger=logging.getLogger("encore.playback.supervisor"),
    )
    playback = PlaybackService(
        player=MpvPlayer(
            _channel_of(supervisor) if launch_engine else _silent_channel(),
            volume=float(config.audio.volume),
        ),
        events=events,
        clock=time,
        recovery=supervisor,
        policy=TransitionPolicy.from_config(config.audio),
        logger=logging.getLogger("encore.playback"),
    )
    # ADR-011's mutual reference, made here because it is a composition rather than a
    # behaviour: the service restarts through the supervisor, the supervisor reports the
    # process to the service, and neither can be built first without the other.
    supervisor.bind(playback)
    queue = QueueService(
        store=runtime,
        library=catalogue,
        player=playback,
        events=events,
        config=config.queue,
        clock=time,
        logger=logging.getLogger("encore.queue"),
    )
    health = HealthService(
        sources=_sources(library=library, runtime=runtime, supervisor=supervisor),
        events=events,
        clock=time,
        logger=logging.getLogger("encore.health"),
    )
    live = SSEPublisher(events=events, logger=logging.getLogger("encore.sse"))

    log.info(
        "appliance composed",
        extra={
            "songs": catalogue.counts().get("songs", 0),
            "search_available": library.fts_available(),
            "queue_max_items": config.queue.max_items,
        },
    )
    return Appliance(
        config=config,
        events=events,
        appliance=ApplianceThread(logger=logging.getLogger("encore.appliance")),
        library=catalogue,
        search=search,
        queue=queue,
        playback=playback,
        supervisor=supervisor,
        health=health,
        live=live,
        logger=log,
        clock=time,
        library_store=library,
        runtime_store=runtime,
    )


def _sources(**parts: object) -> list[tuple[str, HealthSource]]:
    """The components SAPRS 11.7 names, as health sources.

    Each answer is a property read rather than a command: a poll that issued an mpv
    `get_property` would make the readiness path depend on the very engine it is asking
    about, and an engine that is not there is the case that matters. `RuntimeStore.current`
    is the one exception, and it is a `SELECT` of a metadata row — the cost is a query the
    tick would pay anyway to find out that the disk had been pulled.
    """

    # `**parts` keeps the call site readable and types every value as `object`; the keys are
    # set by `build` a few lines above, so these three reads are the whole of the trust.
    library = cast("LibraryStore", parts["library"])
    runtime = cast("RuntimeStore", parts["runtime"])
    supervisor = cast("PlaybackSupervisor", parts["supervisor"])
    return [
        ("playback", supervisor),
        (
            "library",
            Probe(
                "library",
                lambda: ComponentHealth(
                    component="library",
                    # `info`, not `counts()`: the Builder's tally is one row of
                    # `library_meta`, while `counts()` re-derives it with three COUNT(*)
                    # scans — a fair price once per request and not once per second.
                    status=(
                        HealthStatus.HEALTHY
                        if library.info.usable and library.info.song_count
                        else HealthStatus.UNAVAILABLE
                    ),
                    detail="" if library.info.usable else "the library database is unreadable",
                ),
            ),
        ),
        (
            "runtime",
            Probe(
                "runtime",
                lambda: ComponentHealth(
                    component="runtime",
                    # `info.current` is the schema verdict plus a read of both tables, which
                    # is what makes it a health probe rather than a config dump: a WAL file
                    # the disk filled up under shows here, and nowhere else (SAPRS 11.7).
                    status=(
                        HealthStatus.HEALTHY if runtime.info.current else HealthStatus.DEGRADED
                    ),
                    detail="" if runtime.info.current else "runtime.db is behind this build",
                ),
            ),
        ),
        (
            "search",
            Probe(
                "search",
                lambda: ComponentHealth(
                    component="search",
                    # The store's verdict, not the service's: an FTS5 table that failed to
                    # open is the only way search is unavailable, and `SearchService` is
                    # deliberately unable to answer for the file it was handed (SAPRS 11.2).
                    status=(
                        HealthStatus.HEALTHY if library.fts_available() else HealthStatus.DEGRADED
                    ),
                    detail=(
                        ""
                        if library.fts_available()
                        else "the library has no search index; browsing still works"
                    ),
                ),
            ),
        ),
    ]


def _channel_of(supervisor: PlaybackSupervisor) -> Callable[[], CommandChannel]:
    """The player's view of the engine: a channel, looked up every time.

    `MpvPlayer` takes a provider rather than a socket for exactly the reason
    `tests/integration/test_queue_playback_and_search.py` demonstrates — a restart hands
    back a new channel, and a player holding the old one would play the rest of the night
    into a dead file descriptor.
    """

    return supervisor.channel


def _silent_channel() -> Callable[[], CommandChannel]:
    """A channel that refuses, for a composition with no engine (a docs build, a test)."""

    def refuse() -> CommandChannel:  # pragma: no cover - an engine-less appliance
        raise RuntimeError("this appliance has no playback engine")

    return refuse
