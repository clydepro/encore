"""Immutable event types and the in-process Event Bus (SAPRS 11.1, AIG 8).

Events are facts that already happened: typed, timestamped and immutable.
Services publish here instead of calling one another; one failing subscriber must
never prevent unrelated subscribers from receiving an event.

The vocabulary is the eight types AIG 8 and ADR-004 fix. `EVENT_VOCABULARY` is
that list as data, and `tests/unit/test_events.py` asserts it is complete and
exclusive, so the set cannot drift without a failing test.

Import from `encore.events`; the module split - by the area that owns each fact -
is an implementation detail and may move.
"""

from __future__ import annotations

from encore.events.base import Event
from encore.events.bus import (
    MAX_DISPATCH_DEPTH,
    DeliveryReport,
    EventBus,
    EventCycleError,
    Subscription,
)
from encore.events.health import HealthChanged
from encore.events.library import BuildCompleted, LibraryReloaded
from encore.events.playback import FinishedReason, PlaybackRecovered, SongFinished, SongStarted
from encore.events.queue import QueueAdvanced, SongQueued

#: The eight facts AIG 8 names, in the order it names them.
EVENT_VOCABULARY: tuple[type[Event], ...] = (
    SongQueued,
    SongStarted,
    SongFinished,
    QueueAdvanced,
    PlaybackRecovered,
    LibraryReloaded,
    BuildCompleted,
    HealthChanged,
)

__all__ = [
    "EVENT_VOCABULARY",
    "MAX_DISPATCH_DEPTH",
    "BuildCompleted",
    "DeliveryReport",
    "Event",
    "EventBus",
    "EventCycleError",
    "FinishedReason",
    "HealthChanged",
    "LibraryReloaded",
    "PlaybackRecovered",
    "QueueAdvanced",
    "SongFinished",
    "SongQueued",
    "SongStarted",
    "Subscription",
]
