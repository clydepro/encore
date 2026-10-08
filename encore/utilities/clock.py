"""Time access (SAPRS 11.2, SAPRS 14.4).

Events are timestamped, and `docs/Developer/Testing.md` forbids unit tests from
depending on wall-clock timing: "use the injected clock once it exists". This is
the seam that promise refers to - a one-method protocol, a real implementation,
and nothing else.

Domain objects stamp themselves with `utc_now()` by default and accept an
explicit value, so a test can pin an instant without monkey-patching a module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


def utc_now() -> datetime:
    """The current UTC instant, always timezone-aware."""

    return datetime.now(UTC)


@runtime_checkable
class Clock(Protocol):
    """Something that can answer "what time is it?" — injectable, never global."""

    def now(self) -> datetime: ...


class SystemClock:
    """The real clock. The default wherever Encore needs one."""

    def now(self) -> datetime:
        return utc_now()


def ensure_aware(value: datetime, *, field_name: str) -> datetime:
    """Return `value` if it carries a timezone, else raise `ValueError`.

    A naive timestamp cannot be compared with an aware one, and Encore compares
    them: queue order, playback history and statistics all subtract one
    `occurred_at` from another. Failing at construction turns a class of silent
    `TypeError` at runtime into a loud error at the boundary.
    """

    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field_name} must be timezone-aware, got naive {value!r}")
    return value


__all__ = ["Clock", "SystemClock", "ensure_aware", "utc_now"]
