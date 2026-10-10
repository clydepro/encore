# Handoff — after AIG steps 7, 8 and 9 (search, playback, queue)

For whoever picks this up next. Read with
[`current-phase.md`](current-phase.md) (what was done) and
[`context/milestones.md`](context/milestones.md) (what exists). Precedence is unchanged:
task request → SAPRS → AIG → ADRs → AEP (AEP 2). This page is a pointer, not an authority,
and it will be wrong faster than the SAPRS is.

State as of this writing: **Phases 1 and 2 are merged (#20, #22); this branch is the third**
and sits on `main`, rebased onto it at `3e70794`. Both
databases exist, the Builder builds them, and something now *reads* them: search answers, the
queue orders, playback makes sound — all in tests, none inside a running server. 912 tests
pass in the standard gate (933 with the slow suites), 93.1% coverage, `scripts/check.sh`
green. The next phase is `apps/server/`, which is where the wiring stops being a test fixture.

**[Issue #23](https://github.com/clydepro/encore/issues/23) is closed by this branch's PR**,
in the sense phase 2 meant it: everything the issue asks for is implemented. Steps 10–17
have no issues; open yours before branching (CONTRIBUTING §2).

## Start here

The five files worth reading in order, before any of the modules around them:

1. **`docs/adr/ADR-011-*.md`** — ten minutes, and it is the hinge of the phase. Why the queue
   commands the player directly, why everything backward goes through the bus, and why
   "publish a `PlayThisSong` event" is not decoupling but a command in a fact's clothes.
2. **`encore/services/queue_service.py`** — SAPRS Chapter 8 as code. The `SETTLEMENT` table at
   the top is the whole design in four lines; `_settle` and `_advance` are the two handlers
   every other behaviour falls out of.
3. **`encore/playback/service.py`** — SAPRS 7.3's machine, with the ordering rule in its
   docstring: the engine is silenced and the state is idle *before* `SongFinished` is
   published, which is what makes re-entry from the queue work.
4. **`encore/playback/ipc.py`** — the only file in Encore that talks to another process. Long,
   and correct in the ways that matter: array-form commands, `request_id` matching, partial
   lines buffered, garbage replies converted to `MpvGoneError`.
5. **`encore/search/query.py`** — 90 lines, and the only place untrusted text becomes an FTS5
   expression.

Then `tests/integration/test_queue_playback_and_search.py`, which wires all of it the way
`apps/server/` should.

## Things that will surprise you

- **Events are facts, so there is no "play this" event.** The one direct service-to-service
  call in Encore is `QueueService` → `Player.play()`. It is deliberate, it is ADR-011, and it
  is enforced in both directions by import scans.
- **Advancement is a loop, not a recursion.** A file mpv cannot open fails *inside* `play()`,
  which publishes `SongFinished` synchronously, which re-enters the queue's handler. The
  handler settles and returns; the outer loop picks the next head. Written as recursion, a
  shelf of 1,000 corrupted files nested handlers until `EventBus` raised `EventCycleError`,
  and the party ended with a traceback instead of a skipped track. `_MAX_WALK = 1_000` is the
  belt to that braces.
- **Stop is not advance, and a stopped song is the next thing to play.** `STOPPED` settles the
  item back to `PENDING` at position 1 and starts nothing. The first version of the handler
  advanced on every finish reason, which replayed the song an administrator had just stopped
  — the loop a real jukebox operator notices in four seconds.
- **`_play_head()` marks the item `PLAYING` *before* calling the engine.** The opposite order
  meant a file that could not be opened stayed `PENDING` at the head forever, so every
  subsequent advance re-selected it and the queue silently refused to move. It also checks
  `_is_idle()` at entry: an engine in `ERROR` is neither idle nor playing, and handing it a
  track raised mid-event. Recovery, not the queue, decides when to try again.
- **When the monitor notices mpv died, it tells the service first.** `SongFinished(FAILED)`
  then `PlaybackRecovered`, in that order, always. Publish `PlaybackRecovered` first and the
  queue's recovery handler finds a track still marked `Playing`, starts nothing, and you have
  a jukebox that survives its own crash and then plays silence forever.
- **`PlaybackOutcome.counts_as_played` was wrong before there was a caller.** It said "not
  `FAILED`", so a skip counted as a listen. Now it says `COMPLETED`, which is what SAPRS 11.9
  needs ("the appliance promised a track and could not deliver one"). The domain type landed
  in phase 1 and nothing noticed, because nothing read it: worth remembering when a phase
  adds a rule with no caller.
- **`MockMpv` was lying about `eof-reached`.** Loading a file did not clear it, so a queue
  advance appeared to walk the entire list in one tick and a recovery test passed against an
  impossible machine. Fixed, with `test_mpv_mock.py` pinning it. If a playback test starts
  behaving oddly, check the double's state model before the service's.
- **Coverage tracing costs 3× on queue timings.** The latency benchmarks skip under a tracer,
  and `scripts/check.sh --slow` passes `--no-cov` so the budgets are enforced somewhere real.
  A run that does both (release) skips them. That is a decision, not an oversight.
- **Crossfade is a fade.** One engine, volume ramped down at the end of a track and up at the
  start of the next. `audio.crossfade_seconds` promises a mix; `transition.py` says what v1
  does. SAPRS 16.4 books the real thing for 1.2.
- **Playback polls at ~1 Hz instead of subscribing to mpv events.** `tick()` is the clock the
  whole subsystem runs on: eof detection, progress, the death check. It is why `SongStarted`
  can be up to a second late in a running server, which is inside SAPRS 1.9's promise and will
  need re-reading if milestone 13's SSE budget ever gets tight.

## Guardrails that exist now

- `tests/unit/test_architecture_guardrails.py` — 25 checks. New this phase: playback may
  import neither a repository nor a storage driver; playback may import no other service;
  `encore/services/` may not import `encore.playback`; `encore/search/` may contain no SQL in
  any string literal (AST-parsed, so prose about `MATCH` does not trip it).
- `tests/integration/test_library_contract.py` (phase 2) still guards the read side; the
  search service now depends on it, because `encore/search/` has no SQL of its own to be
  wrong in.
- `tests/regression/test_issue_23_search_playback_queue.py` — eight defects, one file, and a
  table of symptoms in the docstring. Its value is the prose; a future session that finds
  three more bugs on one PR adds them *to this file*, not three files (AEP 13 is per issue).
- The one-way queue→playback rule is enforced twice: as an import scan, and behaviourally by
  "one `SongFinished` per track" in `tests/unit/test_playback_service.py`.

## What is deliberately not here

- **No server, no routes, no templates, no SSE, no admin UI, no installer.**
- **No composition root.** `build_core_services()` still builds config and logging only. The
  graph exists in `tests/integration/test_queue_playback_and_search.py`'s `Rig` and nowhere
  else; that file is the specification for `apps/server/`.
- **No `HealthService`, no `StatisticsService`, no `LibraryService`** (AIG 7). `health()`
  exists on the supervisor and returns a `ComponentHealth`; nothing publishes `HealthChanged`
  from a running process. The queue reads `LibraryStore` directly through `SongLookup` —
  revisit if a second reader appears rather than pre-emptively adding a service.
- **No `queue_operation`-style statistics.** `encore/services/errors.py` names the failures;
  counting them is milestone 14's job.

## Claims made that later code must keep true

- **One fact, one publisher.** Only `PlaybackService` publishes `SongFinished`, and only
  `PlaybackSupervisor` publishes `PlaybackRecovered`. A server that publishes either "for the
  UI" will double-settle the queue. Nothing catches this but review, so it is written here.
- **`queue_item_id` is a correlation id and nothing more.** It travels
  queue → player → event so the queue knows which item ended. The moment playback *reads* it,
  playback knows about the queue and ADR-011's asymmetry is gone. No test can check the
  intent; `test_playback_reaches_no_other_service` checks the imports that would follow.
- **The queue never writes `library.db` and never resolves a path itself.** SAPRS 7.9: the
  file to play arrives as an argument.
- **Guests stay anonymous through this layer.** A `SongQueued` event carries no identity —
  asserted by `test_a_queued_event_carries_no_guest_identity`, which is the ADR-007 check that
  matters most here because the queue is where a "per-guest history" feature would try to
  start.
- **`runtime.db` timestamps stay aware UTC** (`repositories/runtime/types.py`), including the
  `played_at` the queue writes when an item moves to `PLAYING` — and only then, which is what
  makes a settlement's `finished_at >= started_at` assertion in the party suite meaningful.
- **Search latency is a budget on the repository, not the service.** `SearchService` adds one
  batch read of song rows per page; the 100 ms budget is held by FTS5 and
  `test_builder_scale.py`.

## Known loose ends

- **This phase has met a real mpv (0.35.1), and it found three bugs.** `MockMpv` answers
  `loadfile` by setting the properties the next read will return; a real mpv answers by
  *promising* and loading asynchronously. Running the stack against the binary produced
  `tests/integration/test_real_mpv.py` plus three fixes. Each fix is also covered by a mocked
  regression test in `tests/regression/test_issue_23_search_playback_queue.py`, so CI without
  mpv still guards all three, and ADR-005 carries the rules they taught under
  `Implementation notes` — read that before touching `encore/playback/` again. `--input-ipc-run` does not exist before mpv 0.36, and an unknown
  option is fatal at parse time: every launch died with "Error parsing option input-ipc-run
  (option not found)" and exit code 1. Permissions are ours to set instead — 0700 on the
  directory, `chmod 0600` on the socket — and the half that needs no mpv is checked by
  `test_the_socket_directory_is_private_before_mpv_is_asked_for_it`. `idle-active=True` for
  ~150 ms after an accepted load was read as a finished track, which published
  `SongFinished(COMPLETED)` in answer to a guest's request. And each engine restart abandoned
  the socket it replaced, leaking a descriptor per recovery.
- **A fourth bug was in the same code and not found by mpv.** A `paths.temp_dir` the process
  cannot create or tighten raised `NotADirectoryError`/`PermissionError` out of
  `MpvLauncher.launch()`, and the supervisor catches only `MpvGoneError` and
  `EngineUnavailableError` — so one mistyped config key made `tick()` raise from a timer while
  every other launch failure reported itself. Read the exception lists on a restart path if you
  touch this again; they are the whole design of that code, and no mock exercises them.
- **What a real mpv run still cannot show:** that sound reaches a speaker. Every one of those
  tests uses `--ao=null`, so the audio path — ALSA device names, a USB DAC, what a Pi's
  `/run/encore` socket permissions look like under the `encore` service account — is
  milestone 15's bring-up checklist, not this suite's claim.
- **`_MAX_WALK`'s error path is untested by design** (a thousand consecutive failed starts is
  a fixture that fakes the situation the guard exists for). The same is true of `ipc.py`'s
  socket-wait and kill-escalation branches; `test_real_mpv.py` now walks some of them on any
  machine that has mpv, and only there.
- **`aac`/`m4a` synthetic media still raises** `SyntheticMediaUnavailableError`, asserted in
  `test_media_generator.py`. The real-mpv pass corrected what the suite claims about the rest:
  MP3 containers *do* decode (mpv reports a length and a running position), so the earlier
  note that they are "not enough for decoding" was pessimistic; FLAC does not — a STREAMINFO
  with no frames is refused. Both halves are now asserted in
  `test_the_synthetic_corpus_is_what_we_claim_it_is` so the sentence cannot rot again.
- **The queue's `up_next()` batch-reads songs but does no pagination.** The service takes
  `page_size` at construction; the UI's "Up Next" list is unbounded today. Fix it when the
  fragment exists, so the shape is chosen with the screen in view.
- **Two skipped budgets, two named milestones**: HTMX navigation (12) and SSE propagation (13)
  have targets with no code to measure. The registry in
  `tests/performance/test_performance_targets.py` says so and fails if a third one lingers.
- **The party simulation is bounded on purpose**: 7 songs, 6 minutes, no HTTP. Its
  conservation ledger is the part to keep and extend; the milestone-16 driver replaces the
  scale, not the assertions.
- **`scripts/check.sh --slow` now runs pytest twice** — once traced for coverage, once untraced
  for the budgets. It costs a minute and buys the only configuration in which both numbers
  mean anything.
- **Human review is still a norm, not a gate**: `required_approving_review_count` is 0 because
  GitHub will not let a sole maintainer approve their own PR.
