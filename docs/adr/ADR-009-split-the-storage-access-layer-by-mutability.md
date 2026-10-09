# ADR-009: Split the Storage Access Layer by Mutability

## Status

Accepted, with the rationale amended on the day of acceptance. Supersedes ADR-003.

The decision is unchanged; three of its first justifications failed measurement.
Read-only enforcement belongs to the connection URI, not the choice of driver; the
performance case for raw `sqlite3` is void at 15,000 songs; and SQLAlchemy Core was
never considered, despite being the strongest alternative. What follows is the
version that survived being checked.

## Date

2026-10-09

## Context

ADR-003 chose SQLite for both stores and said both would be "accessed through
SQLAlchemy 2.x repositories". The SQLite half stands and is not reopened. The
access-layer half was written when neither database existed.

SAPRS Chapter 5 states the two stores' requirements separately: `library.db` is
opened read-only and the runtime "must never perform INSERT, UPDATE, DELETE, or
schema changes" against it (5.2, ADR-006), while `runtime.db` holds queue, history
and statistics with transactions and concurrent readers (5.7, WAL per ADR-003).

An ORM mapped over something defined as never changing buys identity maps, unit of
work and flush semantics in the read path — machinery that can only misfire there,
and that makes a playback-critical query depend on session expiry policy. For
`runtime.db` the same features are the point: ADR-008's premise is that nobody at
the party can fix the appliance, so the transactional store's crash recovery is not
a detail.

The corpus is 3,049 files and must reach 15,000 (SAPRS 1.8). Rather than argue it, a
15,000-song / 4,500-album library with FTS5 was built and timed, medians of repeated
runs against one warm file:

| query shape | raw `sqlite3` | SQLAlchemy Core | SQLAlchemy ORM |
| ----------- | ------------- | --------------- | -------------- |
| search page, 50 rows | 2.06 ms | 4.53 ms | 4.66 ms |
| album navigation, mapped to `Song` | 0.12 ms | 1.74 ms | 2.39 ms |
| single-row lookup on the load path | 22 µs | 98 µs | ~100 µs |

Every figure is at least 10× inside the SAPRS 1.8 budgets. **Performance does not
choose between these options** and this decision must not be defended on that
ground. What does choose it is where the schema lives.

## Decision

**Access each database through the layer that matches how it is written to.**

- **`library.db` — raw `sqlite3`, read-only.** Opened with a URI of the form
  `file:/var/lib/encore/library.db?mode=ro`, plus `immutable=1` only where the
  platform supports it and the file is confirmed static. Every connection carries
  `PRAGMA query_only = ON`. Row access via a `row_factory`; no ORM, no session, no
  metadata registry.
- **`runtime.db` — SQLAlchemy 2.x.** Declarative mappings and explicit transactions
  for queue, history, statistics and administrative state; WAL mode; one session per
  logical operation, never one per request thread.
- **The split stops at the repository boundary.** `encore/repositories/` returns
  `encore.domain` types. No `Session`, `Engine`, `Row` or `Result` reaches a service,
  controller or template. This bullet describes the **Server**; the Builder's write
  path lives in `apps/builder/` under ADR-010, so nothing in `encore/` imports build
  code.
