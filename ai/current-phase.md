# Current Phase

**Phase 3 — search, playback, queue (AIG steps 7, 8, 9): implemented on
`feat/23-search-playback-queue`, open as a pull request against `main`.** Phases 1 and 2 are
merged: [PR #20](https://github.com/clydepro/encore/pull/20) (domain, bus, configuration) and
[PR #22](https://github.com/clydepro/encore/pull/22) (repositories and the Library Builder,
which closed issue #19). This branch therefore sits directly on `main` and needs nothing from
an unmerged sibling to build — step 7 was only ever blocked on the library database, and that
landed with #22.

Read this page first in a new session, then [`context/milestones.md`](context/milestones.md)
for what exists and what is scaffolding. Neither is authoritative: precedence is
task request → SAPRS → AIG → ADRs → AEP (AEP 2).

## Delivered

| AIG 21 | Delivered as | Spec |
| ------ | ------------ | ---- |
| 7 — Search | `encore/search/` | ADR-009, SAPRS Ch. 6, 9.2 |
| 8 — Playback | `encore/playback/` | ADR-005, ADR-011, SAPRS Ch. 7 |
| 9 — Queue | `encore/services/queue_service.py` | ADR-011, SAPRS Ch. 8 |

The appliance's centre now exists: a guest's text becomes a set of songs, a song becomes a
queue item, a queue item becomes sound from mpv, and the end of that sound becomes the next
item. All three are exercised against both real databases.

**Search** is `parse()` + `SearchService`. The parser is where the value is: it turns
`AC/DC: Thunderstruck` into `"ac"* "dc"* "thunderstruck"*` — word runs, prefix only for
tokens of two characters or more, eight tokens maximum, deduplicated in order — and it does
that by building quoted phrases out of ordinary characters rather than escaping, so SQLite's
own operators (`AND`, `OR`, `NEAR`, `"`, `*`, brackets) cannot arrive from a text field. The
service holds no SQL: every statement stays in `repositories/library/queries.py`, and a
guardrail test parses string literals to keep it that way. `SearchUnavailable` is distinct
from an empty result set (SAPRS 11.2), because "nobody matches" and "search cannot happen"
are different things to tell a guest.

**Playback** is five modules with one responsibility each — `ipc.py` (socket, framing,
process), `player.py` (mpv properties → `EngineObservation`), `service.py` (SAPRS 7.3's
machine and its events), `supervisor.py` (the process monitor), `transition.py` (gapless and
crossfade policy). The engine is observed by `tick()` at ~1 Hz rather than by subscribing to
mpv events, which is deterministic, survives a socket that missed a line, and is what makes
`MockMpv` a faithful double instead of a lucky one.

**The queue** is `QueueService`: strict FIFO, duplicates allowed, the ceiling enforced inside
the transaction that would otherwise break it, and settlement by outcome —

| `FinishedReason` | Queue item becomes | Plays on? |
| ---------------- | ---------------- | --------- |
| `COMPLETED` | `FINISHED` | yes |
| `SKIPPED` | `SKIPPED` | no |
| `FAILED` | `REMOVED` | no |
| `STOPPED` | back to `PENDING`, at the head | no |

It commands the player directly through a `Player` protocol it declares itself, and learns
what happened through `SongFinished` and `PlaybackRecovered`. That asymmetry is ADR-011,
which is the phase's one architectural decision and is now written.

## Verification state

- `scripts/check.sh` — all gates pass: ruff (lint + format), yamllint, markdownlint, secret
  scan, mypy strict, **917 tests**, 93.3% coverage.
- `scripts/check.sh --slow` — **947 pass, 3 skip** on this machine, which has mpv. On one that
  does not, the same run is 938 pass and 12 skip: the three permanent skips are HTMX and SSE
  budgets whose subsystems do not exist, and the other nine are `test_real_mpv.py`. The slow
  run passes `--no-cov`: coverage tracing inflates
  SQLite-heavy work by ~3× and would fail the queue's budget for a reason unrelated to the
  code (see `docs/Developer/Testing.md`).
- Measured, untraced, on the reference machine: enqueue **~11 ms** p95, removal **~23 ms**,
  queue advance **~43 ms** against a 50 ms budget (thin, and said so in the test), playback
  start **~18 ms** for Encore's share of the 250 ms. Search is still ~8 ms p95 over 1,500
  songs from phase 2. Three of SAPRS 1.8's five budgets are numbers now; two are skips that
  name their milestone.
- Package coverage: `encore/search/` 98.6%, `encore/playback/` 93.0%,
  `encore/services/` 98.3% (`queue_service.py` 97.4%), `encore/domain/queue.py` and
  `repositories/runtime/queue.py` 100%. SAPRS 14.16 wants 95% for queue and search and 90%
  for playback; the lowest module is `ipc.py` at 90.0%, whose misses are the socket-wait and
  kill-escalation branches that only a real, slow mpv exercises — and which are the same lines
  whether or not the binary is installed, because `test_real_mpv.py` is marked `slow` and so is
  not in the gate these numbers come from.
- New tests: 208 of them across 13 files — see `tests/unit/test_search_*.py`,
  `test_playback_*.py`, `test_mpv_player.py`, `test_queue_service.py`,
  `tests/integration/test_queue_playback_and_search.py`,
  `tests/integration/test_real_mpv.py`,
  `tests/regression/test_issue_23_search_playback_queue.py`,
  `tests/performance/test_runtime_latency.py` and
  `tests/party_simulation/test_queue_and_playback.py`.
- **The mutation checks that mattered this phase**: make `SETTLEMENT[STOPPED]` advance the
  queue and `test_a_stop_leaves_the_song_at_the_head_and_starts_nothing` fails; mark an item
  `PLAYING` after the load instead of before and the twenty-broken-files test hangs its own
  loop and fails; publish `PlaybackRecovered` before `SongFinished` and the crash-order
  regression fails; stop clearing `eof-reached` in `MockMpv._loadfile` and a party of 200
  advances in one tick, which `test_mpv_mock.py` now catches first.

## Explicitly not in this phase

No HTTP, no templates, no SSE, no admin UI, no installer, and **no composition root**:
`build_core_services()` still builds config and logging only. Nothing in this phase has ever
run inside a process that also serves a request — the integration tests wire the graph by
hand, and `tests/integration/test_queue_playback_and_search.py` is the pattern
`apps/server/` should copy rather than reinvent.

No ninth event. `SongFinished` grew a `completion` field instead, which is why
`tests/unit/test_events.py`'s "exactly eight" assertion still passes and why ADR-004 needed
no amendment.

`HealthService` and `StatisticsService` are still absent (AIG 7 lists them): the supervisor
has a `health()` that returns `ComponentHealth`, and nothing publishes `HealthChanged` from
a running application yet. Wiring those is a milestone-11/14 job with a rule attached — the
service that owns a fact publishes it, and the queue must not.

`LibraryService` likewise: the queue reads through `LibraryStore` and `SongLookup` directly,
which is honest for now and the first thing to revisit if a second reader appears.

## Next

**AIG steps 10–13: the runtime database is already done, so step 11 (FastAPI) is next**, with
`apps/server/` as the composition root that this phase's tests describe. There is no issue for
it; per CONTRIBUTING §2, open one before branching.

What is already decided that the next session can lean on:

- The graph to build is in `tests/integration/test_queue_playback_and_search.py`'s `Rig`:
  open both stores, build `SearchService`, `MpvPlayer(supervisor.channel)`,
  `PlaybackService(recovery=supervisor)`, `supervisor.bind(playback)`,
  `QueueService(store, library, player=playback, events)`, then `queue.start()`. Close in the
  opposite order. Do not invent a second wiring path in the server.
- `queue.start()` restores a `PLAYING` row from a dead process to `PENDING` and does **not**
  start audio (SAPRS 8.6: a restart preserves the queue, it does not resume a party).
- Playback's `tick()` needs a home. It belongs to a background task in the server, not to a
  request handler; nothing about a guest's HTTP request should wait on mpv.
- `EventBus` is synchronous (ADR-004), so `enqueue()` returning "you are playing now" is
  true rather than optimistic. An async bus would make ADR-011's command/notify split wrong,
  and that is the change to argue about in an ADR before writing.
- Search's page size and the `/api/v1/search` shape are undecided; `SearchService` takes
  `limit` per call and `page_size` at construction.

## Loose ends

- **This PR closes [#23](https://github.com/clydepro/encore/issues/23)**, deliberately, in the
  sense phase 2 meant it: the issue's acceptance criteria are all implemented. Steps 10–17
  still have no issues.
- **`encore/playback/` has been run against a real mpv** (0.35.1, `--ao=null`, headless), in
  `tests/integration/test_real_mpv.py`, which skips wherever the binary is absent. It found
  three defects that every mocked test passed over — see ADR-005's new `Implementation notes`
  and the regression file — and fixed them: an unsupported launch option that made mpv exit
  before opening a socket, an accepted-but-not-yet-open `loadfile` being read as a finished
  track, and a socket leaked per engine restart. A fourth came from tidying up after that run
  rather than from mpv: a `paths.temp_dir` the appliance cannot prepare raised a bare `OSError`
  out of `MpvLauncher.launch()`, which the supervisor does not catch, so a typo in one config
  key was a traceback from a timer instead of the named launch failure the other paths raise.
  Still unproven against hardware: the actual audio path (ALSA device names, USB DACs), which
  is milestone 15's bring-up.
- **The advance benchmark has a 7 ms margin.** ~43 ms p95 against 50 ms, with WAL checkpoints
  as the spikes. If it goes red on CI, the answer is fewer writes per advance, not a bigger
  number (AEP 14, and SAPRS 1.8 owns the budget).
- **`_MAX_WALK`'s error log is unreachable by design**: 1,000 consecutive items that all fail
  to start. It is not tested, and it should not be — the test would be a fixture that fakes
  the one situation the guard exists for.
- **Crossfade is a fade, not a mix.** One engine, ramped down at the end of one track and up
  at the start of the next. SAPRS 7.8 says "configurable crossfade", `audio.crossfade_seconds`
  says more than the code does, and `transition.py`'s docstring is the authority. A second
  deck is 1.2's work (SAPRS 16.4) and its own ADR.
- **`QueueService` does not publish `QueueAdvanced` for pause and resume**, and no test
  asserts that it should: SAPRS 8.9 lists the events, and a pause is not an advance. If the
  UI needs a "paused" fact, that is a `SongStarted`-shaped decision and belongs in an ADR
  with the eighth event's vocabulary, not in a handler.
- **Human review is still a norm, not a gate** — `required_approving_review_count` is 0
  because GitHub will not let a sole maintainer approve their own PR.
