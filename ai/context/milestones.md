# Context Bundle — Milestones and Current State

What exists, what is scaffolding, and what comes next. Keep this page honest: it
is the first thing a session should read to avoid implementing something twice
or too early.

## Implementation order (AIG 21)

| # | Deliverable | State |
| - | ----------- | ----- |
| 1 | Repository initialization (PBK) | **Done** |
| 2 | Core domain model | **Done — `encore/domain/`** |
| 3 | Event Bus | **Done — `encore/events/`** |
| 4 | Configuration | **Done — `encore/config/` + `services/logging_service.py`** |
| 5 | Repositories | **Done — `encore/repositories/`** |
| 6 | Library Builder | **Done — `apps/builder/`, `encore-builder`** |
| 7 | Search | Not started ← **next** |
| 8 | Playback | Not started |
| 9 | Queue | Not started |
| 10 | Runtime database | Not started |
| 11 | FastAPI | Not started |
| 12 | HTMX | Not started |
| 13 | SSE | Not started |
| 14 | Administrative interface | Not started |
| 15 | Installer | Not started |
| 16 | Party Simulation | Not started |
| 17 | Documentation | Ongoing |

The steps are ordered, not independent: 2–4 are the ground that 5 and 6 stand
on. Step 5 is where `library.db` and `runtime.db` schemas first exist, which is
why nothing before them touched SQLite — and step 6 is where `library.db`'s DDL
actually lives, which is why 5 and 6 were done together rather than in sequence.

PBK's ten-milestone view groups the same work; both are listed in the README
roadmap. Steps 2, 3 and 4 are PBK milestone 2 ("Core Framework"), and are
declared complete against its checklist in the section below.

## What steps 2–4 delivered

- `encore/domain/` — SAPRS Chapter 4 as immutable dataclasses, plus the
  identifiers, enums and the 7.3 transition table. `encore.domain` is the public
  surface; modules inside it may move.
- `encore/events/` — the eight AIG 8 types and `EventBus`. `EVENT_VOCABULARY` is
  that list as data and `tests/unit/test_events.py` asserts it stays exactly
  eight, so a ninth event is a deliberate ADR-004 change or a failing test.
- `encore/config/` — Pydantic models matching `examples/config.yaml`, and
  `ConfigurationService`, which fails startup naming every problem and its
  location (SAPRS 12.5). It has no write method, and neither does anything else
  here: configuration is not managed state (12.3, 12.7).
- `encore/services/` — `LoggingService` (JSON to the journal, AEP 16) and
  `build_core_services()`, the composition root. There is no `EncoreServer` yet
  and no module-level service instance anywhere (AEP 9); a guardrail test checks
  that rather than trusting it.

Decisions taken here that later steps must honour:

- Events are constructed facts. `SongQueued.position` must agree with
  `queue_length`; `HealthChanged` refuses a status that did not move. A service
  that needs to report "nothing changed" does not publish.
- Timestamps are timezone-aware UTC, injected through
  `encore.utilities.clock`. Anything that subtracts two of them (queue order,
  history, statistics) depends on it.
- Normalization of artist/album/song names is **not** in the domain. SAPRS 6.5
  puts it in the Builder; `normalized_*` fields exist for the Builder to fill and
  nothing computes them yet.
- `library.db`/`runtime.db` paths come from `paths:` in configuration only, and
  the service refuses them if they are the same file.

## What steps 5–6 delivered

- `encore/repositories/contract.py` — every table and column of both databases,
named once. The Builder writes them, the Server reads them, and neither can drift
silently; `tests/integration/test_library_contract.py` proves it by executing every
library statement against a real built database.
- `encore/repositories/library/` — the read side: `mode=ro` URI +
`PRAGMA query_only=ON`, one module of SQL, one of row mapping, contentless FTS5
search through `song_search`/`album_search`/`artist_search`.
- `encore/repositories/runtime/` — the write side: SQLAlchemy 2.x over WAL, ORM
models, numbered forward-only migrations applied on open, queue/history/statistics
and admin-state repositories that speak domain identifiers.
- `apps/builder/` — the pipeline of ADR-010, the `encore-builder` console script,
per-field provenance, the skip ledger, incremental reuse, validation and atomic
publication. The only writer of `library.db` and of the artwork cache.
- `paths.music_dir` in configuration, `examples/config.yaml` and the Administrator
guide together, as ADR-010 required.

Two things this phase deliberately did **not** do: it did not wire the stores into
`build_core_services` (that would pull storage into every config and domain test),
and it did not add any search, queue or playback service on top of them. So both
repositories are finished, tested and unused by a running application.

## What exists today

- Fixed directory layout; `encore/domain`, `encore/events`, `encore/config`,
  `encore/services` and `encore/repositories` implemented; `apps/builder/` is a
  working application. `encore/api`, `encore/controllers`, `encore/playback`,
  `encore/search`, `encore/templates` and `encore/static` are still documented
  placeholders, as is `apps/server/`.
- `pyproject.toml` + `uv.lock` + `.python-version`; editable install.
- Ruff, MyPy (strict), pytest with category markers, coverage, pre-commit.
- Five GitHub Actions workflows; issue forms; PR template; labels; CODEOWNERS;
  Dependabot; branch protection definition.
- Documentation scaffolding and ADR-001…ADR-010. ADR-009 supersedes ADR-003's
  access-layer clause (raw `sqlite3` read-only for `library.db`, SQLAlchemy 2.x
  transactions for `runtime.db`); ADR-010 fixes the Builder pipeline, gives the
  Builder sole ownership of the `library.db` schema and sets `paths.music_dir` to
  `/opt/music`. Both are Accepted, and both are implemented.
- Test infrastructure: fixtures, temp SQLite helpers, mock mpv, Party Simulation
  profiles + loader, a performance target registry whose search budget is measured
  rather than promised, and a synthetic media generator that writes real silent
  MP3/FLAC containers with valid tags and embedded covers. 706 tests (715 with the
  slow suites), 91.8% coverage.
- Architecture guardrail tests and `tools/check_links.py`.

## Explicitly not implemented

- Any HTTP endpoint, template, static asset or SSE stream.
- Any mpv integration, queue rule, search implementation or running service.
- **Any application that opens either database.** The Builder writes `library.db`;
  nothing in a running Server reads it yet. Composition is decided — `apps/server/`,
  not `build_core_services` — and recorded in `apps/server/README.md`.
- `EventBus` persistence: events are in-process only (SAPRS 11.5). The Builder
  publishes `BuildCompleted` on a bus it creates and discards, because there is no
  server to subscribe to it yet.
- The installer and `encore-install`.

## Where to put the first feature

Step 7 (search) starts in `encore/search/` as a thin service over
`LibraryStore.search`, which already exists, is already measured (p95 ~8 ms on
1,500 songs) and is already correct. The SQL belongs in
`encore/repositories/library/queries.py` and nowhere else — a second query layer
would be the drift that ADR-009's contract test exists to prevent. Step 9 (queue)
can use `runtime/queue.py` as it stands; `QueueService` is the rule on top of it,
not a new repository.

## Known open questions to resolve before v1

- Final licence choice among MIT/BSD-3/Apache-2.0 (SAPRS 15.15). Bootstrap uses
  MIT; changing it is a one-file, one-ADR decision.
- HTMX and Tailwind distribution in `encore/static/` (vendored vs CDN) — decided
  during milestone 12.
- Administrator credential mechanism (SAPRS 12.6) — decided during milestone 14.
