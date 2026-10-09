# Current Phase

**Phase 2 — persistence and the Library Builder (AIG steps 5 and 6): implemented on
`feat/19-persistence-and-library-builder`, not yet merged.** Phase 1 — Core Foundation —
is merged ([#20](https://github.com/clydepro/encore/pull/20), `eda2d22`).

Read this page first in a new session, then [`context/milestones.md`](context/milestones.md)
for what exists and what is scaffolding. Neither is authoritative: precedence is
task request → SAPRS → AIG → ADRs → AEP (AEP 2).

## Delivered

| AIG 21 | Delivered as | Spec |
| ------ | ------------ | ---- |
| 5 — Repositories | `encore/repositories/` | ADR-009, SAPRS Ch. 5, 10 |
| 6 — Library Builder | `apps/builder/` | ADR-010, SAPRS Ch. 6 |

Both databases now exist and are opened by both applications that name them:

- **`library.db`** is written only by the Builder. The Server opens it through raw
  `sqlite3` on a `mode=ro` URI with `PRAGMA query_only=ON`, one module of SQL
  (`library/queries.py`), one module of row mapping, and existence checks that
  share a `SELECT` list with the mappers so they cannot be skipped.
- **`runtime.db`** is SQLAlchemy 2.x over WAL, with ORM models, numbered
  forward-only migrations applied on open, and repositories that take domain
  `SongId`/`QueueId` rather than integers.
- **`contract.py`** names every table and column once. That is what makes the two
  halves of ADR-009 safe: the Builder owns the DDL, the Server owns the reads, and
  neither can drift from the other silently.

The Builder is a stage-per-module pipeline (`apps/builder/pipeline.py`): discovery,
extraction, normalization, precedence, optional MusicBrainz enrichment, artwork,
construction, validation, publication. `encore-builder` is a real console script,
with the four documented exit codes, a human report by default and `--json` for
automation, and incremental rebuilds keyed on `(path, size, mtime_ns, tag hash,
rules version)`.

## Verification state

- `scripts/check.sh` — all gates pass: ruff (lint + format), yamllint,
  markdownlint, secret scan, mypy strict, **705 tests**, 91.8% coverage. That is
  the unit + integration + regression total, which is what CI runs.
- `scripts/check.sh --slow` adds the performance and Party Simulation suites:
  714 pass, 5 skips. Every skip is a benchmark whose subsystem does not exist yet
  and names the milestone that writes it — four of the five. The search budget is
  measured now, so it is no longer in that list.
- `uv run python tools/check_links.py` — clean.
- **`tests/integration/test_library_contract.py` is the load-bearing test of this
  phase.** It executes every statement in `library/queries.py` against a
  `library.db` produced by a real Builder run, so the read side cannot drift from
  `apps/builder/schema.py` in either direction. ADR-009 named schema drift as its
  one real risk; this is the mitigation, and it is automatic rather than a review
  habit.
- `tests/integration/test_builder_pipeline.py` is the milestone gate: the whole
  pipeline over generated media, including publication, the event, and the CLI.
- **Measured, not assumed** (`tests/performance/test_builder_scale.py`, `--run-slow`):
  1,500 songs built in ~3.7 s; song search p95 ~8 ms against a 100 ms budget; an
  incremental rebuild of the same corpus in ~1.7 s with every song reused and no
  artwork rewritten.
- **Pressed against the real corpus**: discovery over `/opt/music` scans 3,299
  entries in ~0.3 s, classifies 3,049 as supported and 250 as unsupported
  (188 `.m4p`, 41 `.wma`, 10 `.mid`, 7 `.m4v`, 2 `.aif`, 2 `.m4r`), and reports
  nothing unreadable. ADR-010's 231 counted only `.m4p`/`.wma`/`.aif`; the larger
  number is what the aggregate shows once every unplayable extension counts. What
  it deliberately excludes is paperwork — images, lyric files, playlists, and the
  extensionless `projectData`/`PkgInfo` inside GarageBand bundles — because a skip
  line that mixes those in describes the filesystem rather than the decision.
- The mutation check that matters this phase: repoint one column name in
  `contract.py` and the library contract test fails; delete a table from
  `schema.py` and it fails; make `publish()` rename before it validates and the
  pipeline integration test fails.

## Explicitly not in this phase

Search *service* (`encore/search/`), playback, the queue service, FastAPI, HTMX,
SSE, the admin interface, and the installer. `apps/server/` is still a
placeholder: nothing opens the two stores in a running application yet, because
that composition belongs to the server and pulling it into
`build_core_services` would have made the core foundation depend on storage.

`encore/repositories/` is therefore finished but **unused at runtime**. The first
consumer arrives with milestone 9 (queue) and 11 (FastAPI).

## Next

**AIG steps 7–9: search, playback, queue.** No issues exist for them — `gh issue list`
shows #19 as the only issue ever opened besides two tooling ones, so per
CONTRIBUTING §2 the next session opens its own before branching. Do not assume one
is waiting.

What is already decided that the next phase can lean on:

- Search is `LibraryStore.search` over contentless FTS5 with `bm25` ranking,
  already measured at 8 ms p95 on 1,500 songs. `encore/search/` should be a thin
  service over it, not a second query layer — the SQL lives in `queries.py` and
  belongs there.
- The queue's persistence exists and is tested (`runtime/queue.py`, 1-based
  positions, `played_at` written on status change). `QueueService` is the business
  rule on top of it, and `QueueItem`/`PlaybackOutcome` are already in the domain.
- `BuildCompleted` is published by the Builder and is the event a running Server
  subscribes to for `LibraryReloaded`.

Two decisions, taken at the end of this phase so the next session does not meet
them as a surprise:

1. **The stores are composed in `apps/server/`, not in `build_core_services`.**
   The server builds both, hands them to services, and owns their lifetime. Core
   composition stays storage-free on purpose: opening a database needs files that
   config and domain tests have no business creating, and a composition root that
   cannot start without a filesystem is not a foundation. Recorded in
   `apps/server/README.md`, where the code will actually go.
2. **Playback tests skip when they need decodable audio; they do not fake it and
   they do not commit fixtures.** `tests/support/media.py` writes valid MP3/FLAC
   *containers* with silent audio — enough for metadata, discovery, dedupe and
   search, not enough for decoding. A test that asserted gaplessness on a file mpv
   cannot decode would be testing the mock. So: mpv's IPC contract, the Supervisor's
   state machine and recovery are tested against `MockMpv`, which is honest because
   it is the same JSON mpv reads; anything that depends on *audio coming out*
   skips with a reason. This does not need an ADR — it changes no clone content.
   Revisit if the skips start hiding real regressions, which is the failure mode
   where skipping becomes wrong.

## Loose ends

- **This PR closes #19**, deliberately — the phase implements everything the issue
  asks for. It was closed in error once before, when PR #20 put a closing keyword in
  a "Related issue" line; the difference now is intent. Steps 7–9 have no issues, so
  the next session opens its own before branching.
- **`/opt/music` cleanup is still the operator's**: 250 unplayable files (DRM `.m4p`
  mostly) and 15 unidentifiable ones. The Builder handles all of them correctly now
  and says so in the report; moving them is a choice about what the report should
  look like, not a bug.
- **`aac`/`m4a` generation still raises** `SyntheticMediaUnavailableError`, asserted
  rather than skipped so the gap stays visible. Closing it means either committing an
  encoder dependency or hand-writing ADTS frames; until then the M4A extraction path
  is only exercised by real files. Playback's own skip policy is separate and
  decided — see `docs/Developer/Testing.md`.
- **The `slots` + `super()` trap** still lacks its own regression file (AEP 13 wants
  one per issue); the guard is in `tests/unit/test_events.py`.
- **Human review is still a norm, not a gate** — `required_approving_review_count`
  is 0 because GitHub will not let a sole maintainer approve their own PR.
