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

### Deprecated

- None.

### Removed

- None.

### Fixed

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

- Secret scanning, dependency vulnerability auditing and CodeQL analysis wired
  into CI; reporting process documented in `SECURITY.md`.

## [0.0.0] - 2026-10-07

### Added

- Planning documents committed: SAPRS, AI Implementation Guide, AI Engineering
  Playbook and Project Bootstrap Kit.
- Repository initialised.
