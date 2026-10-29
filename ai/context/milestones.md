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
| 7 | Search | **Done — `encore/search/`** |
| 8 | Playback | **Done — `encore/playback/`** |
| 9 | Queue | **Done — `encore/services/queue_service.py`** |
| 10 | Runtime database | **Done — migrations in step 5** |
| 11 | FastAPI | **Done — `encore/api/`, `apps/server/`** |
| 12 | HTMX | **Done — `encore/controllers/`, `encore/templates/`, `encore/static/`** |
| 13 | SSE | **Done — `encore/services/sse_publisher.py`, `/events`** |
| 14 | Administrative interface | **Done — `encore/api/auth.py`, `controllers/admin.py`, `templates/admin/`, `services/{admin,statistics}_service.py`** |
| 15 | Installer | **Done — `apps/installer/`, `scripts/encore-install`, ADR-014/015 (#32, #38, #41, #43, #44, #46)** |
| 16 | Party Simulation | Not started |
| 17 | Documentation | Ongoing |

The steps are ordered, not independent: 2–4 are the ground that 5 and 6 stand
on. Step 5 is where `library.db` and `runtime.db` schemas first exist, which is
why nothing before them touched SQLite — and step 6 is where `library.db`'s DDL
actually lives, which is why 5 and 6 were done together rather than in sequence.
Steps 7–9 were done together for the mirror-image reason: a queue that cannot
start a song is untestable, a player that nothing commands is unexercised, and
the search service is what makes a guest's request exist at all.

PBK's ten-milestone view groups the same work; both are listed in the README
roadmap. Steps 2, 3 and 4 are PBK milestone 2 (“Core Framework”), and are
declared complete against its checklist in the section below.

## What step 14 delivered

- `encore/services/admin_service.py` — one appliance-wide credential (Argon2id via passlib,
  hashed, stored in `runtime.db`'s `admin_state` table) and the sessions in front of it: random
  32-byte tokens in memory, sliding expiry from `admin.session_minutes`, a five-failure window,
  a first-run `setup()` that refuses to run twice, and a `change_password()` that revokes every
  session. No HTTP, no SQL, no mpv: it is given a store and a clock (`tests/support/…`-style
  protocol seams, satisfied structurally by `RuntimeStore`).
- `encore/api/auth.py` — the cookie (`HttpOnly`, `SameSite=Strict`, `Path=/`, no `Secure`, because
  the appliance is HTTP on a LAN), the origin check on state-changing requests, and
  `depend_admin`, the dependency every protected route reaches. There is no CSRF token; ADR-013
  argues the flags and the origin check are the same defence with less to keep in sync.
- `encore/controllers/admin.py`, `encore/api/admin_views.py`, `encore/templates/admin/` — eleven
  screens and their actions. The controller exports two routers: `router` (the door) and `secure`
  (everything else, protected at router level), which is the shape that makes "I forgot to guard
  the new route" unconstructible. Every screen exists twice — `pages/x.html` and `panels/x.html` —
  rendered from one context builder, so a refresh and a navigation cannot disagree.
- `encore/services/statistics_service.py` — counters subscribed to the facts the bus already
  carries, plus three request-side counters called from `api/views.py`. `report()` is one
  transaction, which is what lets the page and `GET /api/v1/stats` print the same numbers.
- `LogTail` in `encore/services/logging_service.py` — a bounded ring of flattened, redacted
  records, attached to the `encore` logger by the composition root and removed on close. The
  journal stays the durable record; this is the part an operator can read from a phone.
- Library reload: `LibraryConnection.reopen()`, `LibraryStore.reload()`,
  `LibrarySearch.set_availability()` and `LibraryService.reload()`. Verification on the new
  handle, swap only on success, identity preserved — so a rebuild published mid-party reaches the
  running appliance through one click and one fact (`LibraryReloaded`), and `library.db` is still
  never written.
- `encore/static/js/encore-live.js` gained one line's worth of behaviour: it dispatches
  `encore:change` on `document.body` when a frame says the appliance moved. Administrative regions
  trigger on it. There is no second SSE stream, no new frame name, no new region, and nothing
  under `/admin` polls — all four are asserted.

Decisions taken here that later steps must honour:

- **Sessions are not durable.** A restart signs the operator out. A token in `runtime.db` would
  survive a disk-image restore, which is the failure ADR-013 was written to avoid.
- **One credential, and one failure counter.** Sign-in failures are counted per appliance, never
  per address: an appliance on a LAN has one address that matters, and a table of failing addresses
  would be the guest-tracking ADR-007 forbids.
- **Administrative actions are queue mutations.** The console calls `QueueService` and never
  `PlaybackService` directly, so ADR-011's single mutator holds for the operator too and the
  guest's screen stays coherent when someone at the laptop touches the list.
- **A page performs its action before it decides what markup to return.** `?panel=` names the
  region that asked; a browser without JavaScript gets the same command and a 303. The branch order
  is in the regression file because getting it wrong produced a dashboard that said "paused" above
  a track that was playing.
- **No new fact types.** `EVENT_VOCABULARY` is still the same eight, and pausing is still not one
  of them; a phase that wants a guest's screen to react to a pause needs an ADR, not a publisher.

## What steps 11–13 delivered

- `encore/api/` — `create_app()` and nothing else at the top: `app.py` composes
  the FastAPI app from routers, `deps.py` is the one seam through which every
  handler reaches the appliance thread (ADR-012), `errors.py` is the single
  exception→status→code table, `html.py` is the Jinja environment and the
  cache/no-store policy, `sse.py` is `/events`, `views.py` builds the contexts the
  templates render, `rows.py` is the batched label read that keeps a page at two
  statements, and `schemas.py` is the JSON wire contract, Pydantic, versioned
  under `/api/v1`.
- `encore/controllers/` — the guest pages (`browse.py`), the HTMX fragments
  (`panels.py`) and the button (`queue.py`). They contain no SQL, no business
  logic and no mpv, and a guardrail test says so by importing them.
- `encore/templates/`, `encore/static/` — a shell with two swap regions,
  `swap:player` and `swap:alerts`, htmx vendored at
  `static/vendor/htmx/htmx.min.js`, `encore-live.js` as the SSE bridge, and
  hand-written CSS in place of Tailwind (a deviation, recorded in ADR-012's
  consequences: an offline appliance cannot fetch a CDN, and the alternative was
  vendoring a build step).
- `encore/services/sse_publisher.py` — one queue per connection, the `snapshot`
  that makes a late join correct, facts named by their class, region frames
  rendered once per fact, and a `resync` instead of a replay. Its 32-frame limit
  is what turns a phone in a lift into a reconnect rather than a memory leak.
- `apps/server/` — the composition root and the CLI. `build()` is the graph;
  every integration, e2e and performance test that needs a server calls it, so
  the shipped wiring is the wiring under test.
- `encore/utilities/appliance.py` — `ApplianceThread`, the worker ADR-012 argues
  for, plus `call_async`, which is the whole of the HTTP→core boundary. It reports
  the identity of the thread that did the work, because a guardrail that cannot
  say who ran is a comment.

Decisions taken here that later steps must honour:

- A handler reaches a service through `read()`/`call_async` and nothing else. A
  route that opens a session or an mpv socket is not wrong-looking; it is wrong.
- Queue insertion is not idempotent (SAPRS 10.4), so no route may deduplicate it,
  and a test asserts that a double press adds two.
- The SSE vocabulary is `EVENT_VOCABULARY`, framed by class name. A ninth fact
  arrives unnamed-but-forwarded with no regions redrawn, which is the state a
  reviewer should notice, not one a browser should discover.
- Artwork has two miss answers on purpose: nothing depicted is a placeholder, a
  file the cache lost is a 404. Collapsing them hides an install fault.
- The confirmation a guest gets derives from the queue item's status, not from
  whether the appliance happened to be idle.

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

## What steps 7–9 delivered

- `encore/search/` — `parse()` turns a guest's typing into a safe FTS5 `MATCH`
  expression, `SearchService` answers songs/albums/artists over the repository's
  `SearchRead`, and `SearchUnavailable` distinguishes "no matches" from "search cannot
  happen" (SAPRS 11.2). No SQL in the package; a guardrail test parses the string
  literals to keep it that way.
- `encore/playback/` — `ipc.py` (JSON IPC: framing, `request_id` replies, events beside
  them, `MpvProcess` with a socket-length guard and `quit`-before-`kill`), `player.py`
  (`MpvPlayer`, state read from mpv properties, not assumed), `service.py` (SAPRS 7.3's
  machine, one `SongFinished` per track, `tick()` watching for eof), `supervisor.py`
  (SAPRS 7.2/7.6: watches the process, restarts with a bounded ladder, publishes
  `PlaybackRecovered`, binds to the service it reports deaths to), `transition.py`
  (gapless and crossfade *policy*, documented as what v1 actually does).
- `encore/services/queue_service.py` — strict FIFO, duplicates allowed, the ceiling
  enforced inside the transaction, settlement by `FinishedReason` (`COMPLETED`→`FINISHED`,
  `SKIPPED`→`SKIPPED`, `FAILED`→`REMOVED`, `STOPPED`→`PENDING` at the head), history one
  row per ended track, wait times, and advancement as a bounded loop rather than a
  recursion. It subscribes to `SongFinished` and `PlaybackRecovered` and commands the
  player through a local `Player` protocol: ADR-011.
- `tests/integration/test_queue_playback_and_search.py` — the three together over both
  real databases, including a crash mid-set, a file that cannot be opened, a song that
  left the library, and a process restart.
- `tests/integration/test_real_mpv.py` — the same stack against the actual mpv binary, for
  the ways a synchronous mock flatters the design (launch options, the asynchronous load
  window, who closes the socket, and the one question the polled properties cannot answer).
- `tests/performance/test_runtime_latency.py` and a bounded party in
  `tests/party_simulation/test_queue_and_playback.py`.

Two things this phase deliberately did **not** do: it did not add a composition root for
them (`container.py` still builds config and logging only — the wiring belongs to
`apps/server/`, milestone 11), and it did not add a ninth event. `SongFinished` grew a
`completion` field instead, which ADR-011 explains.

## What exists today

- Fixed directory layout; `encore/domain`, `encore/events`, `encore/config`,
  `encore/services`, `encore/repositories`, `encore/search` and `encore/playback`
  implemented; `apps/builder/` is a working application. `encore/api`,
  `encore/controllers`, `encore/templates` and `encore/static` are still documented
  placeholders, as is `apps/server/`.
- `pyproject.toml` + `uv.lock` + `.python-version`; editable install.
- Ruff, MyPy (strict), pytest with category markers, coverage, pre-commit.
- Five GitHub Actions workflows; issue forms; PR template; labels; CODEOWNERS;
  Dependabot; branch protection definition.
- Documentation scaffolding and ADR-001…ADR-011. ADR-011 is the queue→playback seam:
  one direct command, in one direction, everything backward through the bus.
  ADR-009 supersedes ADR-003's
  access-layer clause (raw `sqlite3` read-only for `library.db`, SQLAlchemy 2.x
  transactions for `runtime.db`); ADR-010 fixes the Builder pipeline, gives the
  Builder sole ownership of the `library.db` schema and sets `paths.music_dir` to
  `/opt/music`. Both are Accepted, and both are implemented.
- Test infrastructure: fixtures, temp SQLite helpers, mock mpv, Party Simulation
  profiles + loader, a performance target registry with all five of its budgets measured rather
  than promised, an ASGI driver that can hold an SSE response open, and a synthetic media generator
  that writes real silent MP3/FLAC containers with valid tags and embedded covers. 1547 tests in the
  standard gate, 1599 with the slow suites.
- Architecture guardrail tests and `tools/check_links.py`.

## Explicitly not implemented

- **An install verified on a Raspberry Pi.** `encore-install` is implemented and tested against a
  real staged filesystem (ADR-014), including the systemd commands, which are recorded rather than
  run. What no test proves is sound reaching a speaker on real hardware — ALSA device names, USB
  DACs, the mpv socket under the `encore` account on a machine with a `13.5` group in it. That is
  the bring-up checklist, and it is milestone 15's remaining work rather than its design.
- **An install run as root, until late.** The checklist above was written from a laptop, and the
  first time the documented command ran in a root shell it failed twice — `uv` "missing" from a
  `PATH` that sudo had trimmed (#38), then a `chown` to an account a rehearsal had deliberately not
  created (#41). Both are fixed and both are covered, but the lesson is not about PATH: *a fake that
  agrees with whatever it is asked is not a check*, and `RecordingRunner` answered every `chown`
  with success while the real command failed. Everything the suite asserts about a machine it
  cannot see is provisional in exactly this way.
- **The full Party Simulation driver** (milestone 16). The shape of a party with an operator in it
  is simulated and its ledger asserted; 15,000 songs and hundreds of HTTP guests are not.

- Any HTTP endpoint, template, static asset or SSE stream.
- Any *running* service. `encore/playback/` has driven a real mpv (0.35.1, `--ao=null`) in
  `tests/integration/test_real_mpv.py`, which skips where the binary is absent, and nothing
  else. What no test proves is that sound reaches a speaker — ALSA device names, USB DACs,
  the socket under the `encore` service account — which is milestone 15's bring-up.
- **Any application that opens either database.** The Builder writes `library.db`;
  nothing in a running Server reads it yet. Composition is decided — `apps/server/`,
  not `build_core_services` — and recorded in `apps/server/README.md`.
- `EventBus` persistence: events are in-process only (SAPRS 11.5). The Builder
  publishes `BuildCompleted` on a bus it creates and discards, because there is no
  server to subscribe to it yet.

## Where a new feature goes now

Steps 5–14 are behind us, so the question this section used to answer ("where does the first
feature go?") has a settled shape: an HTTP feature starts in `encore/controllers/` (routes) and
`encore/api/` (dependencies, errors, contexts), reaches state through `deps.read()`, and puts its
business rules in `encore/services/`. The composition root is `apps/server/appliance.py::build()`,
which is no longer unwritten: it opens both stores, wires the bus, the queue, the player, the
supervisor, health, statistics and the publisher, starts them in the order the facts require and
unwinds in reverse. `tests/support/appliances.py::composed_appliance()` is the same graph with a
fake engine, and it is what an integration or e2e test should use rather than a hand-wired copy —
that is the reason `test_server_app.py` and `test_admin_interface.py` exist in that order.

Two rules from that history are worth restating because they constrain where a *new* service can
put work: `EventBus` is synchronous and one thread owns SQLite and mpv, so anything a request
starts must publish and return rather than play; and a service that talks to another service does
it through the bus, with ADR-011's queue→playback command as the one justified exception.

## Known open questions to resolve before v1

- Final licence choice among MIT/BSD-3/Apache-2.0 (SAPRS 15.15). Bootstrap uses
  MIT; changing it is a one-file, one-ADR decision.
- HTMX and Tailwind distribution in `encore/static/` (vendored vs CDN) — decided
  during milestone 12.
- Administrator credential mechanism (SAPRS 12.6) — **decided in milestone 14**: the hash lives in
  `runtime.db`, the sessions live in memory, ADR-013 records both and why.
