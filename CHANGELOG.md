# Changelog

All notable changes to Encore are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html), and versions are
tagged `vX.Y.Z` (PBK Chapter 17).

This file is updated by every pull request that changes behaviour, interfaces or
the build and operations tooling, under **Unreleased**. Dependabot's dependency
bumps are the exception — the release notes and the `uv.lock` diff describe them.
The release process moves the section into a dated, bracketed version heading.

## Unreleased

### Added

- MusicBrainz has a configuration home, and the default is *on* (SAPRS 6.6, 6.7, ADR-016, [#57](https://github.com/clydepro/encore/issues/57)).
  `config.yaml` grows a `builder:` section — `musicbrainz: true`, `album_art: true` — and an
  ordinary `encore-builder`, run by nobody in particular, now fills a gap and fetches a cover
  instead of publishing whatever the tags happened to say. Until this the only switch was a
  command-line flag, so every systemd timer, CI job and reader of the documentation built with the
  feature off: the line `MusicBrainz: disabled` appeared on 100% of the appliance's builds without
  anyone having chosen it. The section is modelled in `encore/config/models.py` rather than in the
  Builder, because the Server validates the same file and `extra="forbid"` would turn a correct
  `builder:` key into a machine that does not boot; neither value is read by a request handler, and
  `test_the_builder_reaches_the_network_from_one_module_only` is the new guardrail that keeps it
  that way. Three states on the command line (`--musicbrainz`/`--no-musicbrainz`,
  `--album-art`/`--no-album-art`, `--offline`) mean that *not given* is not `false`, and `--offline`
  — a fact about this afternoon's network rather than about the library — outranks the file.
  Cost, stated beside the switch: one request per second, about two minutes on a 3,000-file library
  with holes in it, two requests per newly identified album and one thereafter.
- `apps/builder/albumart.py`, SAPRS 6.7's second precedence level (ADR-016): an album whose files
  carry no picture of their own asks the Cover Art Archive once, and gets an image only when a
  release matches on *both* names after folding. Not closest, not highest-ranked, not the only
  result — a wrong cover tells a guest something false about where their music is, which is worse
  than a grey tile, and the report counts the refusals (`79 not named exactly`) because that number
  is the policy working. The unit of asking is the album (`Track.group_key`, the same key
  `construction` groups by), so the 463 uncovered albums of the measured corpus cost 926 requests
  rather than 1,989, and an incremental build asks about almost none of them, because a reference
  carried over from #52 *is* a picture. Embedded art still outranks the archive, and the archive's
  bytes go through the same normalization, content addressing, cache and `--max-artwork` bound as
  an embedded image — `generated` still means images written, so a cover that turns out to be
  outranked is never written on its way to being discarded. 13 unit tests, 5 of them in the artwork
  stage's file, and the end-to-end version in `test_issue_57_musicbrainz_configuration.py` builds a
  library where no album is grey.
- The installer (AIG 21 step 15, #32, ADR-014, ADR-015): `encore-install`, a third
  application in `apps/installer/`, shipped as a console script and testable on a
  machine that is not a Raspberry Pi. Five commands — `install`, `upgrade`,
  `remove`, `verify`, `reset-credential` — and four exit codes, because "the
  machine is not ready" and "the machine is fine and Encore is not" are answered
  differently. `install` is idempotent in the strong sense: it observes the host,
  plans only the differences, and an unchanged appliance produces an empty plan
  and the sentence "nothing to do". It never overwrites an existing
  `config.yaml`, and when your file disagrees with a flag it keeps yours and names
  the setting (SAPRS 13.8). `remove` deletes the application and keeps the
  configuration, the databases, the artwork and your music; `--purge-config` and
  `--purge-data` are separate flags because they are separate acts (13.10), and
  both need `--yes`. `--dry-run` prints the plan, and `--root /prefix` rehearses
  the whole install into a self-contained tree that boots — which is how
  `tests/e2e/test_installed_appliance.py` proves that an installed appliance
  serves guests rather than merely existing.
- `GET /qr` and `GET /qr.svg` (SAPRS 9.11): the printable QR page and the code
  alone, rendered server-side as SVG by `segno`. Public, cached for a day, and
  with no `?url=` parameter to make it an appliance that prints phishing codes
  about itself.
- `ENCORE_ADMIN_PASSWORD`, which the documentation had described since ADR-013 and
  nothing had ever read. The appliance now seeds its administrator credential on
  first boot, on the appliance thread, and never overwrites a credential an
  operator has already set (ADR-015). The value reaches the installer only through
  the environment — there is no `--admin-password` flag, because a flag would put
  the appliance's only credential in every user's process list — and it appears in
  no report, log or transcript.
- `ServerConfig.guest_url`, the one expression of where a guest points a browser.
  `/qr`, `/api/v1/info`, the page footer and the installer's report all read it.
- `scripts/encore-install`, a bootstrap wrapper with no logic in it, for the
  command that has to work before anything is installed.
- Installer, packaging, QR and seeding tests: 133 unit (the planner is pure, so
  ordering, idempotence, removal scopes and every acceptance verdict are answered
  without a filesystem), 29 integration against a real staged tree with a scripted
  host, 14 QR route tests that read the URL back out of the SVG rather than trust
  the handler, and 9 end-to-end tests that install an appliance and then boot it.

- The administrative interface (AIG 21 step 14, #28): sign-in and first-run
  password setup, a dashboard, playback controls, queue management, health,
  statistics, a log tail, a configuration summary, library information with a
  reload, password change and logout. Every screen exists twice — a page for a
  browser and a fragment for `HX-Request` — from one context builder, so an
  operator refreshing a region and then navigating to the page cannot see a
  number move. No new SSE region and no new fact: admin regions refresh on
  `encore:change`, which the live script dispatches when a frame it already
  needed to send arrives, so the guest's stream budget is unchanged and nothing
  under `/admin` polls.
- `encore/services/admin_service.py` — the credential and the sessions. Argon2id
  or bcrypt through `passlib`, never a hand-rolled hash; sessions in memory with
  a sliding expiry, so a restart signs the operator out deliberately rather than
  leaving a durable token that survives a disk image restore; five failures per
  window per appliance before a lockout, counted in memory because counting them
  per address would be the guest-tracking ADR-007 forbids. A wrong password and
  an untouched appliance raise the same sentence from the same line, because the
  first thing an unauthenticated caller should not be able to learn is whether
  there is a password to be wrong about. ADR-013 records the choice and its two
  consequences (`Secure` cannot be set on an HTTP appliance; a password rotation
  ends every session, including the one that asked).
- `encore/api/auth.py` — the cookie and the origin check. `HttpOnly`,
  `SameSite=Strict`, `Path=/`, `Max-Age` from `admin.session_minutes`, and no
  `Secure`, because the appliance serves plain HTTP on a LAN and a `Secure`
  cookie would make the interface unreachable rather than safer. There is no
  CSRF token: the flags are the defence, plus a refusal of any state-changing
  request whose `Origin` names somewhere else. A request with no `Origin` is
  allowed, which is what keeps `curl --cookie` in a setup script working.
- `encore/services/statistics_service.py` — counters over the facts the bus
  already carries, subscribed once by the composition root, plus the three
  request-side counters a page can add (`searched`, `artist_viewed`,
  `album_viewed`). A `SongFinished` is the one event that makes a play, and
  whether it counts is `PlaybackOutcome.counts_as_played`'s existing domain rule
  rather than a new one; a report is built in one transaction so the screen and
  `GET /api/v1/stats` cannot describe two different parties. Counter names live
  in `repositories/runtime/statistics.py`'s vocabulary, which means
  `encore/services/__init__.py` deliberately does not import this module eagerly
  — see the note there.
- `encore/repositories/library/connection.py::reopen` and
  `LibraryStore.reload()`: a library reload that keeps the object identity every
  repository and `SearchService` depend on. A live SQLite handle keeps the inode
  it opened and the Builder publishes by renaming a completed file into place
  (ADR-006, ADR-010), so "notice the rebuild" is one reopen of one handle, not a
  second store nobody closed. Verification runs on the new handle before the
  swap: a build interrupted mid-write leaves the running library serving and
  raises a refusal about that file.
- `LogTail` in `encore/services/logging_service.py` — a bounded ring of the
  appliance's own records, flattened on the way in (a retained `LogRecord` holds
  frames, and frames hold locals), redacted through the one `record_fields` rule
  both formatters use, and read by the log page instead of by a shell. The
  journal remains the durable record; the page says so beside the `journalctl`
  command.
- `encore/api/admin_views.py` and `encore/controllers/admin.py`. The controller
  exports two routers rather than one — `router` (the sign-in page) and `secure`
  (everything else, `dependencies=[Depends(depend_admin)]` at router level) — so
  that adding a route under `/admin` next month cannot accidentally be public.
  Administrative actions go through `QueueService` and never to
  `PlaybackService` directly (ADR-011), which is why the guest's screen stays
  coherent when the operator touches the queue.
- Two administrative JSON routes beside their page equivalents:
  `DELETE /api/v1/queue/{item_id}` and `GET /api/v1/stats`, both requiring the
  same session as the pages. `docs/api/README.md` states the difference in
  authentication — a tool may present a cookie, and no guest ever does.
- ADR-013 (The Administrator Credential Lives in `runtime.db` and Sessions Live in
  Memory), with the two rejected alternatives named: a token file under
  `ENCORE_HOME`, which would make a disk image restore a set of live sessions,
  and sessions in `runtime.db`, which would survive the same restore and be
  revocable only by writing to the database the reboot was meant to escape.
- Test support: `encore-live.js` now dispatches one DOM event, `encore:change`,
  on `document.body`, and every administrative region listens for it. Guest
  regions stay `player` and `alerts` — `tests/unit/test_templates.py` still
  asserts that, and a room of phones pays nothing for an operator's dashboard.
- Administrative coverage in five suites: `tests/integration/test_admin_interface.py`
  (every route, every failure path, and an enumeration of the OpenAPI document
  that makes "nothing under `/admin` answers without a session" a property rather
  than a list), `tests/unit/test_admin_service.py`, `tests/unit/test_api_auth.py`,
  `tests/unit/test_statistics_service.py`,
  `tests/unit/test_library_reload.py` (against two real Builder passes over two
  real corpora), plus `tests/e2e/test_admin_journey.py`, four admin page budgets,
  and `tests/party_simulation/test_admin_in_the_room.py`, which runs an operator
  removing, clearing and skipping inside a party and asserts the ledger still
  closes. Regression coverage for #28 is in
  `tests/regression/test_issue_28_administration_interface.py`.

- The HTTP runtime, the guest interface and the live stream (AIG 21 steps 11, 12
  and 13, #25), which is the first thing in this repository that can be started:
  `scripts/run-server.sh`, or `uv run python -m apps.server.main --config …`.
- `encore/api/` — `create_app()`, whose only job is to compose routers, and
  `read()`, which is the whole of the HTTP-to-core boundary: every handler awaits
  the appliance thread through that one function (ADR-012). `errors.py` is a
  single exception→status→code table so a failure says the same kind of word
  wherever it happens; `schemas.py` is the Pydantic wire contract under `/api/v1`;
  `views.py` builds template contexts and nothing else; `rows.py` labels a page of
  songs in two statements instead of `3 × rows`, which is what the 100 ms search
  budget actually depends on.
- The versioned JSON API: search, artists, albums, one song, the queue, Now
  Playing, health and an `/info` that states the appliance's version and limits —
  so a script can do everything a phone can, and OpenAPI is generated from the
  same routes rather than maintained beside them.
- `encore/controllers/` and `encore/templates/` — the HTMX surface: pages for a
  reload and fragments for a swap, one shell with two named regions
  (`swap:player`, `swap:alerts`), and a queue button whose response *is* the new
  Up Next rather than a redirect, because a guest on a bad connection should not
  need a second round trip to find out what happened. Vendored htmx, hand-written
  CSS, nothing fetched from a CDN at runtime (SAPRS 13: the party is offline).
- `encore/services/sse_publisher.py` and `GET /events` — one bounded queue per
  connection, a `snapshot` frame first so a late join is correct without replay,
  facts named by their class so the wire vocabulary cannot drift from
  `EVENT_VOCABULARY`, `progress` once a second, `resync` when a stream stalls, and
  region HTML rendered once per fact for every screen rather than once per screen.
- `apps/server/appliance.py` — the composition root that opens both stores,
  wires the bus, starts the queue before the player so the first fact has a
  listener, and unwinds in reverse on shutdown. `build()` takes a launcher, which
  is what lets a test run the shipped graph against a fake mpv instead of a
  copy of the wiring.
- `encore/utilities/appliance.py::ApplianceThread` — the worker ADR-012 argues
  for, with `worker_ident` for the guardrail that has to say which thread answered.
- `LibraryService` and `HealthService` in `encore/services/`: the read side that
  keeps artwork references inside the cache directory (a path from `library.db` is
  data, not a path to trust), and the aggregate that turns four component answers
  into one honest status without asking mpv a question on the readiness path.
- Test support worth having in one place: `tests/support/http.py` (an ASGI driver
  that can hold an SSE response open — `httpx`'s `ASGITransport` buffers the body
  and cannot), `tests/support/appliances.py` (the real `build()` with a fake
  engine, so no test re-implements composition), and
  `tests/support/latency.py` (nearest-rank percentiles, and the refusal to measure
  under a tracer).
- A first end-to-end suite, `tests/e2e/test_guest_journey.py`, marked `e2e`: one
  long walk through browse → search → queue → redraw → natural end of a track, and
  a reboot that restores the list and stays silent.
- Two performance budgets, closing the last gaps in
  `tests/performance/test_performance_targets.py`: `htmx_navigation` (< 200 ms) and
  `sse_propagation` (< 1 s), measured over the composed app rather than over
  services.
- Regression coverage for #25 in `tests/regression/test_issue_25_http_runtime.py`:
  eleven defects, each demonstrated in the shape it had when found, then the
  behaviour that replaced it.
- `tests/unit/test_templates.py`, which reads the templates as text: every `hx-*`
  attribute is a word htmx knows (`hx-swap="find …"` was not), no template or
  script points off the appliance, and the regions the live layer names are the
  ids the panels declare. A suite with no browser cannot run htmx, so it checks
  the vocabulary instead.

- Search (AIG 21 step 7) in `encore/search/`: a query parser that turns a guest's
  typing into a safe FTS5 `MATCH` expression (word runs, prefix only for tokens of two
  characters or more, eight tokens maximum, SQLite's own operators and quotes stripped
  rather than escaped by hand), typed results that name the field that matched, and
  `SearchService` with `search`/`songs`/`albums`/`artists` over the repository's
  `SearchRead`. The package contains no SQL — every statement stays in
  `repositories/library/queries.py`, which ADR-009 is about — and an index that cannot
  answer raises `SearchUnavailable` rather than reporting an empty library (#23).
- Playback (AIG 21 step 8, ADR-005) in `encore/playback/`: JSON IPC over the mpv socket
  (array-form commands with `request_id` replies, events retained beside them, partial
  lines buffered, garbage replies turned into `MpvGoneError` instead of a traceback),
  process management with a socket-length guard and `quit`-before-`kill` shutdown,
  `MpvPlayer` reading state from mpv's properties rather than assuming it, the SAPRS 7.3
  state machine in `PlaybackService` publishing one `SongFinished` per track whatever
  races with it, and `PlaybackSupervisor` watching the process and bringing it back with
  a bounded ladder of retries (#23).
- The queue service (AIG 21 step 9) in `encore/services/queue_service.py`: strict FIFO
  over `runtime.db` with duplicates allowed (SAPRS 8.3), the ceiling enforced inside the
  transaction that would otherwise exceed it, settlement by outcome (`COMPLETED`,
  `SKIPPED`, `FAILED`, `STOPPED`) with one history row per ended track, immediate start
  when idle and appending when not, wait times the guest can see, and advancement as a
  loop rather than a recursion so a shelf of unreadable files skips past instead of
  nesting event handlers until the bus reports a cycle (#23).
- ADR-011 (The Queue Commands Playback; Playback Notifies the Queue): the one
  justified direct service-to-service call, in one direction, with every backward
  notification on the bus, and the two rules that keep the exception from becoming a
  mesh.
- `PlaybackSupervisor.bind()`, so that wiring a player and its monitor does not mean
  assigning a private attribute from a test.
- Search, queue and playback latency benchmarks in
  `tests/performance/test_runtime_latency.py`, measured over the two real databases
  rather than fakes: enqueue ~11 ms p95 against 50 ms, queue advance ~43 ms p95 (thin
  margin, documented), playback start ~18 ms p95 for Encore's own share of the 250 ms
  budget, which is the part a software change can slow down. `queue_operation` and
  `playback_start` left the registry of unmeasured budgets; only HTMX navigation and
  SSE propagation remain, until milestones 12 and 13 (#23).
- A bounded Party Simulation in
  `tests/party_simulation/test_queue_and_playback.py`: six minutes of the baseline
  profile's rates over the real queue, the real state machine and both databases, with a
  ledger asserting that every request is accounted for. FIFO order under an
  administrator pressing skip, duplicates played twice, two mpv deaths recovered inside
  one night, the ceiling refusing exactly the requests it should, and nothing left
  `Playing` at the end. The 15,000-song, HTTP-fronted driver still lands with
  milestone 16; what is here is the accounting, which does not depend on library size.
- Four architecture guardrails beyond the ones that existed: `encore/playback/` may reach
  neither a database nor another service, `encore/services/` may not import
  `encore.playback` (the coupling is a protocol, ADR-011), and `encore/search/` may
  contain no SQL in any string literal — checked by parsing the literals rather than by
  grepping the file, so prose about `MATCH` does not trip it (#23).
- Repositories (AIG 21 step 5, ADR-009) in `encore/repositories/`, split by
  mutability rather than by table. The library side is raw `sqlite3` over a
  `mode=ro` URI with `PRAGMA query_only=ON`, one module of SQL
  (`library/queries.py`) and one of row mapping, with existence checks that
  cannot be skipped because they share a `SELECT` list with the mappers.
  The runtime side is SQLAlchemy 2.x over WAL, with ORM models, repository
  interfaces taking domain `SongId`/`QueueId` rather than integers, and
  `open_runtime_store()` applying numbered forward-only migrations on open.
  `contract.py` names every table and column once, so the schema cannot drift
  silently between the Builder that writes it and the Server that reads it —
  ADR-010's one named risk, answered with a real SQLite file in
  `tests/integration/test_library_contract.py`.
- The Library Builder (AIG 21 step 6, ADR-010) in `apps/builder/`: a linear
  pipeline of discovery, extraction, normalization, precedence, optional
  MusicBrainz enrichment, artwork, construction, validation and publication;
  the sole author of `library.db`'s schema, including the contentless FTS5
  indexes and the `song_search`/`album_search`/`artist_search` views; per-field
  provenance with the file's original value beside it; a skip ledger that
  aggregates unsupported containers by extension and itemises everything it
  could not read; incremental rebuilds keyed on size, mtime, tag fingerprints
  and checkpoints, so a rebuild after adding an album re-reads only what
  changed and re-encodes no artwork; `encore-builder` as a `console_script`;
  and `BuildCompleted` published on the bus with one number per ledger row.
- Two architecture guardrails #19's acceptance criteria asked for: within the Server,
  only `encore/repositories/` may import `sqlite3` **or** `sqlalchemy` (previously the
  rule checked `sqlite3` in four named packages, which stopped applying anywhere new),
  and `apps/builder/` may import the repository layer's `contract` module for shared
  names but no connection, store or repository. Both were mutation-checked.
- `paths.music_dir` in configuration — the one filesystem path the Builder
  needed that SAPRS 13.3 did not already define, documented as such.
- Synthetic media generation in `tests/support/media.py`: real silent MP3 and
  FLAC containers with valid frames, tags and embedded JPEG covers, with
  durations set in the container where a probe reads them. `aac`/`m4a` decline
  rather than fake it. This replaces the milestone-2 placeholder.
- Shared fixtures `music_tree`, `built_library`, `library_store` and
  `runtime_store`; `tests/performance/test_builder_scale.py` builds a real
  1,500-song library and holds search to the 100 ms budget, measuring ~8 ms p95
  and ~3.7 s of build time on the reference machine (#19).
- `ai/current-phase.md` and `ai/HANDOFF.md`: what a phase was intended to cover,
  what it delivered, what it deliberately left out, and what the next session
  must decide before writing code. `ai/README.md` and the CONTRIBUTING definition
  of done now require them to be rewritten at the end of each phase.
- Core domain model (AIG 21 step 2) in `encore/domain/`: `Artist`, `Album`,
  `Song`, `Artwork`, `MusicFile`, `Metadata`, `QueueItem` and `ComponentHealth`
  as immutable dataclasses; `AudioFormat`, `ArtworkKind`, `QueueItemStatus` and
  `PlaybackState` enums; the SAPRS 7.3 transition table behind
  `can_transition()`; and `NewType` identifiers so an `AlbumId` cannot be passed
  where a `SongId` is wanted. No entity carries guest identity (ADR-007), and no
  module in the package imports anything above it (ADR-001).
- The Event Bus (AIG 21 step 3, ADR-004, SAPRS 11.1–11.4) in `encore/events/`:
  the eight fixed event types, an `Event` base that is frozen, slotted and
  UTC-stamped, and `EventBus` with typed subscribe/unsubscribe, thread-safe
  registration, per-handler failure isolation (SAPRS 11.3), non-blocking
  scheduling of coroutine handlers plus `publish_async()`/`drain()` for shutdown
  (SAPRS 11.4, 11.8), and a cascade guard that raises `EventCycleError` instead
  of exhausting the stack.
- Configuration (AIG 21 step 4, SAPRS Chapter 12) in `encore/config/`: Pydantic
  models for every section of `examples/config.yaml` with `extra="forbid"` and
  `frozen=True`, and `ConfigurationService` which validates at startup and
  reports every problem at once with its location (SAPRS 12.5). The queue
  duplicate rule (SAPRS 8.3) cannot be configured away, and `library.db` and
  `runtime.db` are refused if they name the same file (ADR-006).
- `LoggingService` in `encore/services/`: stdlib structured logging as JSON (one
  object per line) or console text, with sensitive field names withheld by the
  formatter rather than by callers (AEP 16), and `build_core_services()` as the
  one composition root allowed to know how the core is constructed (AEP 9).
- Clock and redaction helpers in `encore/utilities/`; `occurred_at` and
  `enqueued_at` are injectable so timestamps are asserted, not slept on.
- 258 new tests for the above: `tests/unit/test_domain_model.py`, `test_events.py`,
  `test_event_bus.py`, `test_configuration.py`, `test_logging_service.py`,
  `test_core_services.py` and `tests/integration/test_core_foundation.py`.
- Five architecture guardrails that became checkable once the code existed:
  the domain as the innermost layer, no service reachable as a module global,
  no guest identity anywhere in the vocabulary, events frozen and timestamped,
  and the Configuration package holding no write at all (SAPRS 12.3).
- Repository bootstrap (PBK): the fixed directory layout, `pyproject.toml` as
  the single project configuration, `uv`-managed dependencies with a lockfile,
  and an editable development install.
- Quality tooling: Ruff (lint + format), MyPy in strict mode, pytest with
  category markers, Coverage.py and pre-commit hooks covering formatting,
  linting, import order, whitespace, EOF, YAML, TOML, Markdown and secret
  detection.
- CI/CD: `validate.yml`, `test.yml`, `docs.yml`, `security.yml` (CodeQL, OSV
  audit, dependency review, secret scan) and `release.yml` for tagged releases.
- GitHub configuration: CODEOWNERS, Dependabot, branch protection definition,
  the label taxonomy from PBK Chapter 10, seven issue forms and a pull request
  template.
- Documentation scaffolding: README, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY,
  CHANGELOG, LICENSE, the documentation map, and outlines for the user guide,
  administrator guide, developer handbook, API reference, troubleshooting guide
  and release process.
- Initial ADRs 001–008, covering builder/server separation, HTMX over a SPA,
  SQLite storage, the internal Event Bus, mpv playback, the immutable library
  database, the anonymous guest model and the appliance-first philosophy.
- ADR-009 (Split the Storage Access Layer by Mutability): `library.db` is read
  through raw `sqlite3` on a read-only URI with `PRAGMA query_only`, so the
  runtime's inability to write the catalogue is enforced by SQLite rather than by
  review; `runtime.db` keeps SQLAlchemy 2.x transactions, where a torn write is the
  failure that loses a party. Also settles `runtime.db` versioning: numbered,
  forward-only migrations applied at startup, while `library.db` is stamped by the
  Builder and never migrated. Supersedes ADR-003's "SQLAlchemy repositories for
  both" clause.
- ADR-010 (Library Builder Pipeline Shape and Library Ownership): one module per
  SAPRS 6.2 stage, the Builder alone holds the `library.db` DDL, metadata resolved by
  a fixed four-level precedence with the filesystem path demoted to a hint, files
  that cannot be catalogued reported rather than fatal, and a new `paths.music_dir`
  defaulting to `/opt/music`. Its Context records what was measured in the real
  corpus rather than assumed.
- Test infrastructure: shared fixtures, temporary SQLite helpers, a mock mpv
  JSON-IPC double, Party Simulation load profiles with a loader, performance
  target registry, and synthetic media generator hooks (placeholders).
- Architecture guardrail tests that enforce the SAPRS 11.10 rules in CI.
- AI development support materials in `ai/`: the standard prompt header, task
  templates, code-review prompt, feature and regression checklists and context
  bundles derived from the AIG.
- Developer scripts: `bootstrap.sh`, `lint.sh`, `format.sh`, `test.sh`,
  `check.sh`, `run-server.sh`, `run-builder.sh`, `sync-labels.sh`,
  `configure-branch-protection.sh`, `setup-github-repo.sh`.

### Changed

- `LibraryService`'s `LibraryRead` protocol gained `info` and `reload()`, because
  the library page prints provenance and the reload button needs a fact published
  by exactly one caller (ADR-004). `LibraryStore` satisfies it structurally; the
  doubles in `tests/unit/test_library_service.py` and
  `tests/unit/test_library_read.py` grew to match, which is the cost of a protocol
  and the reason it is written down rather than assumed.
- Three guest routes now write one counter each: `/search`, `/artists/{id}` and
  `/albums/{id}` call `stats.searched()`, `artist_viewed()` and `album_viewed()`.
  The increment lives where the request is assembled rather than inside
  `SearchService` or `LibraryService`, so the page, the fragment and the JSON route
  cannot count a view three different ways — and so that a service used by the
  Builder or a unit test stays free of `runtime.db`. The catalogue reads per screen
  are unchanged and asserted as unchanged: an artist page that began writing a
  counter *and* reading twice as much would fail `test_an_artist_screen_is_three_reads`.
- `apps/server/appliance.py` starts `StatisticsService` before the queue starts, so
  the first fact of a party has a listener, and attaches `LogTail` to the `encore`
  logger on configure and removes it on close. `encore/services/__init__.py` does
  not import `statistics_service`: the module needs `StatisticKey` from the runtime
  repository's vocabulary, and pulling SQLAlchemy into every configuration and
  domain test to re-export one class is a trade nobody should have to make twice.
- The OpenAPI description no longer says statistics and queue removal have no
  machine endpoint, and `GET /docs` stays public on purpose: the guest API's shape
  has to be discoverable by someone who has not signed in, and the administrative
  routes in the same document are protected by the session rather than by the
  document's absence.

- `PlaybackOutcome.counts_as_played` means what SAPRS 11.9 needs it to mean: only
  `COMPLETED` counts as a play. It used to be "not `FAILED`", which made a skipped
  track a listen and a stopped one a listen, and would have had the statistics service
  report a party that heard three songs as having heard thirty. The domain type landed
  in phase 1 with the wrong rule and nothing could notice until there was a caller
  (#23).
- The nightly `Test / long-running` job installs mpv and runs `tests/integration --run-slow`,
  which is what makes `tests/integration/test_real_mpv.py` a check rather than a claim: the file
  skips without a binary and is `slow` with one, so before this it ran nowhere in CI and only on
  whoever's machine remembered it. The job is informative, not required, so a runner without the
  package — or an `apt` mirror having a bad night — cannot gate a pull request (#23). The first
  nightly run says the suite passes against mpv 0.37.0 as well as the 0.35.1 it was written
  against, which is a wider version span than any single machine gives it.
- The playback launch line no longer passes `--input-ipc-run=0600`, and socket permissions are
  set by Encore instead: the directory holding the socket is `0700` and the socket is
  `chmod`-ed to `0600` once it appears. That mpv option arrived in 0.36 and an unknown option
  is fatal at parse time, so on mpv 0.35.1 every engine start failed with "mpv exited with
  code 1" before a socket existed — a launch bug every mocked test passed, because a mock
  never parses a command line (`tests/integration/test_real_mpv.py`, found by running a real
  mpv). Encore claims no version floor for this and does not need one: pinning a minimum is
  the installer's decision (SAPRS 7.7). Encore therefore needs no version floor for this, and
  does not claim one: the installer decides that (SAPRS 7.7).
- `MpvLauncher` keeps the channel it created and closes it in `terminate()`, and
  `PlaybackSupervisor` closes the channel a restart replaces. Each recovery had been
  abandoning a connected socket, so an hour of mpv crash loops left an appliance holding one
  descriptor per attempt with nothing in the logs to explain it.
- `CommandChannel` documents why closing is *not* part of the protocol: making it mandatory
  would force every transport double to model a lifecycle it has no opinion about, which is
  the argument ADR-011 makes about `Player` in the first place.
- `tests/support/media.py`'s MP3 containers are now described accurately: a real mpv decodes
  them (length and running position reported), which the suite had been claiming it could
  not. FLAC remains refused, as its container carries no audio frames.
- `MockMpv` now clears `eof-reached` and `pause` when a file is loaded, as mpv does. The
  double had been carrying the previous track's end-of-file onto the next one, which
  made a queue advance appear to walk the entire list in a single tick — and let a
  recovery test pass against a behaviour the appliance cannot have (#23).
- `JsonIpc` and `MpvProcess` take injection seams (`connect=` and `spawn=`) so the byte
  framing and the shutdown ladder are tested directly instead of being reasoned about.
  Production behaviour is unchanged; no seam is a branch.
- The four unmeasured performance budgets are now two, and `scripts/check.sh --slow` runs
  the slow suites untraced so a wall-clock budget is measured rather than distorted; it
  reports coverage from the ordinary gate run instead.
- The core-foundation guardrail in `tests/integration/test_core_foundation.py`
  forbids web frameworks rather than `sqlalchemy`. It had been written in
  milestone 2, when nothing stored anything yet, and ADR-009 makes SQLAlchemy
  the runtime write store by decision: the test was enforcing a rule no document
  states, and would have failed the milestone it was protecting. The check that
  services and domain stay framework-free is unchanged and still passes.
- `encore/domain/` gained `PlaybackOutcome` (the SAPRS 4.5 vocabulary for how a
  track ended, which the playback service had been spelling in strings at each
  call site) and `QueueItem.played_at`, a column SAPRS 4.5 defines that the
  queue repository needs to write. Both are additive; no existing field, enum
  member or transition changed.
- Ruff and mypy now cover `apps/builder/` and `tests/` with the same strictness
  as the rest of the tree, including `TID251` banned-API rules that make a
  network call inside Builder stages other than `musicbrainz.py` a lint failure.

- ADR-003 is marked superseded by ADR-009. SQLite remains the storage engine for
  both databases — only the access layer changed — and the record is kept and
  annotated rather than edited, per `docs/adr/README.md`.
- ADR-009's rationale was amended before implementation, not its decision. Three
  justifications failed measurement: read-only enforcement belongs to the connection
  URI rather than the driver choice, the performance case for raw `sqlite3` is void
  at 15,000 songs (every access path is 10×+ inside the SAPRS 1.8 budgets), and
  SQLAlchemy Core had never been considered as the alternative. The record now rests
  the decision on schema ownership and adds a startup shape check, because both
  access layers create a missing database file and hand back an empty library.
- Architecture guardrail tests now run against real packages rather than
  docstrings: `tests/unit/test_architecture_guardrails.py` imports the core and
  inspects it, so a violation added later fails in CI instead of in review.
- `examples/config.yaml` is now executed by the test suite on every commit. It
  is the document an installer copies, so a key the models do not accept, or a
  default that differs from the model's, is a failing test rather than a
  surprise on someone's first boot.
- Dependabot's `pip` ecosystem runs with `versioning-strategy: lockfile-only` in
  one group, so dependency pull requests change `uv.lock` — what CI actually
  installs — instead of only raising the `>=` floors in `pyproject.toml`. See
  `docs/Developer/Repository-Administration.md` and #11.

- Error responses now carry `Cache-Control: no-store`. SAPRS 10.10's rule is about
  mutable state and has no exception for failures; a cached "queue full" is the
  same fault as a cached queue.
- `Accept: */*` is treated as a tool rather than a browser, and gets JSON. A
  browser names `text/html` because it is about to render it, so the previous
  behaviour — any `Accept` at all yielding the page — made `curl https://…/queue`
  return markup. `docs/api/README.md` states the rule.
- Artwork that cannot be shown is now two answers instead of one: nothing
  depicted is a `200` placeholder, a library row naming a file the cache has lost
  is a `404`. The split is the diagnosis.
- `scripts/test.sh` and `scripts/check.sh` now include `tests/e2e` in the default
  gate. Three seconds of the suite that drives the product the way a guest does is
  cheaper than a milestone in which nobody ran it, and neither script's slow path
  changed.
- `.pre-commit-config.yaml`'s `vendored` exclusion, which existed so a hygiene hook
  could not rewrite an upstream planning document, now also covers
  `encore/static/vendor/htmx/`: a minified third-party file whose sha256 is recorded in
  `NOTICE.md` has to stay byte-identical, and a trailing newline is a different file.
- `pytest` runs with `--timeout=60` (`pytest-timeout`, dev dependency). An SSE
  response that never ends is the feature, so a suite that can hang is a suite
  that reports nothing; the stream helper gives each read its own shorter deadline.
- `tests/performance/test_runtime_latency.py` and
  `tests/performance/test_http_latency.py` share their sampling and percentile code
  in `tests/support/latency.py` rather than each keeping a copy that could disagree
  about what "p95" means.

### Deprecated

- None.

### Removed

- None.

### Fixed

- An offline build stopped making a library worse, and a value that is present and cannot be true
  stopped being believed ([#57](https://github.com/clydepro/encore/issues/57)). Two defects, one
  sentence in the spec that had been implemented in half. SAPRS 6.6's second level is "fill gaps
  **or correct implausible values**", and `enrichment._gaps` asked only whether a field existed: an
  artist of `01` — a rip tool writing the track number into the artist field, 34 of this corpus's
  3,049 files — was present, therefore correct, therefore grouped sixty files under an artist named
  01, and was never so much as asked about; `date: 24` (2 files) was published as 24 and printed as
  24. Plausibility now lives in `normalization.py` beside `is_placeholder`, which is where it can
  act with the network off: a counter artist falls to ADR-010's path level, so the directory the
  file sits in names the artist, and MusicBrainz is the second answer rather than the only one. The
  rule is narrow on purpose — a field that is *nothing but* a small integer, in artist fields only,
  because 21, 25, 30 and 13 are albums and there is no number a title cannot be — and when nothing
  can say better, `_resolve`'s last resort stands: a value is questioned, never discarded, so a band
  named 1776 keeps the name. A counter is corrected but not searched with, because
  `artist:01 AND recording:"Morning"` matches a hundred releases and the client takes the first.
  The second half was worse: the reuse path sat inside the `if options.enrich` gate, so `--offline`
  and `--no-musicbrainz` — the careful options — rewrote every previously enriched value back to the
  tag and let the library quietly decay, which is 6.6's "shall not be degraded to un-enriched data"
  broken by the flag meant to honour it. Asking nothing is now free and forgetting is not: the stage
  runs with a `DisabledClient` and backfills from the previous build. Two fields that had a writer
  or a reader but not both are wired end to end — `albums.musicbrainz_release_id` was never written
  and `PreviousEnrichment.release_id` was never loaded, which is #52's shape, a documented cache
  with no reader — so an album identified once costs one request thereafter. And a date that no rule
  could rescue invalidates the files that rest on it: `NORMALIZATION_RULES_VERSION` goes to 2, so
  the first build after upgrading is a full one. `MusicBrainz:` and `Cover art:` now say off,
  unavailable or asked, in numbers, because a feature that is on by default has to be legible for
  an operator to turn it off for a reason. Regression:
  `tests/regression/test_issue_57_musicbrainz_configuration.py` (10), with 12 new stage tests in
  `tests/unit/test_builder_enrichment.py`, 8 in `test_builder_musicbrainz.py` for the second host,
  and 4 in `test_configuration.py` for the section's defaults and its error sentences.
- `sudo encore-install upgrade` decided it had nothing to install by reading one file,
  so an appliance two merged pull requests behind planned four steps, copied nothing
  and reported success (#55). `_source_matches` compared `pyproject.toml` in the
  checkout against `pyproject.toml` in `/opt/encore/application`; a release that changes
  only Python changes no byte of a manifest, and every release this project has shipped
  since the installer did exactly that. Measured on the hardware where it was found:
  seven files differing under `encore/`, nine under `apps/` — one of them
  `encore/playback/fault.py`, which was not on the appliance at all, because it is
  #48's — and `diff -q` on the two manifests answering `identical`. `install` used the
  same predicate, so re-running the documented first command was equally blind, and no
  line of the acceptance list could have caught it: the question had never been asked
  of the filesystem. That is #46's shape again — a check that is true about the wrong
  subject, printed over fifteen other true things.

  `apps/installer/application.py` is now the one place that says what "the code" means,
  because three modules had to agree about it and only one of them had a list: the
  copier, which had *two* hand-maintained exclusion lists in two glob dialects
  (`shutil.ignore_patterns` and `rsync --exclude=`, 20 entries each, 12 of the rsync
  ones carrying a trailing slash the shutil ones did not — which is the same rule
  written twice, and the reason it drifts); the observer, which had no list because it
  was not looking at the tree; and the planner, which said in its own docstring that
  "the files in `application/` match the ones being offered" while a branch three lines
  below asked about one file. One tuple, both dialects derived from it, one walk that
  digests `size:sha256` per file, and `observe()` answers the sentence the docstring
  claims it answers. The count rides along in `ApplianceState.application_drift` so a
  plan line and a check can name it — `16 differ of 396 files: apps/builder/artwork.py,
  encore/playback/fault.py …` in a dry run — and `verify` grew the acceptance-list line
  #46 never had, WARN on `install`/`upgrade` and FAIL on `verify`, which is #43's
  distinction applied to a new observation rather than a new rule.

  Proving the fix surfaced a second disagreement, this one between the copier and the
  observer rather than inside the observer: rsync's default is to skip a file whose size
  and `mtime` agree, and it compares `mtime` at one-second granularity. A source file
  rewritten in the same second as the last copy is "already transferred" to rsync and a
  difference to a byte comparison, which is #55 arriving from the other direction — a
  report ending in `nothing to do` about a file it did not copy. The copy now passes
  `--checksum`, so the copier and the observer read the same property; `--ignore-times`
  would have been correct and would have transferred all 4 MB every run.
  Regression: `tests/regression/test_issue_55_application_tree.py` (9), which stages a
  tree, copies it for real, changes one `.py` file and no manifest, and asserts both
  that the second run copies and that an unchanged run still prints
  `nothing to do — the machine already matches this request`; and
  `tests/unit/test_installer_application.py` (11) for the exclusion rule itself,
  including the sharp edge of pruning by path component: `build`, `dist` and `site` are
  excluded at every depth, so a future `encore/static/dist/` would install a page with
  no CSS in it and every installer test would stay green. That one is a test now.
- An incremental rebuild — the default, and what you get when you add three albums —
  published a library with **no artwork references in it** (#52). It did not crash,
  it validated, it printed `Artwork 0` and renamed itself into place over a library
  that had 222 images and 1,060 covered songs, so every album tile a guest opened
  afterwards was grey. The mechanism was a cache field with a loader and no reader:
  `Reusable.artwork` is documented ("so an unchanged build writes zero images"),
  filled by `PreviousLibrary` from `music_files.embedded_artwork_sha`, and read by
  nothing — while `_extract` kept only `earlier.probe`, and a restored `ProbeResult`
  carries tags and a duration and no image bytes, because SAPRS 5.6 keeps BLOBs out
  of the artifact. Stage 5 therefore saw 3,049 files with no embedded cover and wrote
  zero references; construction derives every row from what the stage named, so zero
  is not "no new rows", it is no rows. `generate` now takes the previous build's
  references, the checkpoint file carries them across a resumed build (ADR-010's
  stage boundary — a build that dies at minute nine may not come back with fewer
  pictures than it started with), and a reference whose cache file has gone is
  reported rather than published. The report prints both numbers — `0 written to the
  cache (222 in this library)` — because the one it used to print was a true answer
  that the whole failure hid behind.

  Verifying the criterion "a retagged file is re-read for artwork" turned up the same
  shape one layer down, and a worse product of it: `_extract` consulted the previous
  library by *path*, so `Reusable.matches` — ADR-010's key, written out and unit-tested
  and called by no production path — compared nothing, and **an incremental build never
  noticed a file had been retagged**. Correct an artist in a tag editor, rebuild, and the
  library kept what the file used to say, forever, with `reused 3049` printed above it as
  evidence that everything was fine. That is #48's sentence again — a check that is true
  about the wrong subject — and the fix is the call, not a new rule: reuse now requires
  path, size, `mtime_ns` and the normalization-rule version to agree, which is every part
  of the key a `stat()` can answer without the read the cache exists to avoid (the tag
  hash is not one of them; a retag that moved neither byte count nor timestamp is the
  case this cannot see, and it is not a case any tagger produces).
  Regression: `tests/regression/test_issue_52_incremental_artwork_reuse.py` (6), and
  `tests/integration/test_builder_pipeline.py`'s two incremental assertions now state
  both halves of the promise instead of the one that was true.
- A library in which one cover existed in two byte forms could not be built at all,
  and the appliance left standing afterwards showed a grey tile wherever a cover
  should be (#51). The artwork stage keys its cache on the digest of the bytes it
  *wrote* — that is what "content-addressed" means, and `ArtworkOutcome.assets` says
  so — but it deduplicated on the digest of the bytes it *found*. Normalization
  decodes, resizes and re-encodes to one JPEG, which is many-to-one by construction:
  measured over a real 3,049-file library, 224 embedded payloads produce 222 images,
  because one album's cover was tagged twice (a PNG differing from its eight siblings
  at byte 131) and another's differed from its thirteen siblings by one trailing byte.
  Two assets, one cache file, and `artwork.relative_path` is UNIQUE (SAPRS 5.6: the
  path is the artifact), so the second INSERT raised `IntegrityError` inside the build
  transaction: nothing published, the previous library intact, and a traceback —
  `main` caught `(OSError, ValueError)` and a database error is neither, which is the
  second half of the finding. The operator's way round it was `--max-artwork 0`, and a
  library built that way has no references to make, which is what 222 orphaned JPEGs
  above an empty `artwork` table means. Now the stage deduplicates on the address it
  wrote and keeps the payload memo, so a 40-track album still normalizes once; the
  write resolves every digest naming one row rather than dying on the index; an
  unwritable database is exit 3 with the sentence about the previous library; and
  `--max-artwork` caps images, which is what its help text always claimed. Two files
  carrying literally the same bytes were already one entry — the defect needed a
  re-save, not a duplicate, which is why 23 files out of 3,049 were enough to make a
  whole build impossible. Found while verifying the fix, and filed rather than fixed
  here: an *incremental* rebuild — the default — publishes a library with zero artwork
  references, because `Reusable.artwork` is loaded from the previous library, described
  in its docstring, and never read by anything (#52). Until that lands, a rebuild that
  is meant to change the pictures is `encore-builder --full`, which the Administrator
  Guide now says.
  Regression: `tests/regression/test_issue_51_builder_artwork_content_address.py` (7),
  whose first test fails with the field traceback itself — same exception, same line.
- An appliance with no usable audio output refused every file in its library and
  reported itself healthy while doing it (#48). The trigger was configuration —
  `audio.device: default` on a Raspberry Pi 4 names card 0, which is the HDMI port,
  and ALSA will not open an HDMI device with nothing on the other side of it — but
  the cost was everything else. mpv answers that with `end-file reason=error
  file_error="audio output initialization failed"`, which is the shape it uses for a
  file it cannot demux, and Encore read the two as one event: a WARNING per track
  naming whichever innocent file a guest had most recently requested, a queue walking
  itself to empty, and `/health` saying `playback: healthy, mpv running as pid
  503668` — true, and the exact sentence the unit file's own comment exists to
  prevent. Now: `EngineObservation.output_fault` asks whether mpv's own words name
  the audio output or the audio device (phrase, not pinned string — the same
  sentence arrived from three different faults here, and `test_real_mpv.py` re-checks
  it against a real engine refusing a real device); `PlaybackFault` records which of
  the two it was and how many tracks in a row have proved it; the first refusal of a
  run is one ERROR carrying the count instead of forty warnings carrying a filename;
  and `PlaybackSupervisor.health()` reports `degraded` in mpv's words, naming
  `audio.output` and `audio.device`, because liveness and sound are different claims.
  A fault ends when a track plays — not when mpv restarts, since a restart reads the
  same `audio:` and gets the same answer. Per-track behaviour is deliberately
  unchanged: `SongFinished(FAILED)` still names the refused track so the queue moves
  on, and a corrupt file in a 15,000-song library still costs the appliance nothing,
  because a banner that is always red is a banner that gets ignored. The generated
  `config.yaml` and `examples/config.yaml` say where a device name comes from and what
  leaving it at `default` resolves to on this hardware, and the generator now quotes
  `audio.device` (and `playback.mpv_path`, which had the same latent problem) instead
  of interpolating them: `alsa/plughw:CARD=Headphones,DEV=0` is the value that works,
  and one stray space after a colon made it a mapping. `MpvLauncher` logs the output
  and device it handed mpv, once per launch, which is the line that turns an evening
  of reading `audio:` into ten seconds. A device name mpv cannot parse is not an error
  either: it logs `[ao/alsa] Trying the default audio device...` and then fails exactly as
  an absent card fails, so a typo in `audio.device` looks like a sound-card problem and a
  sound-card problem looks like a typo. The value has to come from the list mpv prints for
  itself (`mpv --ao=<output> --audio-device=help`); `aplay -l` prints card numbers, a
  different spelling of the same hardware and not what goes in the file.
- An installed appliance could not start, and the acceptance list said it was fine (#46).
  `Failed at step EXEC … Permission denied` / `status=203/EXEC` is a statement about the
  *interpreter*, not the file named in `ExecStart`: a console script's first line tells the
  kernel which program to run, and `uv` puts that program wherever the account that ran the
  install keeps its downloads. Encore requires Python 3.12 and Debian 12 ships 3.11, so on
  the primary platform every install downloaded one — into `/root/.local/share/uv/python`
  under `sudo`, behind a `drwx------` the service account cannot search. `install` had asked
  `uv sync` for a venv location and nothing else, `_environment` had tested the entry point's
  `0o111` bits (true, and beside the point), and so `verify` printed fifteen green lines over
  a dead service. Three changes: the appliance now installs its own interpreter under the new
  `/opt/encore/python` and *names* it on the `uv sync` command line (`UV_PYTHON_INSTALL_DIR`
  alone is not enough — `uv` also searches a data directory beside the `uv` binary, which #38
  deliberately made the operator's); "can the account in `User=` reach what `ExecStart`
  names" is an observed fact (`interpreter_blocker`, the DAC walk the kernel makes) rather
  than a guess from a mode bit; and an unreachable interpreter counts as drift, so the older
  release's machines are repaired by re-running `install` instead of being told the machine
  already matches. It is a warning on `install`/`upgrade` and a failure on `verify`: a
  finding the requested change can fix is not a prerequisite, and the first cut of this check
  made it one, producing the #43 sentence from the opposite direction — "a prerequisite is
  unmet, so nothing was changed", over the defect that run existed to repair.
- `sudo encore-install install` refused to run on a machine that had systemd and
  did not yet have `encore.service` (#43) — which is every machine, on the first
  install, on the platform SAPRS 13.1 names as primary. `ServiceState.detail` held
  two facts ("there is no service manager" and "here is what systemctl said about
  the unit") and `checks.py` decided the prerequisite from which one happened to be
  non-empty, so the installer declined to install the unit because the unit was not
  installed, and advised `--no-service`, the one answer that leaves an appliance that
  never comes back after a power cut. The manager is now its own observed fact
  (`Machine.systemd_running`, `ApplianceState.systemd_running`) and a missing unit is
  what it always was: a warning on `verify`, and the plan's business on `install`.
  A machine with no systemd still refuses, and now says why. The same report's
  opening line was `nothing to do — the machine already matches this request`, four
  lines above `a prerequisite is unmet`: an empty `Plan` stood for idempotence, for
  `verify`, and for "never planned", so `Plan.planning` names which of the three it
  is and `--json` carries it as `"planning"`. The 13.7 idempotence sentence is
  unchanged, because it was right where it belonged.
- A rehearsal asked this machine to change itself (#44). `systemctl` has no notion
  of a prefix: it speaks to PID 1 about `encore.service`, so `install --root /tmp/x`
  ran `daemon-reload`, `enable` and `start` against the host (where they fail,
  because there is no such unit), and `upgrade --root` and `remove --root` ran
  `stop`/`disable` against a running appliance — silencing the party to change a
  directory nobody was listening to. One predicate now decides it
  (`plan.registers_host_service`, used by all four commands), the staged plan emits
  the registration command as a note instead, and the unit file is still written,
  still staged, still booted by the e2e suite. Staging already declined to run
  `useradd`; this is the same rule applied to the step that was missed, and it was
  unreachable before #43 was fixed, which is why it is its own issue rather than a
  line in that one.
- `encore-install` reported that `uv` was missing on a machine that had it (#38).
  The astral installer writes `~/.local/bin`; `sudo` replaces `PATH` with its
  `secure_path`, which names no home directory; `RealMachine.which` searched only
  `PATH`, so a correctly installed tool was invisible to a correctly invoked
  installer — the two instructions in this repository, followed in order, produce
  this. One function now owns where a host binary can be (`search_path`: your
  `PATH`, then `~/.local/bin`, `~/bin` and `~/.cargo/bin` for both `HOME` and
  `SUDO_USER`'s home), and both the check and the child process ask it, because a
  check that searches one list while the command resolves through another is how
  the next version of this bug becomes a 127 in the middle of a plan. The failure
  line prints the directories it searched. `scripts/encore-install` — bash,
  before any Python can run — keeps its own three-line copy of the same list, with
  a comment naming the owner. Getting Started now says where that install
  puts `uv`, because the fault lived between two documents rather than inside
  either one.
- A rehearsal run as root aborted at the first action (#41). `--root` skips
  `useradd`, so `chown root:encore` names an account the plan deliberately never
  created, and `Executor._ownership` judged a failed `chown` by `os.geteuid()`
  alone: right about the process, wrong about the machine. The decision now comes
  from the observed state (`ApplianceState.root` and `.user_exists`), which is how
  that module was meant to work, and a rehearsal reports ownership as left unset and
  exits 0. On a real appliance, where the account exists, a failed `chown` is still
  fatal — fixing this must not mean a machine that cannot read its own
  configuration shrugs. Neither half was covered before: the lifecycle suite never
  ran as root, and the recorder answered every `chown` with success. It now refuses
  one naming an account the machine does not have, which turned 19 tests red and is
  the reason they are worth more afterwards.
- The built wheel contained no server. `[tool.setuptools] packages` listed `encore`
  and `apps.builder` and omitted `apps.server` and the two
  `encore.repositories.*` subpackages, so an installed appliance had nothing to
  start — while every test passed, because pytest imports from the checkout
  (#32). `tests/unit/test_packaging.py` now builds the wheel and opens it, and
  compares its contents against the source tree rather than against a list.
- Templates and static files were not package data, so a wheel that did contain
  the server would have started cleanly and answered 500 on the first page view
  (#32).
- `/api/v1/info` reported `http://{domain}/` whatever port the appliance was
  listening on, which on the shipped default of 8080 is a URL that resolves and
  refuses the connection (#32, ADR-015).

- The dashboard answered its own transport buttons with a URL segment. The routes in
  `encore/controllers/admin.py` write their feedback sentences inline, all except the
  five-word `?command=` one, whose table (`_sentence()`) was written, documented and
  never called — so a panel that had just been paused read "pause." beside a
  `Paused` badge. The coverage report found it (a function with a docstring that no
  line ever entered); `tests/integration/test_admin_interface.py` now asserts the five
  sentences and asserts that none of them is the path segment echoed back.
- A failure that the appliance rendered as a fragment was thrown away by the
  client. htmx's default `responseHandling` ends in `{code: "[45]..", swap:
  false}`, so every 4xx and 5xx body — a guest's "the queue holds 8 songs", an
  operator's "that rebuild is not a database" — was computed correctly, sent
  correctly and never swapped into the region that asked for it. The symptom was
  not an error message, it was a button that appeared to do nothing. The live
  bridge now allows the swap for an HTML error response (`htmx:beforeSwap`), and
  the failure answers with `HX-Reswap: innerHTML` so it goes *into* the region
  rather than over it — an administrative panel swaps itself `outerHTML` on
  purpose, and replacing one with an error paragraph would have left it with no
  `id` and no refresh trigger, visible exactly once. A `401` additionally carries
  `HX-Redirect` to the sign-in page with `?next=`, because a panel cannot sign
  itself in and htmx acts on that header before it consults its own table. Both halves are pinned, including the vendored default
  the rule depends on, so an upgrade that changes it fails a test rather than
  silently restoring the dead interface.
- A locked appliance answered a sign-in with an error page and no way back.
  `RateLimitError` is a sibling of `AuthenticationError` — right for the service,
  invisible to a handler that caught only the parent — so `_reason()`'s "try
  again in N minutes" was unreachable code, the operator got `error.html` where
  the login form should have been, and the wait appeared nowhere a client could
  obey. The login route now names both cases; the page says the wait above the
  password field, and the number is in a `Retry-After` header beside
  `details.retry_after_seconds` for anything that reads headers rather than
  markup (`docs/api/README.md`,
  `tests/regression/test_issue_28_administration_interface.py`).
- A file at `paths.library_db` that is not a database at all — an interrupted
  publish, a truncated copy, someone's `cp` into the wrong directory — raised
  `sqlite3.DatabaseError` from inside the shape check, because SQLite reports a bad
  header lazily and the check *is* the first query. It is now a
  `LibraryContractError` naming the file and the Builder, which matters at two
  moments: at startup the log line says what to do, and at the library page's
  reload button the answer is a 503 about that file rather than a 500 about this
  process. The previous library stays current either way (it always did —
  verification happens on the new handle before the swap); what changed is that the
  refusal is now a sentence an operator can act on
  (`tests/regression/test_issue_28_administration_interface.py`).
- `LibraryStore.reload()` updated search's availability by building a new
  `LibrarySearch`, which left `SearchService` — handed the old object at
  composition — answering from the verdict a library that was no longer current had
  made. The verdict is now a property of the file refreshed in place
  (`set_availability`), so a rebuild that added or lost an FTS index is noticed by
  the service that was already running
  (`tests/unit/test_library_reload.py::test_search_follows_the_file_rather_than_the_process`).

- Five more defects in the launch path, four of them found by running the finished playback
  stack against a real mpv rather than against `MockMpv`, and fixed where a mock could not hide
  them again: the unsupported launch option and the leaked socket described above, and a load
  that mpv had accepted but not yet completed being read as an ended track — which answered a
  guest's request with `SongFinished(COMPLETED)` and let the queue walk itself to empty against
  a silent appliance. `Loading` is now a state that can last, bounded by a five-second grace
  after which a file mpv never opened is reported as `FAILED`
  (`tests/regression/test_issue_23_search_playback_queue.py`).
  The fix that grace made necessary was the fifth: a file mpv refuses and a file that finishes
  between two polls read identically — idle, no filename, no position — so the appliance had no
  way to tell a corrupt track from a 400 ms one, and whichever rule it picked was wrong half the
  time. On the real engine the symptom was a test that passed three runs in four. Encore now
  reads mpv's own `end-file` verdict, which the IPC client had been collecting and discarding:
  `"error"` ends the wait with `FAILED` in one tick instead of five seconds, `"eof"` is counted
  as a listen and announced even when the announcement is late, and a build that says nothing
  falls back to the grace as before. Polling still decides everything in progress; this is the
  one fact the engine is asked about rather than watched for (ADR-005).
  The fourth came from reading what that run put in front of us rather than from mpv itself: a
  `paths.temp_dir` that cannot be created or tightened raised a bare `OSError` out of
  `MpvLauncher.launch()`, which the supervisor does not catch — so a misconfigured directory
  was a traceback from a timer where the other launch failures are a named cause and a backoff.
  It is `MpvGoneError` now, like the failure to spawn (`tests/unit/test_playback_ipc.py`).
- Eight defects found while building AIG steps 7–9, each now guarded by
  `tests/regression/test_issue_23_search_playback_queue.py`, which carries the table of
  symptoms. Four of them were only findable by running the new code: an item was marked
  `PLAYING` only *after* a successful load, so one unreadable file stopped the party
  forever; `STOPPED` auto-advanced, which replayed the song an administrator had just
  stopped; the queue handed a track to an engine sitting in `ERROR`, because the only
  check was "not playing"; and a death detected by the monitor restarted mpv without
  telling the service, so the queue saw a `PlaybackRecovered` over a track still marked
  `Playing` and started nothing — a jukebox that survives its own crash and then plays
  silence. The others are a `health()` that trusted a stale channel object over the
  process, an unguarded second wait in `MpvProcess.stop()` that turned a slow shutdown
  into a traceback, a malformed IPC reply leaking `JSONDecodeError` through a playback
  call, and the `counts_as_played` rule described above (#23).
- Ten silent failures in the persistence and Library Builder work were found and
  fixed while building it, each now guarded by
  `tests/regression/test_issue_19_persistence_and_library_builder.py`. They are
  listed together because none of them raised: FTS5 `MATCH` against a table alias
  returned nothing, a contentless index returned `title=None`, an ID3 `TDRC` frame
  is not a `str` so every ID3 date vanished, an all-time statistics row keyed on a
  `NULL` day duplicated once per reset, `.git/objects` was indexed as music, DAW
  package internals were reported as unplayable audio, and a relative
  `--music-dir` built a library that its own validation then refused to publish
  (#19).
- `encore-builder` is now a real command. The Administrator guide, ADR-009, ADR-010
  and three repository error messages all told operators to run it, and no console
  script existed (#19).
- `scripts/run-builder.sh` called `--source`/`--output`, flags the Builder's CLI
  never had, and changed directory before resolving a relative argument (#19).
- `scripts/sync-labels.sh` and `scripts/setup-github-repo.sh` failed with
  "unknown flag: --repo" on the GitHub CLI shipped by Debian (2.23), where
  `gh api` has no such flag; repository selection now travels in the API path
  and `gh label`'s own `--repo` is used where it is supported.
- Branch protection required `CodeQL` and `Dependabot`, checks that no workflow
  produces, so every pull request would have waited on them forever. The
  required contexts are the real `Workflow / job` names, and
  `tests/unit/test_ci_contract.py` keeps them in sync with the workflows.
- `Security / vulnerabilities` failed on every run because of an unsupported
  `uv audit --strict` flag.
- `Security / codeql` failed because GitHub rejects SARIF from this workflow
  while the code-scanning **default setup** is enabled. The setup script now
  turns the default setup off, and the workflow analyses `python` and `actions`
  so nothing is lost by doing so.
- CI actions were referenced by moving major tags that some authors never
  publish, which failed jobs at "Set up job"; every action is now pinned to a
  full version tag and a test rejects moving references.
- Branch protection required approvals that nobody could give. A single
  maintainer cannot approve their own pull request, so "one approving review",
  "code owner review" and "approval of the most recent push" made every pull
  request unmergeable — an admin merge does not bypass review requirements. The
  review gates are off until a second person with write access joins (see
  `docs/Developer/Repository-Administration.md`); all fourteen status checks
  still gate every merge, for admins included.

- `scripts/setup-github-repo.sh --dry-run` failed immediately: it passed
  `--repo` to `tools/sync_labels.py`, which takes the repository positionally.
  `scripts/sync-labels.sh` also dropped its arguments, so `--prune` and
  `--dry-run` did nothing.
- Two setup steps were listed as "UI only" when they do have endpoints: private
  vulnerability reporting (`PUT …/private-vulnerability-reporting`) and the
  merge/branch options (`PATCH …/`). The script now applies six steps and the
  remaining checklist is genuinely short.

- `tests/regression/` was collected by nothing. Neither `scripts/check.sh` nor
  any workflow named the directory, so the suite SAPRS 14.14 mandates only ran at
  release time, where `--run-slow` happens to gather everything. It runs on every
  commit now, and its README's promise that CI checks the `Regression: #<issue>`
  citation is finally true.
- `scripts/setup-github-repo.sh --verify` reported GitHub's own `accessibility`
  label as taxonomy drift on every run; it joins the defaults that are never
  pruned and never complained about.
- The first tagged release would have died in `Release / verify`: the workflow
  greps `CHANGELOG.md` for `## [X.Y.Z]`, and the file's only release heading was
  written without brackets (#15). Headings are now Keep a Changelog format, and
  `tests/regression/test_issue_15_changelog_heading_gate.py` holds both halves to
  that shape.

- `docs/Developer/Continuous-Integration.md` still described the pre-matrix world:
  `Test / arm64 smoke` as a required check and CodeQL as one check named `CodeQL`.
  The table now states the fourteen real contexts, and a test keeps it equal to
  `.github/branch_protection.json` so documentation and settings cannot drift
  apart again.
- Release process wording aligned with what CI actually enforces: `## Unreleased`
  (no brackets) becomes `## [X.Y.Z] - YYYY-MM-DD`, and Dependabot bumps are
  recorded by the release notes rather than a line each.

### Security

- The open Dependabot alerts, closed (#39): Pillow 12.3.0 → 13.0.0, certifi
  2026.7.22 → 2026.10.23, and — development-only, in a contributor's `uv sync` and
  never in `/opt/encore/venv` — virtualenv 21.14.5 → 21.15.1 and filelock
  4.0.12 → 4.2.4. Lockfile bumps, as `.github/dependabot.yml`'s
  `lockfile-only` strategy intends: `pillow>=10.4` in `pyproject.toml` stays a
  statement of the minimum that works, and 13.0.0 is what resolves against it.
  The floor is unchanged deliberately — Pillow 12 still encodes artwork
  correctly, and raising a floor to close an alert says the older version is
  unsafe to depend on, which is a different claim from the one GHSA makes.
  `apps/builder/artwork.py` uses `open`, `load`, `size`, `resize`, `convert` and
  `save`, all stable across the major boundary, and the suite asserts decoded
  dimensions rather than bytes, so nothing here rests on a JPEG encoder holding
  still.
- The administrative interface, and the credential model behind it (ADR-013). One
  appliance-wide password, hashed with Argon2id through `passlib` and stored in
  `runtime.db` — never in the image, never in a template, never in a log, and the
  test suite asserts the plaintext is not in the database's bytes. Sessions are
  random 256-bit tokens held in memory; a restart signs everyone out, which is the
  intended behaviour and is written down rather than discovered. `ENCORE_ADMIN_PASSWORD`
  is read once at startup to seed the credential for unattended installs and is
  then forgotten — a password that stayed in the environment would be readable from
  `/proc` for the life of a machine that outlives the event.
- Cross-site request defence without a CSRF token: `SameSite=Strict` plus `HttpOnly`
  plus a refusal of a state-changing request whose `Origin` names another site. The
  combination is stated in the ADR with the one thing it does not do — it does not
  protect a GET, which is why no administrative GET changes anything.
- Sign-in failure is rate limited per appliance (five failures, then a window) and
  deliberately not per address: an appliance has one LAN, ADR-007 forbids keeping
  notes about who was connecting, and a per-address table would be both the
  tracking and the false comfort. The lockout resets on a successful sign-in, so an
  operator who mistypes four times and then gets in is not punished later. A locked
  appliance answers `429` with the remaining seconds in `Retry-After`, and it
  refuses before hashing anything, so neither the status nor the timing says whether
  the last guess was close.
- `?next=`, which is a suggestion from someone else's page, is filtered before it is
  obeyed. The filter refuses a scheme, an authority, a backslash (which some browsers
  fold into a slash before they look for a host) and any character a URL parser would
  delete before deciding what it is parsing, so a percent-decoded tab cannot turn
  `/host` into `//host` on the way out; anything refused lands on the dashboard
  instead. CodeQL flagged the redirect as a sink, which it is: the reason it is not a
  vulnerability is now written where the code is, tested twice (once with the hostile
  shapes, once with the paths that must survive intact), and recorded in
  [SECURITY.md](SECURITY.md) along with the alert's dismissal. A filter that had only
  ever refused `http://` and `//` would have passed every test written against it,
  which is what the second test is for.
- Template autoescaping is asserted rather than assumed. `tests/unit/test_templates.py`
  scans every template for `| safe` and `{% autoescape false %}` and renders a hostile
  value through the same `Renderer.response` method the requests use, so the answer to
  "does the admin surface escape?" is a test that fails rather than a reading of the
  environment's configuration.
- The log tail the administrative interface serves is redacted by the same
  `record_fields` rule that writes the journal, so a password in a debug line is
  hidden from both readers or from neither. Nothing was added to the sensitive-name
  list to make that true — it already carries `password`, `credential`, `session`,
  `token` and `cookie` — which is the point of one rule with one owner rather than a
  second list maintained beside the first.

- Secret scanning, dependency vulnerability auditing and CodeQL analysis wired
  into CI; reporting process documented in `SECURITY.md`.

## [0.0.0] - 2026-10-07

### Added

- Planning documents committed: SAPRS, AI Implementation Guide, AI Engineering
  Playbook and Project Bootstrap Kit.
- Repository initialised.