- **Schema ownership is not duplicated.** The `library.db` DDL exists in the Builder
  alone; the Server holds `SELECT` statements and no `Table` metadata, and refuses an
  incompatible version stamp at open (ADR-006's publication contract). `library.db`
  is never migrated — a new schema is a new build.
- **`runtime.db` is migrated explicitly and forward-only** (5.9): numbered modules
  under `encore/repositories/runtime_migrations/`, applied in one transaction at
  startup, tracked in a `schema_version` row. The installer refuses to start on a
  database requiring a downgrade. This settles an open question from issue #19.
- **Both stores are verified by shape, not by handle.** `sqlite3.connect()` and
  `create_engine()` both *create* a missing file and return a usable connection to
  it, so a typo in `paths.library_db` boots an appliance with zero songs and no
  error. Startup must assert the file exists, contains the expected tables, and log
  the song count where an operator would see it. `mode=ro` fails loudly on a missing
  file by accident, and is not relied on as that check.

No dependency is added: SQLAlchemy is already declared, `sqlite3` is standard
library.

## Consequences

Positive:

- The `library.db` schema exists in exactly one place — the file the Builder writes.
  No mirror to drift, no reflection step to reconcile two definitions.
- Read-only is enforced by SQLite rather than by discipline. That is credited to
  `mode=ro` and `query_only`, both of which sit below the access layer and behave
  identically for either tool; `test_only_repositories_touch_sqlite` already refuses
  a `sqlite3` import outside repositories, and can now also be tested behaviourally
  by attempting a write and requiring SQLite to reject it.
- The transactional path gets maintained crash recovery instead of hand-rolled
  `BEGIN` placement, which is the part of storage where correctness is genuinely
  hard.
- Two small seams rather than one abstraction describing two unrelated things.

Costs:

- Two idioms in one package. The question "which side am I on?" is answered by "does
  this write?", and the directory layout has to make that visible rather than
  discoverable by accident.
- Hand-written SQL means a column rename surfaces at runtime, not in the type
  checker. Mitigation: one module naming every column, plus a test that every SELECT
  resolves against a real built database.
- Type affinity is our business. Measured: `BOOLEAN` returns `int` through raw
  `sqlite3` and `bool` through SQLAlchemy, and a datetime stored as `TEXT` is
  reflected as `TEXT` by both, so neither converts it. Strict MyPy flags
  `no-any-return` on raw row access, which forces each row→entity mapper to write its
  coercion down; `CHECK` constraints in the DDL are the other half.
  `encore.utilities.clock.ensure_aware()` therefore belongs on the read path.
- A migration framework is code we own — roughly one module.

## Alternatives considered

- **SQLAlchemy repositories for both stores** (ADR-003). Rejected: session machinery
  in a read-only path can only misfire. Not rejected for speed, and not for
  read-only reasons — an ORM engine given `mode=ro` is as read-only as anything.
- **SQLAlchemy Core, no ORM, for `library.db`.** The strongest alternative: it
  removes the two-idiom objection while keeping one library. Rejected because
  `Table` objects must come from somewhere, and the two sources are both worse than
  none: reflection costs ~20 ms at startup and again on every `LibraryReloaded`,
  while hand-declared metadata is the duplicated DDL ADR-010 exists to prevent.
  Reflection is also incomplete exactly where this project leans: an FTS5 virtual
  table reflects its three content columns and **no `rowid`**, so the join becomes
  `literal_column("song_search.rowid")` — raw SQL reintroduced to buy the abstraction
  that removes it — and a bare `rowid` then fails at runtime as `ambiguous column
  name`. Virtual tables declare no foreign keys, so Core's join inference refuses
  outright ("Can't determine which FROM clause to join from"), leaving the FROM
  clause hand-established on precisely the queries that matter. Revisit if the
  Builder ever emits SQLAlchemy metadata as a build artifact, which would make both
  halves one source again.
- **Raw `sqlite3` for both stores.** Rejected: puts hand-written transaction
  demarcation and retry-on-busy under the queue, the one place a torn write loses a
  party, to avoid a dependency already declared.
- **One repository abstraction with a pluggable backend.** Rejected: an interface
  spanning "read-only catalogue query" and "transactional queue write" specifies the
  least common multiple of two unrelated things, and every capability that is not the
  common multiple needs an escape hatch. The domain-facing interfaces are already the
  seam.
- **Alembic for `runtime.db`.** Rejected: built for metadata-diffing and fleets of
  targets. One appliance, one schema, forward-only, versioned in a table — numbered
  modules are easier to audit in a PR and cannot silently regenerate a migration from
  model drift. Revisit if downgrades are ever needed.
- **PostgreSQL for `runtime.db` only.** Rejected: ADR-003's argument against a daemon
  is stronger for a second store than a first, and two engines doubles the backup
  story for a device meant to be unplugged and moved.
