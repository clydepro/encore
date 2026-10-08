"""Event vocabulary (SAPRS 11.1-11.4, AIG 8, ADR-004).

An event is a fact that already happened. Three properties follow from that, and
they are enforced in code rather than in review comments:

* **Immutable.** Frozen dataclasses. A handler that could rewrite an event would
  change what every other handler believes occurred.
* **Typed.** Subclasses of `Event`, matched by type on subscribe. Nothing untyped
  travels on the bus, so a wrong name is an import error rather than a silent
  non-delivery.
* **Timestamped.** Each carries `occurred_at` in UTC (SAPRS 11.2), stamped by the
  publisher.

`occurred_at` defaults to now and accepts an explicit value, which is what makes
the rule in `docs/Developer/Testing.md` - no wall-clock timing in unit tests -
followable without a process-global clock.

One convention for subclass authors: run the base check with
`Event.__post_init__(self)`, not `super().__post_init__()`. These dataclasses use
`slots=True` for consistency with the domain entities, and `slots=True` makes
`@dataclass` rebuild the class - which orphans the zero-argument `super()` left in
the original class body, so it raises `TypeError` on the first publication rather
than at import. The explicit form cannot break, and
`tests/unit/test_events.py` publishes one of every event to prove it.

Nothing in this package imports HTTP, SQL or mpv. The Bus is `encore/events/bus.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from encore.utilities.clock import ensure_aware, utc_now

__all__ = ["Event"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    """Base class for every fact published on the Event Bus.

    Attributes:
        occurred_at: When the fact became true. Handlers order on it and the SSE
            stream forwards it, so it is timezone-aware or nothing: a naive
            timestamp cannot be compared with an aware one, and queue order and
            statistics both subtract one timestamp from another.
    """

    occurred_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        ensure_aware(self.occurred_at, field_name="occurred_at")

    @property
    def name(self) -> str:
        """The event's name, for logs and the SSE `event:` field (SAPRS 10.5)."""

        return type(self).__name__
