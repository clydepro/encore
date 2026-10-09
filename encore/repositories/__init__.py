"""Storage access, split by mutability (SAPRS 5, ADR-009).

Two halves, and the split is the point:

| | `library/` | `runtime/` |
| --- | --- | --- |
| File | `library.db`, built offline | `runtime.db`, owned by the Server |
| Access | raw `sqlite3`, `mode=ro` | SQLAlchemy 2.x, WAL |
| Operations | `SELECT` only | transactional reads and writes |
| Schema | versioned, never migrated at runtime | numbered, forward-only migrations |

Both halves return names from `encore.domain` and nothing else. That boundary — no
`Engine`, `Session`, `Row` or `Result` reaches a service, controller or template — is
what `tests/unit/test_architecture_guardrails.py` enforces, and it is the reason `library.db`
corruption surfaces as a startup error naming a file rather than as an `AttributeError`
inside a template four layers away.

`contract.py` holds the names both halves share, and imports nothing from either.
"""

from __future__ import annotations

from encore.repositories.contract import (
    COLUMNS,
    LIBRARY_SCHEMA_VERSION,
    LIBRARY_TABLES,
    METADATA_FIELDS,
    METADATA_SOURCES,
    QUEUE_STATUSES,
    RUNTIME_TABLES,
    SEARCH_VIEWS,
    AlbumColumn,
    ArtistColumn,
    ArtworkColumn,
    FileColumn,
    MetadataColumn,
    MetadataField,
    MetadataSource,
    RuntimeTable,
    SongColumn,
    Table,
    columns_for,
)
from encore.repositories.errors import (
    ContractViolationError,
    LibraryContractError,
    RepositoryError,
    RuntimeContractError,
    SearchUnavailableError,
    StoreError,
    StoreNotFoundError,
)
from encore.repositories.library import (
    AlbumHit,
    ArtistHit,
    LibraryConnection,
    LibraryInfo,
    LibrarySearch,
    LibraryStore,
    SongHit,
    open_library,
)
from encore.repositories.runtime import (
    CURRENT_VERSION,
    AdminStateRepository,
    HistoryEntry,
    PlaybackHistoryRepository,
    QueueRepository,
    RuntimeConfig,
    RuntimeStatisticsRepository,
    RuntimeStore,
    RuntimeStoreInfo,
    StatisticKey,
    open_runtime_store,
)

__all__ = [
    "COLUMNS",
    "CURRENT_VERSION",
    "LIBRARY_SCHEMA_VERSION",
    "LIBRARY_TABLES",
    "METADATA_FIELDS",
    "METADATA_SOURCES",
    "QUEUE_STATUSES",
    "RUNTIME_TABLES",
    "SEARCH_VIEWS",
    "AdminStateRepository",
    "AlbumColumn",
    "AlbumHit",
    "ArtistColumn",
    "ArtistHit",
    "ArtworkColumn",
    "ContractViolationError",
    "FileColumn",
    "HistoryEntry",
    "LibraryConnection",
    "LibraryContractError",
    "LibraryInfo",
    "LibrarySearch",
    "LibraryStore",
    "MetadataColumn",
    "MetadataField",
    "MetadataSource",
    "PlaybackHistoryRepository",
    "QueueRepository",
    "RepositoryError",
    "RuntimeConfig",
    "RuntimeContractError",
    "RuntimeStatisticsRepository",
    "RuntimeStore",
    "RuntimeStoreInfo",
    "RuntimeTable",
    "SearchUnavailableError",
    "SongColumn",
    "SongHit",
    "StatisticKey",
    "StoreError",
    "StoreNotFoundError",
    "Table",
    "columns_for",
    "open_library",
    "open_runtime_store",
]
