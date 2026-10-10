"""The Health Service: aggregation, and the only publisher of "the appliance is X".

SAPRS 11.6 gives Health Service ownership of health aggregation, and SAPRS 11.7 step 8
makes publishing the ready/health state part of starting up. Until now the only
`HealthChanged` on the bus came from the playback supervisor, which reports its own
component (and should: nobody knows mpv's liveness better). What nobody owned was the
aggregate — the single word a banner, a readiness probe and an `EventSource` all need.

Two rules, both stated in the code that enforces them:

* **A component reports, the aggregate publishes.** `check()` asks each source for its
  `ComponentHealth`, takes the worst by `HealthStatus.severity`, and publishes
  `HealthChanged` only when the aggregate has actually moved. `HealthChanged` refuses a
  status that did not move (ADR-004), so the second rule is partly enforced by the event.
* **A source that fails is itself a health fact, not a crash.** `check()` catches what a
  probe raises and turns it into an `UNAVAILABLE` component report with the exception's
  message. The alternative is a monitoring service that terminates the tick it was called
  from — SAPRS 11.9's exact prohibition.

Nothing here knows about HTTP, and nothing here subscribes to anything: health is polled
by the server's tick (once a second, inside SAPRS 1.8's SSE budget) and by the endpoint
that is asked. A push model would need a component to tell the service when it changed,
which every component already does by publishing `HealthChanged` for itself.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from encore.domain.health import ComponentHealth, HealthStatus
from encore.events.bus import EventBus
from encore.events.health import HealthChanged
from encore.utilities.clock import Clock, SystemClock

__all__ = ["HealthService", "HealthSnapshot", "HealthSource"]

#: The component name on an aggregate report. Deliberately not one of the sources' own
#: names, so a reader can tell "playback is degraded" from "the appliance is degraded".
AGGREGATE = "appliance"


class HealthSource(Protocol):
    """Something that can say how it is. `PlaybackSupervisor` is the first."""

    def health(self) -> ComponentHealth: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthSnapshot:
    """The appliance's health at one instant, with the reasons beside it.

    `status` is the aggregate and `components` is the per-source detail, which is what
    SAPRS 1.5 means by "clear health information": a word, and the thing behind the word.
    """

    status: HealthStatus
    checked_at: datetime
    components: tuple[ComponentHealth, ...] = ()

    @property
    def detail(self) -> str:
        """The worst component's message, or the quiet word when nothing is wrong."""

        worst = _worst(self.components)
        if worst is None:
            return "all components reporting"
        return f"{worst.component}: {worst.detail}" if worst.detail else worst.component

    @property
    def is_ready(self) -> bool:
        """Whether the appliance should be taking requests at all (SAPRS 11.7 step 8)."""

        return self.status is not HealthStatus.UNAVAILABLE


class HealthService:
    """Collects component reports, publishes the aggregate when it moves.

    Args:
        sources: Named components. Order is preserved in the report, so the first is
            the one an operator reads first — pass playback first.
        events: Where `HealthChanged` goes when the aggregate moves.
        clock: Injectable, because `checked_at` is part of the answer.
        logger: For a source that raised rather than reported.
    """

    def __init__(
        self,
        *,
        sources: Sequence[tuple[str, HealthSource]],
        events: EventBus,
        clock: Clock | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._sources = tuple(sources)
        self._events = events
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._logger = logger or logging.getLogger("encore.health")
        self._status = HealthStatus.HEALTHY
        self._snapshot = HealthSnapshot(status=HealthStatus.HEALTHY, checked_at=self._clock.now())

    # -- the reads ---------------------------------------------------------

    @property
    def status(self) -> HealthStatus:
        """The last computed aggregate. Never touches a component, so a handler can
        ask without provoking the probe it is reporting on."""

        return self._status

    @property
    def snapshot(self) -> HealthSnapshot:
        """The last complete report, for a fragment that renders without polling."""

        return self._snapshot

    def check(self) -> HealthSnapshot:
        """Ask every component how it is, publish if the answer moved, return it.

        Called by the server's tick and by `GET /api/v1/health`. It is not free — a
        probe may read a process table or a database header — which is why the
        properties above exist and why this one is only called from the appliance
        thread (ADR-012).
        """

        components = tuple(self._probe(name, source) for name, source in self._sources)
        # An appliance with no health sources is not healthy, it is unwired: the `default`
        # argument that would make this one expression would have to be `HEALTHY`, which is
        # the one answer a configuration mistake must not be allowed to produce — the
        # readiness path would tell systemd this box is fine (SAPRS 11.6).
        aggregate = (
            max((component.status for component in components), key=_severity)
            if components
            else HealthStatus.UNAVAILABLE
        )
        previous = self._status
        self._status = aggregate
        self._snapshot = HealthSnapshot(
            status=aggregate, checked_at=self._clock.now(), components=components
        )
        if aggregate is not previous:
            worst = _worst(components)
            self._events.publish(
                HealthChanged(
                    status=aggregate,
                    previous=previous,
                    # The component behind the move, not the word "aggregate": SAPRS 1.5's
                    # "clear health information" is a name and a sentence, and a fact that
                    # says only "degraded" sends an operator to read four components to
                    # find out which one is lying.
                    component=worst.component if worst is not None else AGGREGATE,
                    detail=worst.detail if worst is not None else "",
                    occurred_at=self._clock.now(),
                )
            )
            self._logger.info(
                "appliance health changed",
                extra={
                    "status": aggregate.value,
                    "previous": previous.value,
                    "component": None if worst is None else worst.component,
                },
            )
        return self._snapshot

    # -- internals ---------------------------------------------------------

    def _probe(self, name: str, source: HealthSource) -> ComponentHealth:
        """One component's report, with a failure turned into a status.

        A source that raises is reporting the worst possible thing about itself, and
        the aggregate needs a value rather than a traceback: a monitoring service that
        can be taken down by the thing it watches is not monitoring (SAPRS 11.9).
        """

        try:
            return source.health()
        except Exception as error:
            self._logger.exception("health source failed", extra={"component": name})
            return ComponentHealth(
                component=name,
                status=HealthStatus.UNAVAILABLE,
                detail=f"{type(error).__name__}: {error}",
            )


def _severity(status: HealthStatus) -> int:
    return status.severity


def _worst(components: Sequence[ComponentHealth]) -> ComponentHealth | None:
    if not components:
        return None
    return max(components, key=lambda component: component.status.severity)
