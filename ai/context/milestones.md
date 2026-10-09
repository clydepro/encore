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
| 5 | Repositories | Not started ← **next** |
| 6 | Library Builder | Not started |
| 7 | Search | Not started |
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
why nothing before them touched SQLite.

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

## What exists today

- Fixed directory layout; `encore/domain`, `encore/events`, `encore/config` and
  `encore/services` implemented, `encore/api`, `encore/controllers`,
  `encore/playback`, `encore/repositories`, `encore/search`, `encore/templates`
  and `encore/static` still documented placeholders.
- `pyproject.toml` + `uv.lock` + `.python-version`; editable install.
- Ruff, MyPy (strict), pytest with category markers, coverage, pre-commit.
- Five GitHub Actions workflows; issue forms; PR template; labels; CODEOWNERS;
  Dependabot; branch protection definition.
- Documentation scaffolding and ADR-001…ADR-010. ADR-009 supersedes ADR-003's
  access-layer clause (raw `sqlite3` read-only for `library.db`, SQLAlchemy 2.x
  transactions for `runtime.db`); ADR-010 fixes the Builder pipeline, gives the
  Builder sole ownership of the `library.db` schema and sets `paths.music_dir` to
  `/opt/music`. Both are Accepted and neither is implemented.
- Test infrastructure: fixtures, temp SQLite helpers, mock mpv, Party Simulation
  profiles + loader, performance target registry, synthetic media hooks
  (placeholders). 342 tests (344 with the slow suites), 99% coverage of `encore/`.
- Architecture guardrail tests and `tools/check_links.py`.

## Explicitly not implemented

- Any HTTP endpoint, template, static asset or SSE stream.
- Any database schema, migration or query — including the two databases whose
  paths configuration already validates. Repositories are step 5, and
  `paths.music_dir` (ADR-010) is not in the model yet.
- Any mpv integration, queue rule, search implementation or running service.
- `EventBus` persistence: events are in-process only (SAPRS 11.5), and nothing
  survives a restart yet.
- The installer and `encore-install`.

## Where to put the first feature

Step 5 (repositories) starts in `encore/repositories/`, split by ADR-009: a
read-only `sqlite3` side for `library.db` and a SQLAlchemy side for `runtime.db`.
The `library.db` DDL belongs to the Builder and nowhere else (ADR-010), and
`runtime.db` gains numbered forward-only migrations under the repository package.
The domain entities already name the columns they need; `tests/support/sqlite.py`
has the temporary-database helpers, and `ai/HANDOFF.md` carries the measurements
from the real corpus at `/opt/music` that ADR-010's rules were written against.

## Known open questions to resolve before v1

- Final licence choice among MIT/BSD-3/Apache-2.0 (SAPRS 15.15). Bootstrap uses
  MIT; changing it is a one-file, one-ADR decision.
- HTMX and Tailwind distribution in `encore/static/` (vendored vs CDN) — decided
  during milestone 12.
- Administrator credential mechanism (SAPRS 12.6) — decided during milestone 14.
