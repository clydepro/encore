"""The Playback Supervisor: mpv as a process that must survive a party (SAPRS 7.2, 7.6).

Seven responsibilities, in the order SAPRS 7.6 lists them: launch, establish IPC, monitor,
detect, record diagnostics, publish health and recovery, restart and reconnect. Nothing
else in Encore starts a process, and the reason that matters is that this is the only place
the appliance is allowed to know anything about the hardware — every audio-specific word it
uses came from configuration (SAPRS 7.7).

Three decisions worth reading before changing anything:

**No thread.** `tick()` is the monitor, called beside `PlaybackService.tick()`. A watchdog
thread would need ownership of the loop that reads the socket, and ADR-004 rejected exactly
that leakage. The cost is that a crash is noticed up to one second late, which is the
interval SAPRS 7.5 already accepts for progress. The benefit is that every test here is
deterministic and nothing in `encore/playback/` can deadlock.

**Restarts are scheduled, not slept.** Backoff is a deadline in the future, compared
against the injected clock — not `time.sleep()`. Blocking here would freeze the HTTP server
and every SSE client for up to thirty seconds (SAPRS 11.4: long-running work must not
delay the critical path), and a sleeping thread cannot be tested without depending on
wall-clock time, which `docs/Developer/Testing.md` forbids.

**The channel is provided, not stored by callers.** `channel()` is what `MpvPlayer` holds,
so after a restart the next read goes to the new mpv without the player, the service or any
subscriber being rebuilt (SAPRS 7.6 step 5).

The queue is never restarted from here. `PlaybackRecovered` is the fact the queue subscribes
to, which is the ordering ADR-011 records: one component reports what happened to the
process, another decides what that means for what is playing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from encore.config.models import PlaybackConfig
from encore.domain import PlaybackState
from encore.domain.health import ComponentHealth, HealthStatus
from encore.events.bus import EventBus
from encore.events.health import HealthChanged
from encore.events.playback import PlaybackRecovered
from encore.playback.errors import EngineUnavailableError, MpvGoneError
from encore.playback.ipc import CommandChannel
from encore.utilities.clock import Clock, SystemClock

__all__ = [
    "COMPONENT",
    "EngineLauncher",
    "PlaybackNotifier",
    "PlaybackSupervisor",
    "RecoveryReport",
]

#: What the dashboard calls this component. Part of the admin surface, so it is a constant
#: rather than a constructor default a call site could change.
COMPONENT = "playback"

#: Consecutive failed restarts after which the component reports `unavailable` rather than
#: `degraded`. The ladder keeps climbing past this: the status line changes, the attempts
#: do not stop, because an appliance that gave up would need someone to notice.
UNAVAILABLE_AFTER_ATTEMPTS = 6

#: Restarts at which "recovered" starts being reported as a problem rather than a save. A
#: box that flaps is one the Administrator Guide tells the host to look at tonight, not at
#: the end of the party.
FLAPPING_RESTARTS = 5


class EngineLauncher(Protocol):
    """mpv as something that can be started, stopped, and asked whether it is alive.

    `encore.playback.ipc.MpvLauncher` is the implementation; tests pass a fake, which is
    how SAPRS 14.4's "unit tests must not require real mpv" is satisfied without the
    supervisor being a different class in tests.
    """

    def launch(self) -> CommandChannel: ...

    def terminate(self) -> None: ...

    @property
    def alive(self) -> bool: ...

    def diagnostics(self) -> str: ...


class PlaybackNotifier(Protocol):
    """What the supervisor needs from the playback service: one report and one read.

    Declared as two members rather than typed `PlaybackService` because that is the whole of
    the collaboration, and because a test can then hand in a recorder instead of reaching
    into a private attribute. The dependency is one-way — the supervisor tells the service
    about the *process*, and the service is the only thing allowed to publish what that
    meant for the track (ADR-004's "one publisher per fact").
    """

    @property
    def state(self) -> PlaybackState: ...

    def engine_lost(self, reason: str) -> None: ...

    def recovered(self, *, resumed: PlaybackState = PlaybackState.IDLE) -> None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryReport:
    """What the supervisor has done about mpv, for `/health` (SAPRS 11.7 step 8).

    Attributes:
        running: Whether there is an engine right now.
        restart_count: Restarts since the process started — the number a host watches.
        last_reason: Why the most recent restart happened, in words to act on.
        attempts: Failed restart attempts currently outstanding, 0 when none.
        retry_in: How long until the next attempt is allowed. Zero when it is due now.
    """

    running: bool = False
    restart_count: int = 0
    last_reason: str = ""
    attempts: int = 0
    retry_in: timedelta = timedelta(0)

    @property
    def waiting(self) -> bool:
        return self.attempts > 0 and self.retry_in > timedelta(0)

    @property
    def flapping(self) -> bool:
        return self.restart_count >= FLAPPING_RESTARTS


class PlaybackSupervisor:
    """Launches, monitors, restarts and reports the player (SAPRS 7.2).

    Args:
        launcher: The process factory.
        events: Where `PlaybackRecovered` and `HealthChanged` go.
        config: `playback:` — the binary path and the backoff ladder.
        service: The playback service, so a detected death becomes the state change and the
            event that only the service may publish. Referenced, not subscribed to.
        clock: For timestamps, and for deciding when a restart is due.
        logger: Where the diagnostics go.
    """

    def __init__(
        self,
        *,
        launcher: EngineLauncher,
        events: EventBus,
        config: PlaybackConfig,
        service: PlaybackNotifier | None = None,
        clock: Clock | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._launcher = launcher
        self._events = events
        self._config = config
        self._service = service
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._logger = logger or logging.getLogger("encore.playback.supervisor")
        self._channel: CommandChannel | None = None
        self._restart_count = 0
        self._attempts = 0
        self._last_reason = ""
        self._retry_at: datetime | None = None
        self._status = HealthStatus.UNAVAILABLE

    # -- lifecycle --------------------------------------------------------

    def start(self) -> CommandChannel:
        """Bring mpv up: SAPRS 11.7 step 6. Startup fails loudly if it cannot.

        Raises:
            EngineUnavailableError: mpv would not start. Named at startup rather than at
                the first play, because "the server is running and silent" is the one
                failure an appliance must not produce.
        """

        channel = self._launch()
        self._report(HealthStatus.HEALTHY, detail="mpv started")
        return channel

    def stop(self) -> None:
        """Let mpv go on shutdown (SAPRS 11.8 steps 3-4).

        Idempotent, because the shutdown handler runs from a signal and from the FastAPI
        lifespan, and a second `SIGTERM` to a reaped pid is an error in the log at the exact
        moment someone is reading the log to find out why the party stopped.
        """

        channel, self._channel = self._channel, None
        self._retry_at = None
        if channel is None:
            return
        close = getattr(channel, "close", None)
        if callable(close):
            close()
        self._launcher.terminate()

    def bind(self, service: PlaybackNotifier) -> None:
        """Name the service that owns the state machine, after construction.

        A dead process is reported *to* the service, and the service asks the monitor to bring
        it back — a cycle no constructor can express. ADR-011 makes the notification half of it
        event-based and leaves this half to the composition root; `bind` is the seam the
        composition root uses, so that wiring a player does not mean assigning a private
        attribute from outside the package.
        """

        self._service = service

    def channel(self) -> CommandChannel:
        """The live channel, starting one if the caller is the first to need it.

        Laziness here is not tidiness: a server that boots to an empty queue should not hold
        the audio device while a guest finds the page.
        """

        if self._channel is None:
            if self._waiting():
                raise MpvGoneError(self._pending_message())
            channel = self._launch()
            self._report(HealthStatus.HEALTHY, detail="mpv started on demand")
            return channel
        if not self._launcher.alive:
            recovered = self._report_death(reason=self._launcher.diagnostics())
            if recovered is None:
                raise MpvGoneError(self._pending_message())
            return recovered
        return self._channel

    @property
    def running(self) -> bool:
        return self._channel is not None and self._launcher.alive

    # -- monitoring (SAPRS 7.2, 7.6 step 1) ------------------------------

    def tick(self) -> bool:
        """One look at the engine; returns whether it is playing for us.

        The monitor SAPRS 7.2 asks for. Detecting death here rather than in the service
        keeps "mpv is a process" in one file, and means the recovery path is the same
        whether the death was noticed through a failed command or through a poll.
        """

        if self._channel is None:
            return False
        if self._launcher.alive:
            self._attempts = 0
            self._retry_at = None
            return True
        if self._waiting():
            return False
        self._report_death(reason=self._launcher.diagnostics())
        return self.running

    def recover(self, *, reason: str) -> bool:
        """Restart now, or arm the next attempt if a restart is already outstanding.

        This is `EngineRecovery`, the one thing `PlaybackService` is allowed to ask of
        anything outside its own class. Returning False is not a failure to handle: it means
        "the restart is scheduled", and the `PlaybackRecovered` event will say when it
        worked.
        """

        if self._waiting():
            return False
        if self._channel is not None and self._launcher.alive:
            # Somebody else got here first. Launching a second mpv because two callers asked
            # for the one that is already up would put the audio device in a fight.
            return True
        return self._recover_now(reason=reason) is not None

    def _report_death(self, *, reason: str) -> CommandChannel | None:
        """Say the engine died, once, and make sure a restart is in flight.

        The supervisor notices the process; the service publishes what that meant for the
        track and asks for the restart. Routing detection through the service rather than
        calling `_recover_now` here is what keeps the queue moving after a crash —
        `SongFinished` is the only thing the queue is listening for, and a supervisor that
        restarted mpv in silence would leave a `Playing` row in the database and a party
        waiting for a song that already died.

        The order is not cosmetic. `_recover_now` publishes `PlaybackRecovered`, and the
        queue's handler for that event starts the head item only if the engine is idle; if
        recovery were announced before the failure, the handler would find a track still
        marked `Playing`, do nothing, and the appliance would come back and stay quiet.
        """

        if self._service is not None:
            self._service.engine_lost(reason)
        if self._channel is not None and self._launcher.alive:
            return self._channel
        if self._waiting():
            return None
        return self._recover_now(reason=reason)

    # -- reporting (SAPRS 7.2's last bullet) ------------------------------

    def health(self) -> ComponentHealth:
        """This component's own report, for the aggregation milestone 11 will do properly.

        Liveness is `launcher.alive`, not "a channel object exists": a supervisor that
        called itself healthy while holding a socket to a dead mpv would be the dashboard
        lying, and the dead-process case is exactly when someone is reading it (SAPRS 11.7).
        """

        running = self._channel is not None and self._launcher.alive
        if running:
            return ComponentHealth(
                component=COMPONENT,
                status=HealthStatus.HEALTHY,
                detail=self._launcher.diagnostics(),
            )
        # "Not running" is never healthy, whatever the last report said. The alternative is a
        # dashboard that reads healthy in the second or so between an mpv dying and the poll
        # that notices — which is exactly when someone is looking at it.
        status = self._status if self._status is not HealthStatus.HEALTHY else HealthStatus.DEGRADED
        detail = (
            f"mpv is not running; {self._last_reason}"
            if self._last_reason
            else "mpv is not running"
        )
        return ComponentHealth(component=COMPONENT, status=status, detail=detail)

    @property
    def restart_count(self) -> int:
        return self._restart_count

    @property
    def report(self) -> RecoveryReport:
        now = self._clock.now()
        retry_in = (
            timedelta(0) if self._retry_at is None else max(timedelta(0), self._retry_at - now)
        )
        return RecoveryReport(
            running=self.running,
            restart_count=self._restart_count,
            last_reason=self._last_reason,
            attempts=self._attempts,
            retry_in=retry_in,
        )

    # -- internals --------------------------------------------------------

    def _recover_now(self, *, reason: str) -> CommandChannel | None:
        """Try to bring mpv back once, and either publish recovery or arm the ladder.

        One attempt per call, never a loop: the loop is `tick()`, and a method here that
        retried until it succeeded would be the blocking behaviour the module docstring
        refuses to have.
        """

        self._last_reason = reason.strip() or "mpv terminated unexpectedly"
        try:
            channel = self._launcher.launch()
        except (MpvGoneError, EngineUnavailableError) as error:
            self._attempts += 1
            delay = self._delay()
            self._retry_at = self._clock.now() + timedelta(seconds=delay)
            self._logger.warning(
                "mpv restart failed", extra={"attempt": self._attempts, "detail": str(error)}
            )
            self._report(
                HealthStatus.UNAVAILABLE
                if self._attempts >= UNAVAILABLE_AFTER_ATTEMPTS
                else HealthStatus.DEGRADED,
                detail=f"{self._last_reason}; retrying in {delay:g}s",
            )
            return None
        self._channel = channel
        self._attempts = 0
        self._retry_at = None
        self._restart_count += 1
        resumed = self._restore()
        self._events.publish(
            PlaybackRecovered(
                reason=self._last_reason,
                restart_count=self._restart_count,
                resumed_state=resumed,
                occurred_at=self._clock.now(),
            )
        )
        self._report(
            HealthStatus.DEGRADED
            if self._restart_count >= FLAPPING_RESTARTS
            else HealthStatus.HEALTHY,
            detail=f"recovered after {self._last_reason}",
        )
        return channel

    def _launch(self) -> CommandChannel:
        """Start mpv, with no recovery semantics: no event, no restart counted.

        Kept apart from `_recover_now` because an initial start is not a recovery, and a
        log that says "recovered after starting mpv" is the kind of sentence that sends
        someone looking for a crash that never happened.
        """

        try:
            channel = self._launcher.launch()
        except (MpvGoneError, EngineUnavailableError) as error:
            self._last_reason = str(error)
            self._attempts += 1
            self._retry_at = self._clock.now() + timedelta(seconds=self._delay())
            self._report(HealthStatus.UNAVAILABLE, detail=str(error))
            raise EngineUnavailableError(str(error), attempts=self._attempts) from error
        self._channel = channel
        self._attempts = 0
        self._retry_at = None
        return channel

    def _restore(self) -> PlaybackState:
        """Bring the service's state in line with the new process (SAPRS 7.6 step 6).

        The answer is always `Idle`, and then the queue decides whether that means silence
        or the next track. Restoring a *track* here would mean the supervisor knew what was
        queued, which is the coupling SAPRS 7.9's last acceptance line forbids.
        """

        if self._service is None:
            return PlaybackState.IDLE
        self._service.recovered(resumed=PlaybackState.IDLE)
        return self._service.state

    def _delay(self) -> float:
        """The ladder's rung for the attempt just failed, in seconds.

        The last value repeats: an appliance that stopped trying after five attempts would
        need someone to be there to restart it, and ADR-008's whole premise is that nobody
        is.
        """

        ladder = self._config.restart_backoff_seconds
        if not ladder:  # pragma: no cover - `PlaybackConfig` requires at least one rung
            return 1.0
        return float(ladder[min(self._attempts, len(ladder)) - 1])

    def _waiting(self) -> bool:
        """Whether a restart is armed and not yet due."""

        return self._retry_at is not None and self._clock.now() < self._retry_at

    def _pending_message(self) -> str:
        report = self.report
        return (
            f"mpv is not running: {report.last_reason}; attempt "
            f"{report.attempts + 1} in {report.retry_in.total_seconds():g}s"
        )

    def _report(self, status: HealthStatus, *, detail: str) -> None:
        """Publish a health move, if this is one (SAPRS 11.7 step 8).

        `HealthChanged` refuses a status that did not move, and it should: a stream that
        repeats "degraded" once a second is indistinguishable from one trying to say
        something.
        """

        previous = self._status
        self._status = status
        if previous is status:
            return
        self._events.publish(
            HealthChanged(
                status=status,
                previous=previous,
                component=COMPONENT,
                detail=detail,
                occurred_at=self._clock.now(),
            )
        )
