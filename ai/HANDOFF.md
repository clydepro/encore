# Handoff — after AIG steps 5 and 6 (persistence and the Library Builder)

For whoever picks this up next. Read with
[`current-phase.md`](current-phase.md) (what was done) and
[`context/milestones.md`](context/milestones.md) (what exists). Precedence is
unchanged: task request → SAPRS → AIG → ADRs → AEP (AEP 2). This page is a pointer,
not an authority, and it will be wrong faster than the SAPRS is.

State as of this writing: **Phase 1 is merged; Phase 2 is on
`feat/19-persistence-and-library-builder`, unmerged.** Both databases exist, the
Builder builds them, 703 tests pass and `scripts/check.sh` is green (712 with the
slow suites). Nothing opens
either store from a running application yet — that is the next phase's job, and it
is the reason `encore/repositories/` is finished but has no production caller.

**[Issue #19](https://github.com/clydepro/encore/issues/19) is open, and this is the
branch that closes it.** It was closed in error once already (PR #20 put a closing
keyword in a "Related issue" line and GitHub obeyed it). Do not repeat that: decide
whether this PR closes #19, say so in one unambiguous sentence, and if it does not,
comment on #19 with what remains.

## Start here

The four packages this phase added, in the order that makes sense to read them:

1. **`encore/repositories/contract.py`** — five minutes, and it is the hinge of the
   whole phase. Every table and column name, once, for both databases. Read it
   before `library/queries.py` and the SQL stops being mysterious.
2. **`encore/repositories/library/`** — the read side. `connection.py` (the only
   place a library connection is made: `mode=ro`, `query_only`, the shape check),
   then `queries.py` (all the SQL, nothing else), then `mappers.py`, then the
   repositories. `store.py` is what a caller uses.
3. **`encore/repositories/runtime/`** — the write side, and deliberately the
   opposite shape: `models.py` (ORM), `migrations.py` (numbered, forward-only,
   applied on open), `session.py` (WAL, transactional), then the repositories.
4. **`apps/builder/pipeline.py`** — the only file worth reading start-to-finish in
   the Builder. Every stage is one module behind it; the pipeline's job is ordering,
   accounting and the event.

Then `tests/integration/test_library_contract.py`, which is what keeps 2 and 4 from
drifting apart.

## Things that will surprise you

- **`sqlite3.connect()` and `create_engine()` both create a missing file.** A typo
  in `paths.library_db` boots an appliance with zero songs and no error. ADR-009
  named this as the one silent failure mode in the area, so both stores check shape
  on open and raise `StoreNotFoundError`/`LibraryContractError`. If you add a third
  way to open either database, it needs the same check; `open_library()` and
  `open_runtime_store()` exist so that nobody writes a fourth `connect()`.
- **Contentless FTS5 has two traps, both silent.** `x MATCH ?` is read by SQLite as
  *a column named `x`* — the table name must appear on the left, not an alias. And
  you cannot `SELECT title FROM song_search`: a contentless index stores no
  columns, so a hit must be resolved by `rowid` through the view. Both were shipped
  bugs here, caught only when a real database was queried; `MATCH` against an empty
  index returns nothing and does not complain.
- **`UNIQUE` treats `NULL` as distinct.** `runtime_statistics` needed both per-day
  and all-time rows in one table with a `UNIQUE(metric, day)`; an all-time row keyed
  on `day = NULL` duplicated once per reset. All-time totals use the empty string.
- **`mutagen` returns objects that are not `str`.** An ID3 `TDRC` frame gives you an
  `ID3TimeStamp`. A reader that only understands `str` drops every ID3 date and
  nothing fails. See `extraction.py._rendered` — it rejects anything that stringifies
  to `<...>` on purpose, because `<mutagen.id3.ID3TimeStamp object at 0x…>` in an
  artist field is worse than absent.
- **SQLite `mtime` is second-granular and `shutil.copy2` preserves it**, so "size +
  mtime" is not an identity. The cache key includes a tag hash and the
  normalization-rule version for exactly this reason (ADR-010).
- **The Builder ignores hidden trees and non-audio files rather than reporting them
  as unsupported.** `scanned == songs + skipped` is an invariant the report prints,
  and a `.DS_Store` counted as "music Encore cannot play" makes one line of the
  report describe the filesystem instead of the decision.
- **`enrich=False` by default is a measured decision, not a placeholder.** At
  MusicBrainz's 1 request/second, filling gaps on 3,000 files is a 50-minute build.
  Enrichment also does not query for a missing *date* alone — too common to be worth
  a request each; the date comes along when another gap triggers the lookup.
- **Domain types now include `PlaybackOutcome` and `QueueItem.played_at`.** The
  first is SAPRS 4.5's vocabulary for how a track ended, which the playback service
  had been spelling in strings at each call site. Playback (step 8) should record
  history through it, not invent a parallel enum.

## Guardrails that exist now

- `tests/integration/test_library_contract.py` — every library statement executes
  against a real built database. Fails on drift in either direction.
- `tests/unit/test_architecture_guardrails.py` — imports the real packages and
  asserts AIG 4. `encore/repositories/**` may not import FastAPI; services and
  domain may not import either store's driver.
- `tests/integration/test_core_foundation.py` — importing the core pulls in no web
  framework. **This was narrowed this phase**: it previously banned `sqlalchemy`
  outright, written in milestone 2 when nothing stored anything, and it would have
  failed the milestone it was protecting. ADR-009 makes SQLAlchemy the runtime
  write store by decision. Do not re-widen it without amending the ADR.
- Builder stages may not open a socket: `TID251`-style review is manual here, and
  `musicbrainz.py` is the only module with a URL in it. The tests inject an opener
  and never hit the network (SAPRS 14.4).

## What is deliberately not here

No HTTP, no player, no templates, no installer, no `encore/search/` service, and
**no caller of either store**. If a task looks like it needs one of those, it is a
later step; this phase made storage real and correct, which is what steps 7–13 are
built on.

## Claims made that later code must keep true

- **`library.db` is never written by the Server.** Checked by construction: the only
  function that opens it uses `mode=ro` + `PRAGMA query_only=ON`, and the Builder is
  the only writer of DDL. A new code path that opens `library.db` some other way is
  a bug, and nothing will catch it but review — the guardrail is one function, not a
  test per call site.
- **Repositories contain no business rules.** Now that both sides are real, the
  check that catches a rule landing in a repository is `test_architecture_guardrails`
  plus the absence of any import of `encore.domain` services in `repositories/**`.
- **Runtime timestamps are aware UTC.** `runtime/types.py.UTCDateTime` is the only
  datetime column type; if you add a timestamp to a model, use it. A naive value
  read back at 03:00 during a DST change is the failure mode ADR-009 warns about.
- **Migrations are forward-only and numbered.** `open_runtime_store()` applies them;
  a model change without a migration fails `test_runtime_migrations`, and a
  migration that changes an existing revision fails too.
- **The Builder never modifies user music.** Nothing in `apps/builder/` opens a media
  file for writing; that is SAPRS 6.8 and it is worth keeping as a review question
  because no test can check it without a real filesystem watch.

## Known loose ends

- **No issues exist for AIG steps 7–9.** #19 is one of only three issues ever
  opened. Open yours before branching (CONTRIBUTING §2).
- **Where the stores get composed is undecided.** The honest answer is `apps/server/`
  builds them and hands them to services; `build_core_services` deliberately does not,
  because opening a database requires files that pure config/domain tests have no
  business needing. Make it a decision in the server milestone, not by accident in a
  fixture.
- **`aac`/`m4a` synthetic media still raises** `SyntheticMediaUnavailableError`. It is
  asserted rather than skipped (`test_media_generator.py`), so nothing silently stops
  covering it, but the M4A path in `extraction.py` is therefore only tested against
  real files. The corpus is ~96% MP3 and the rest FLAC/M4A, so the gap is real but
  narrow. Closing it needs an encoder dependency or hand-written ADTS frames.
- **Playback tests will hit the synthetic-media limit soon.** Silent-but-valid
  containers are not decodable audio. Decide early whether to commit small licensed
  fixtures, generate them with `ffmpeg` in CI, or skip; if the answer is "commit
  files", that changes what a clone contains and wants an ADR.
- **`tests/regression/` is one file per issue**, so this phase's eight fixed defects
  all live in `test_issue_19_persistence_and_library_builder.py`. Their value is the
  docstrings saying what each symptom was; a future session that fixes three bugs on
  one PR will be tempted to make three files. Resist that — the convention is per
  *issue*.
- **Coverage of `apps/builder/` plus `encore/repositories/` is 91.9%**, with
  `musicbrainz.py` at 95% after the HTTP client got a fake opener. SAPRS 14.16 wants
  90% for the builder, which is met; the misses are mostly `extraction.py` container
  edge cases and the M4A note above.
- **Human review is still a norm, not a gate**: `required_approving_review_count` is
  0 because GitHub will not let a sole maintainer approve their own PR.
