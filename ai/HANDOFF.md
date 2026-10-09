# Handoff — before AIG steps 5 and 6 (persistence and the Library Builder)

For whoever picks this up next. Read with
[`current-phase.md`](current-phase.md) (what was done) and
[`context/milestones.md`](context/milestones.md) (what exists). Precedence is
unchanged: task request → SAPRS → AIG → ADRs → AEP (AEP 2). This page is a
pointer, not an authority, and it will be wrong faster than the SAPRS is.

State as of this writing: **Phase 1 is merged to `main`** (`eda2d22`, via
[#20](https://github.com/clydepro/encore/pull/20)), CI green, 342 tests, 99%
coverage of `encore/`. **No database, schema, repository or Builder code exists
yet.** The design questions that blocked steps 5 and 6 are decided in
[ADR-009](../docs/adr/ADR-009-split-the-storage-access-layer-by-mutability.md) and
[ADR-010](../docs/adr/ADR-010-library-builder-pipeline-shape-and-library-ownership.md);
neither has been implemented, and ADR-010's Context section carries measurements
that only make sense to read before you design, not after.

## Start here

Work on **[issue #19](https://github.com/clydepro/encore/issues/19)** — the two
schemas and `encore/repositories/` — with the **Library Builder (step 6) in the
same arc**, because the `library.db` schema is the Builder's output contract and
ADR-010 gives the Builder sole ownership of it. #19's text predates both ADRs;
where they disagree, the ADRs are newer and ADR-009 supersedes ADR-003, which #19
cites.

Sequence that will not cause rework:

1. `library.db` DDL in the Builder, plus the artwork cache layout (SAPRS 5.3–5.6).
2. `runtime.db` schema and its numbered migrations (ADR-009). ADR-003's
   SQLAlchemy-for-both clause no longer applies.
3. `encore/repositories/` — read side on raw `sqlite3` with `mode=ro`, write side on
   SQLAlchemy. Neither a `Session` nor a raw row crosses the boundary.
4. A startup shape check on both stores: file exists, expected tables present, song
   count logged. `sqlite3.connect()` and `create_engine()` both *create* a missing
   file and return a working empty library, so a typo in `paths.library_db` boots an
   appliance with zero songs and no error. ADR-009 requires this; nothing else in the
   design catches it, and it is the only silent failure mode in this area.
5. The Builder pipeline stage-by-stage (SAPRS 6.2), ending in validation and
   atomic publication (5.8, 6.10, 6.11).
6. `paths.music_dir`, default `/opt/music`, **added to three files in one commit** —
   `PathsConfig`, `examples/config.yaml`, the Administrator guide. It does not exist
   yet, and two of the three fails CI: `extra="forbid"` rejects an unknown key in the
   example, and a test executes `examples/config.yaml` against the model.

Branch it, per `CONTRIBUTING.md` §7. `main` rejects direct pushes.

## The corpus you are building against

`/opt/music` is the shipping library — 3,049 playable files in 20 GB, world-readable,
no permission problems, and the target for step 6's acceptance run. Sized for
15,000 without significant performance impact, which is roughly four times what is
there now, so the FTS5 and index choices in step 5 are made against a corpus four
times larger than the one you can measure.

The findings that change code, all measured rather than assumed:

- **`albumartist` is on only 19% of files**, so the album-grouping field SAPRS 6.4
  names must be derived or repaired, not read.
- **Path is not a reliable artist source.** `country_5/`, `fun_songs/`, `gospel/`
  and `Unknown_Artist/` hold 44 files whose directory is a mood bucket, not an
  artist (Deanna Carter, Elvis Presley, The Muppets — all in tags only), while
  `AC_DC/`, `Amy_Winehouse/` and 330 others are genuine artist trees. Of the 15
  files that have no tag artist and so would fall back to the path, the directory
  name supplies a usable artist in **zero** of them and the filename in 8. ADR-010
  therefore puts the path at precedence level 3 and the filename at level 4.
- **Tags are not automatically the truth either**: `Uncle Kraker - Drift Away.mp3`
  has the correct artist in tags and a promo `No Stranger To Shame-ADVANCE` album;
  one 53-minute whole-album file is tagged `artist="AlbumWrap - Kenny Chesney"`.
- **15 playable files cannot be catalogued** — no usable artist, 7 with no title
  either. `Metadata` raises on both (SAPRS 6.5), so the Builder must catch and
  report, not crash mid-run. These 15 and the 231 unplayable files below are the
  operator's cleanup, decided deliberately *not* to be pruned to make the Builder's
  job easier: 6.8 forbids the Builder deleting user music and a Builder that only
  works on clean data fails the first time someone adds a mix CD.
- **231 files are real music the appliance cannot play**: 188 `.m4p` (DRM — a
  permanent no, not a codec to add later), 41 `.wma`, 2 `.aif`. Report them in
  aggregate; a 263-line skip list trains the operator to ignore the report.
- **Artwork**: embedded on 98% of M4A, 23% of MP3, 0% of FLAC. The artwork stage
  must treat "nothing to write" as normal (6.7).

Use `tests/support/media.py` for fixtures and keep the above as real-data
integration cases; the unit tests cannot represent "the tag disagrees with the
path" honestly.

## Ten minutes of orientation

```bash
scripts/bootstrap.sh          # once
scripts/check.sh              # the gate CI runs; ~8 s
uv run pytest tests/unit/test_event_bus.py -q --no-cov   # the most alive part
```

The four packages to read, in the order that makes sense:

1. `encore/domain/` — SAPRS Ch. 4 as frozen dataclasses. Fifteen minutes, and it
   is the vocabulary everything else speaks. `media.py` holds `AudioFormat` and its
   `for_path`, which is the whole of discovery's format rule; `metadata.py` holds the
   required-field rule that the 15 unidentifiable files will hit.
2. `encore/events/` — `base.py` first (three properties of a fact), then `bus.py`.
   `BuildCompleted` and `LibraryReloaded` already exist as types; steps 5 and 6 are
   the first code allowed to publish them.
3. `encore/config/` — `models.py` mirrors `examples/config.yaml` one class per
   section; `service.py` is the loader and the diagnostic. `PathsConfig` is where
   `music_dir` goes, and its `_databases_are_distinct` validator is the pattern for
   path rules.
4. `encore/services/container.py` — 97 lines, and the only place that knows how
   the core is built. Repositories get constructed here, not inside a service.

## Things that will surprise you

- **`@dataclass(slots=True)` and zero-argument `super()` do not mix.** A subclass
  calling `super().__post_init__()` raises `TypeError` on first *publication*, not
  at import. Every event uses `Event.__post_init__(self)`. See
  `encore/events/base.py`.
- **A dataclass field named `logging` shadows the module** for the rest of that
  class body, so `CoreServices.logger` is annotated `Logger`, not
  `logging.Logger`. See the comment in `encore/services/container.py`.
- **`publish()` does not await coroutine handlers.** It schedules them, so a slow
  subscriber cannot delay playback (SAPRS 11.4). Tests that need completion call
  `await bus.drain()`, or use `publish_async()`. A coroutine handler published with
  no running loop is *skipped and logged*, not run late.
- **`EventCycleError` escapes while every other handler error is swallowed.** That
  asymmetry is deliberate: SAPRS 11.3 protects a publication from a subscriber,
  not from the wiring being wrong. A cycle would otherwise exhaust the stack.
- **`allow_duplicates: false` fails startup.** SAPRS 8.3 is a rule, not a
  preference, and the configuration layer refuses to look like a knob for it
  (SAPRS 12.3). The same guard sits on the model, so `model_copy(update=…)` cannot
  bypass it — `EncoreConfig` revalidates instances.
- **An empty section (`audio:` with nothing under it) means "defaults"**, not
  "disabled". Normalized in the loader, with the reason in a comment.
- **Secrets are withheld by the log formatter**, not by callers, because a service
  forgetting to redact is the normal case. `record_fields()` is the single answer
  to "what did we log?". An `extra` key that collides with the four envelope keys
  is kept under an `extra_` prefix rather than dropped or allowed to overwrite.
- **`assert` in production code is not a validation strategy.** Every invariant in
  this phase raises `ValueError` from `__post_init__` or a Pydantic validator. The
  same reasoning applies to schema constraints: prefer `NOT NULL` and `CHECK` in
  DDL over a Python check that a direct INSERT can bypass.
- **CodeQL's merge protection blocks on alerts, not on checks.** A dismissed
  false positive looks like a swallowed finding, so dismissals get a paragraph in
  the PR body — see #20's note on `py/clear-text-logging-sensitive-data`.

## What is deliberately not here

No HTTP, no database, no player, no templates, no installer. If a task looks like
it needs one of those, it is a later step and the honest answer is that this phase
prepared the vocabulary for it and nothing else.

## Claims made in Phase 1 that later code must keep true

- The domain imports nothing from `encore/` except `encore.utilities`. Checked.
- No service instance is reachable as a module global. Checked.
- No entity or event carries guest identity. Checked by field name, so
  `source_ip` fails CI.
- Configuration cannot write anything. Checked by AST scan.
- `library.db` and `runtime.db` are different files. Checked at load; steps 5 and 6
  must make it mean something by actually opening one read-only — ADR-009 is the
  mechanism (`mode=ro` plus `PRAGMA query_only`), and a repository that can write to
  the library is a bug the driver should now refuse.
- Timestamps are timezone-aware UTC. Checked at construction; the queue table must
  not let a naive value into a row, so store ISO 8601 with offset and assert on
  read.

The first four are tests in `tests/unit/test_architecture_guardrails.py`. The
last two are only half-enforced until the databases exist — that is this next
phase's acceptance criteria, not a gap in Phase 1.

## Known loose ends

- No tracking issue existed for steps 2–4, so the commit, PR #20 and the CHANGELOG
  entry are the record of what was done. #19 was the first issue in the sequence
  `CONTRIBUTING.md` describes — and it is **currently closed in error**, so a session
  looking for step 5 by browsing open issues will not find it. PR #20's description
  opened its "Related issue" line with a closing keyword pointing at #19 and then
  negated it in prose; GitHub read the keyword. Reopen it before starting step 5:
  `gh issue reopen 19`. Both of its open questions were answered on 2026-10-09 in
  ADR-009 and ADR-010, and the comment on the issue records that plus two corrections
  to its own text (an ADR *was* required, and the shape-check requirement is new).
- The `slots`/`super()` trap deserves a file in `tests/regression/` per AEP 13,
  but that suite is one file per issue number and this was found while writing
  code, not reported. The guard is in `tests/unit/test_events.py` and labelled as
  such; open a `chore` issue and move it if tidiness matters.
- `examples/config.yaml` is asserted equal to the model defaults on every commit.
  If you change a default, that file changes in the same commit — the test tells
  you, the docs will not.
- **Music cleanup is pending with the operator, not in code**: the 231 unplayable
  files and the 15 unidentifiable ones listed above. Steps 5 and 6 should be written
  to handle all of them correctly *anyway*, because the report has to be honest for
  the next library, not just this one.
- Human review is still a norm rather than a gate. GitHub will not let a sole
  maintainer approve their own pull request, so `required_approving_review_count`
  stays 0 and the reviewer checklist stays unticked until a second developer
  arrives. Revisit then, with these two ADRs as the first thing that reviewer
  should read.
