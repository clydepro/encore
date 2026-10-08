"""Health facts (SAPRS 11.7, 11.9, AIG 8)."""

from __future__ import annotations

from dataclasses import dataclass

from encore.domain.health import HealthStatus
from encore.events.base import Event

__all__ = ["HealthChanged"]


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthChanged(Event):
    """Aggregate health moved, or the component behind the move is named.

    `HealthService` owns the aggregation (SAPRS 11.6); this event only reports
    its result. `component` is the report that caused the change, so an operator
    reading the dashboard sees "degraded: playback" rather than a bare
    "degraded" — the difference between a ten-second answer and a troubleshooting
    session (SAPRS 1.5).

    `detail` is operator-facing text. It must never carry credentials, session
    material or personal data (AEP 16, AEP 17).
    """

    status: HealthStatus
    previous: HealthStatus | None = None
    component: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        Event.__post_init__(self)
        if self.previous is self.status:
            raise ValueError(
                f"HealthChanged reports a move; {self.status} -> {self.previous} is not one"
            )
