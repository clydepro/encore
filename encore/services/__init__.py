"""Application services: configuration, logging, queue rules, and the composition root.

Each service owns exactly one capability (AIG 7, SAPRS 11.6) and receives its dependencies
through its constructor. Services cooperate through the Event Bus rather than by direct
coupling — with one documented exception, `QueueService` calling the playback engine, which
is a command rather than a fact and is justified in ADR-011.

In place so far:

* `LoggingService` — structured logging (AEP 16), from milestone 2.
* `QueueService` — SAPRS Chapter 8's queue rules over `runtime.db`, from milestone 3.
* `build_core_services` — the composition root SAPRS 11.7 steps 1, 2 and 5 describe, and
  that `apps/server/main.py` (milestone 11) calls rather than duplicates.
* `LibraryService` — the read side of `library.db` that a request uses (milestone 11).
  It exists because the HTTP layer is the second reader issue #23 named as its trigger.
* `HealthService` — aggregation over component reports, and the publisher of the
  aggregate `HealthChanged` (SAPRS 11.6), from milestone 11.
* `SSEPublisher` — bus facts to browser frames (SAPRS 9.10, 10.5), from milestone 13.
  A service rather than an endpoint: it knows the bus and nothing about HTTP.

`StatisticsService` is the name on AIG 7's list that is *not* imported here. It needs
`StatisticKey`, the counter vocabulary the runtime repository owns, and importing it at
package level would put SQLAlchemy in front of every configuration and domain test in the
suite — the same reason phase 2 declined to wire the stores into `build_core_services`. The
composition root in `apps/server/` imports the module directly, which is where storage
belongs.

`AdminService` arrives with milestone 14 (SAPRS 10.8, ADR-013): the credential check, and
the sessions that follow from it. It holds the one piece of runtime state in this process
that is deliberately not in a database, and ADR-013 is where that is argued rather than
assumed.

Search lives in `encore/search/` and playback in `encore/playback/`, each a package with its
own public surface: a service does not appear here to describe itself, it appears when it
does something. `LibraryService`, `HealthService`, `StatisticsService`, `AdminService` and
`SSEPublisher` are the five named as arriving with their milestones; five of them have.
"""

from __future__ import annotations

from encore.services.admin_service import AdminService, AdminSession
from encore.services.container import CoreServices, build_core_services
from encore.services.errors import QueueError, QueueFullError, SongNotAvailableError
from encore.services.health_service import HealthService, HealthSnapshot
from encore.services.library_service import LibraryService, Page
from encore.services.logging_service import LoggingService
from encore.services.queue_service import (
    QueueEntry,
    QueueService,
    QueueStore,
    SongLookup,
)
from encore.services.sse_publisher import SSEPublisher

__all__ = [
    "AdminService",
    "AdminSession",
    "CoreServices",
    "HealthService",
    "HealthSnapshot",
    "LibraryService",
    "LoggingService",
    "Page",
    "QueueEntry",
    "QueueError",
    "QueueFullError",
    "QueueService",
    "QueueStore",
    "SSEPublisher",
    "SongLookup",
    "SongNotAvailableError",
    "build_core_services",
]
