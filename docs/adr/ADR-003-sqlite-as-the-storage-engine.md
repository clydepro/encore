# ADR-003: SQLite as the Storage Engine

## Status

Accepted

## Date

2026-10-07

## Context

Encore needs two very different stores: a read-mostly catalogue of ~15,000
songs with artist/album/text lookup, and a small mutable store for the queue,
playback history and statistics. The appliance must run on a Raspberry Pi 4
with one process, no network service to secure, no schema migration fleet and
no database administrator.

SAPRS Chapter 5 fixes the shape: two database files, normalized canonical
tables, denormalized search structures with FTS5, artwork referenced rather than
embedded.

## Decision

Use **SQLite** for both stores, accessed through SQLAlchemy 2.x repositories.

- `library.db` — produced by the Builder, opened read-only by the Server
  (see ADR-006).
- `runtime.db` — the appliance's mutable state: queue, history, statistics and
  administrative state.
- FTS5 virtual tables provide artist/album/song search inside the same file, so
  a query is a single local open with no serialization hop.
- WAL mode for `runtime.db` so the queue can be written while SSE readers
  query.
- Schema versions are recorded and migrated explicitly (SAPRS 5.9, AEP 18);
  neither database is ever "modified informally".

## Consequences

Positive:

- Zero-service storage: one file to copy, verify, back up and roll forward.
- Search and relational access share one query planner and one transaction
  monitor, which is how the < 100 ms budget stays reachable.
- Deterministic behaviour during tests — fixtures are literally temporary files.

Costs:

- Single-writer concurrency; queue contention must stay short and queued in
  process.
- No cross-host replication, which is fine for one appliance and out of scope
  for v1.
- FTS5 must be present in the linked SQLite build; the installer verifies it and
  the test helpers skip rather than fail misleadingly otherwise.

## Alternatives considered

- **PostgreSQL.** Rejected: a daemon, an auth model and a backup story for a
  device that is meant to be unplugged and moved.
- **DuckDB.** Rejected: analytics-first design, weaker fit for frequent small
  writes from a queue.
- **JSON/files on disk.** Rejected: no transactional integrity for queue and
  history, and search would be reimplemented badly.
- **Embedded key-value store (LMDB/sled) plus a custom index.** Rejected: the
  query shapes are relational and text-search heavy; this duplicates SQLite.
- **Whoosh/Tantivy alongside SQLite.** Rejected: a second index to keep in sync
  when FTS5 already lives in the same file as the canonical rows.
