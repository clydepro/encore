"""Application services: configuration, logging, queue rules, and the composition root.

Each service owns exactly one capability (AIG 7, SAPRS 11.6) and receives its dependencies
through its constructor. Services cooperate through the Event Bus rather than by direct
coupling — with one documented exception, `QueueService` calling the playback engine, which
is a command rather than a fact and is justified in ADR-011.

In place so far:

* `LoggingService` — structured logging (AEP 16), from milestone 2.
* `QueueService` — SAPRS Chapter 8's queue rules over `runtime.db`, from milestone 3.
* `build_core_services` — the composition root SAPRS 11.7 steps 1, 2 and 5 describe, and
  that `apps/server/main.py` (milestone 11) will call rather than duplicate.

Search lives in `encore/search/` and playback in `encore/playback/`, each a package with its
own public surface: a service does not appear here to describe itself, it appears when it
does something. `LibraryService`, `HealthService`, `StatisticsService` and `SSEPublisher`
arrive with their milestones.
"""

from __future__ import annotations

from encore.services.container import CoreServices, build_core_services
from encore.services.errors import QueueError, QueueFullError, SongNotAvailableError
from encore.services.logging_service import LoggingService
from encore.services.queue_service import (
    QueueEntry,
    QueueService,
    QueueStore,
    SongLookup,
)

__all__ = [
    "CoreServices",
    "LoggingService",
    "QueueEntry",
    "QueueError",
    "QueueFullError",
    "QueueService",
    "QueueStore",
    "SongLookup",
    "SongNotAvailableError",
    "build_core_services",
]
