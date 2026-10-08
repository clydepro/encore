"""Health vocabulary (SAPRS 1.5, 11.6, 11.7).

`HealthService` aggregates health; it does not own the words for it. They live
here because the dashboard, the SSE stream, the systemd readiness probe and
`HealthChanged` must all agree on what "degraded" means, and an enum buried in a
service module is the wrong place to look that up.

Three states, because that is the distinction an operator acts on: the box is
playing, the box is playing with something wrong, or the box is not going to
play. A fourth grade would be answered the same way as one of these three.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

__all__ = ["HEALTHY", "ComponentHealth", "HealthStatus"]


class HealthStatus(StrEnum):
    """Aggregate state of the appliance (SAPRS 1.5 "clear health information")."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"

    @property
    def severity(self) -> int:
        """Ordering, so aggregation is `max()` over component reports.

        Values are spaced rather than 0/1/2 so a later state can be inserted
        between two existing ones without renumbering the others.
        """

        return _SEVERITY[self]

    @property
    def is_healthy(self) -> bool:
        return self is HealthStatus.HEALTHY


_SEVERITY: Final[Mapping[HealthStatus, int]] = MappingProxyType(
    {
        HealthStatus.HEALTHY: 0,
        HealthStatus.DEGRADED: 10,
        HealthStatus.UNAVAILABLE: 20,
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ComponentHealth:
    """One component's report — mpv, a database, the artwork cache.

    Attributes:
        component: Stable name for whatever is reporting, e.g. ``"playback"``.
            Names are part of the admin surface, so they are not free text; a
            report with an unnamed source cannot be routed to an operator.
        status: How bad this report is.
        detail: Short, operator-facing explanation. Must never carry
            credentials, session material or personal data (AEP 16).
    """

    component: str
    status: HealthStatus = HealthStatus.HEALTHY
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.component.strip():
            raise ValueError("a health report needs the name of what is reporting")


HEALTHY: ComponentHealth = ComponentHealth(component="system")
