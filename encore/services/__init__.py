"""Application services: configuration, logging, and the composition root.

Each service owns exactly one capability (AIG 7, SAPRS 11.6) and receives its
dependencies through its constructor. Services cooperate through the Event Bus
rather than by direct coupling.

Populated so far, by milestone 2 (AIG 21 steps 2-4):

* `LoggingService` - structured logging (AEP 16).
* `build_core_services` - the composition root that SAPRS 11.7 steps 1, 2 and 5
  describe, and that `apps/server/main.py` (milestone 11) will call rather than
  duplicate.

`LibraryService`, `SearchService`, `QueueService`, `PlaybackService`,
`PlaybackSupervisor`, `HealthService`, `StatisticsService` and `SSEPublisher`
arrive with their own milestones. A service does not appear here to describe
itself; it appears when it does something.
"""

from __future__ import annotations

from encore.services.container import CoreServices, build_core_services
from encore.services.logging_service import LoggingService

__all__ = ["CoreServices", "LoggingService", "build_core_services"]
