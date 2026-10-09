"""`runtime.db` writes: SQLAlchemy 2.x, WAL, migrated, pooled (SAPRS 5.7, ADR-009).

The counterpart to `encore/repositories/library/`, and the half of ADR-009 where the ORM
belongs. Four repositories, one store, and one rule that matters more than the
arrangement: nothing in here contains a rule about music, queues or playback. A
`QueueRepository` that decided which item is next would be a second `QueueService`, and
the two would eventually disagree.

The engine is created once per process and disposed on shutdown, which is what makes
WAL's write concurrency available at all — a per-request engine would re-run the
`PRAGMA journal_mode` dance and, on a slow card, spend more time on the filesystem than
on the query.
"""

from __future__ import annotations

from encore.repositories.runtime.admin_state import (
    AdminStateRepository,
    HashFormat,
    PasswordHasher,
    Pbkdf2Hasher,
    current_scheme_is_current,
    generate_password,
    parse_hash,
)
from encore.repositories.runtime.history import HistoryEntry, PlaybackHistoryRepository
from encore.repositories.runtime.migrations import (
    CURRENT_VERSION,
    MIGRATIONS,
    Migration,
    UpgradeReport,
    upgrade,
)
from encore.repositories.runtime.models import (
    AdminState,
    Base,
    PlaybackHistory,
    QueueItemRow,
    RuntimeStatistics,
    SchemaVersion,
)
from encore.repositories.runtime.queue import QueueRepository
from encore.repositories.runtime.session import (
    RuntimeConfig,
    configure_wal,
    create_runtime_engine,
    session_factory,
    session_scope,
)
from encore.repositories.runtime.statistics import (
    RuntimeStatisticsRepository,
    StatisticKey,
    StatisticsSnapshot,
)
from encore.repositories.runtime.store import RuntimeStore, RuntimeStoreInfo, open_runtime_store

__all__ = [
    "CURRENT_VERSION",
    "MIGRATIONS",
    "AdminState",
    "AdminStateRepository",
    "Base",
    "HashFormat",
    "HistoryEntry",
    "Migration",
    "PasswordHasher",
    "Pbkdf2Hasher",
    "PlaybackHistory",
    "PlaybackHistoryRepository",
    "QueueItemRow",
    "QueueRepository",
    "RuntimeConfig",
    "RuntimeStatistics",
    "RuntimeStatisticsRepository",
    "RuntimeStore",
    "RuntimeStoreInfo",
    "SchemaVersion",
    "StatisticKey",
    "StatisticsSnapshot",
    "UpgradeReport",
    "configure_wal",
    "create_runtime_engine",
    "current_scheme_is_current",
    "generate_password",
    "open_runtime_store",
    "parse_hash",
    "session_factory",
    "session_scope",
    "upgrade",
]
