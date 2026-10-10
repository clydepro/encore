# Testing

SAPRS Chapter 14 is the policy; this file is the mechanics.

## Suites

| Directory | Marker | Runs in | Requires |
| --------- | ------ | ------- | -------- |
| `tests/unit/` | `unit` | Validate workflow, every commit | nothing |
| `tests/integration/` | `integration` | Test workflow, PR gate | nothing (mpv optional) |
| `tests/regression/` | `regression` | Test workflow + `scripts/check.sh`, every commit | nothing |
| `tests/performance/` | `performance`, `slow` | Scheduled + release | `--run-slow` |
| `tests/party_simulation/` | `party_simulation`, `slow` | Scheduled + release | `--run-slow`, a profile |

One integration file, `test_real_mpv.py`, drives the real binary and is marked `slow` as
well: it skips wherever mpv is not installed, and it is kept out of the per-commit gate so
that a machine with mpv behaves like one without. It is not decoration — the nightly
`Test / long-running` job installs mpv and runs `tests/integration --run-slow`, which is
where CI owns a player and therefore the only place those tests run without someone's
laptop. A suite that only ever passes locally is a claim, not a check.

Markers are applied automatically from the directory by
`tests/conftest.py`, so a new file needs no decorator to land in the right suite.

## Running

```bash
scripts/test.sh                       # unit + integration with coverage
scripts/test.sh unit                  # one directory
scripts/test.sh --all                 # add performance + Party Simulation
uv run pytest -k gapless -x -q        # direct pytest, as often as you like
uv run pytest tests/party_simulation --run-slow \
  --party-profile tests/party_simulation/profiles/stress.toml
```

Coverage: `coverage.xml` for CI, terminal table for you. Raise
`fail_under` in `pyproject.toml` as milestones land; SAPRS 14.16 sets the
destination (90% overall, 95% domain/queue/search, 90% playback/builder).

## Shared fixtures (`tests/conftest.py`)

- `encore_home` — a sandbox mirroring the appliance layout (SAPRS 13.3).
- `library_db` / `runtime_db` — disposable SQLite files;
  `library_db.connect(readonly=True)` proves immutability.
- `mpv` — a `MockMpv` double; drives commands through the same JSON lines the
  real IPC client will send. It queues events and, like the real transport, *drains*
  them when the player reads: a test that emits one and then expects `next_event` to
  still have it will be surprised. Assert through `observe()`, or emit after the read.
- `media_dir` — an empty directory with the tree structure playback expects.
  Empty by design: a test that needs a file makes one.
- `music_tree` — a small generated corpus in `<artist>/<album>/` layout.
- `built_library` — one real `encore-builder` run over `music_tree`, published
  into `encore_home`; carries both the report and the options that built it.
- `library_store` / `runtime_store` — the two databases opened through their
  repositories.
- `sqlite_fts5` — skips cleanly when the interpreter's SQLite lacks FTS5.

## Doubles (`tests/support/`)

- `sqlite.py` — temp databases, read-only connections, FTS5 probe.
- `mpv.py` — the mock player: commands, properties, events, `crash()`. It models
  mpv's *state*, not just its reply codes, which is the difference between a double
  that can drive a recovery test and one that merely answers. Loading a file clears
  `eof-reached` and `pause` (a mock that left them set made a queue advance appear to
  walk the whole list in one tick, and `test_mpv_mock.py` now pins it), and
  `crash()` closes the socket so a channel sees the death the way `JsonIpc` does.
- `media.py` — synthetic media. Writes real, playable-headed MP3 and FLAC
  containers with valid frames, tags them through mutagen, and embeds genuine
  JPEG covers built with Pillow. Duration comes from the container, so a test
  that asserts "213 seconds" is asserting what a probe would report.
  The audio is silence. Measured against a real mpv (0.35.1) rather than assumed: MP3
  containers *do* decode — the engine reports a length and a running position, which is
  enough for the IPC-and-state-machine tests in `test_real_mpv.py` — while FLAC does not,
  because a STREAMINFO with no frames is refused. Nothing in the suite needs a recorded
  fixture, and nothing should claim to test sound.
  `aac`/`m4a` raise `SyntheticMediaUnavailableError` rather than emitting a file
  that only looks like one (PBK 16): a builder test that passed against a fake
  container would be testing the fake.
- `party_profiles.py` — loader for the load profiles below.

## What unit tests may not do (SAPRS 14.4)

- Require audio hardware or a real mpv process.
- Make network calls, including to MusicBrainz.
- Read a developer's actual music library or `/etc/encore/config.yaml`.
- Depend on wall-clock timing. Pass `occurred_at=` (events) or `enqueued_at=`
  (queue items) instead: `encore/utilities/clock.py` makes time an argument, and
  every domain and event timestamp is injectable.

## Synthetic media, and what it cannot prove

`tests/support/media.py` writes valid MP3 and FLAC containers — correct frames,
real durations, tags, embedded JPEG covers — containing silence. That is enough
for everything that reads a file's *structure*: metadata extraction, discovery,
deduplication, search, artwork. Measured against a real mpv rather than assumed,
it is also more enough than this file used to claim: MP3 containers decode, with a
length and a running position, so a state machine can be tested against the actual
engine. FLAC is the exception — a STREAMINFO with no frames is refused.

So playback tests divide in three, and the division is deliberate rather than an
accident of what was convenient:

- **Tested, against `MockMpv`.** mpv's JSON IPC contract, the Supervisor's state
machine, gapless and crossfade *commands*, recovery from a crash. The mock is not
cheating because it accepts the same command objects the real client serialises — a
mismatch in the command vocabulary still fails.
- **Tested again, against the real binary** (`tests/integration/test_real_mpv.py`,
skipped where mpv is not installed). A mock that answers `loadfile` by setting the
properties in the same call flatters the design in four specific ways, all of which
were bugs: an unsupported option is fatal at parse time, so the launch never happened;
a load takes ~150 ms to become visible, so `idle-active=True` is not "finished"; a
channel owns a file descriptor, so a restart that replaces one leaks one; and a file the
engine refused is indistinguishable, by polled properties, from a file that played out
between two reads — only mpv's `end-file` event says which, and a mock that never pushes
one cannot show that either. Anything that
asserts a property of *mpv* rather than of Encore belongs here, and where both can prove
it, both do.
- **Skipped, with a reason.** Anything whose assertion is about sound arriving: gapless
*audibility*, crossfade curve shape, decoder behaviour on odd samples, true gapless frame
boundaries, and the whole ALSA/DAC path (`--ao=null` is what these tests use). A test
asserting those against silent containers would be testing the fixture, and a green suite
that quietly means nothing is worse than an honest skip.

If the skips ever start hiding real regressions — a playback bug shipped past a
test that skipped rather than caught it — the policy is wrong and should be
replaced with committed fixtures, which is a decision about what a clone contains
and therefore wants an ADR. Until then, `pytest -rs` lists what is not being
proven.

`aac`/`m4a` generation is not faked: it raises `SyntheticMediaUnavailableError`,
and `test_media_generator.py` asserts that it does, so the gap cannot become
invisible.

## Regression tests (SAPRS 14.14, AEP 13)

Every fixed defect gets a permanent test in `tests/regression/`, named
`test_issue_<number>_<slug>.py`, with `Regression: #<number>` in the docstring.
Write the test that fails on the old behaviour *first*; if it cannot fail, you
have not reproduced the bug.

Two things make that stick: the suite runs on every commit rather than only at
release, and `tests/unit/test_test_harness.py` fails the build if a module in
`tests/regression/` lacks the citation. The first example is
`test_issue_11_dependabot_lockfile_only.py`, which pins the Dependabot settings
that previously produced eight unmergeable pull requests.

## Party Simulation (SAPRS 14.11)

Profiles in `tests/party_simulation/profiles/` describe 5 / 25 / 50 / 100+ guest
parties plus the SAPRS baseline (40 guests, one hour, 15,000 songs). The loader and
the profile assertions have run since milestone 2, so the vocabulary cannot drift.

`test_queue_and_playback.py` simulates a bounded party today: six minutes of the
baseline profile's rates — guests queueing, an administrator skipping, mpv dying
mid-set — against the real queue, the real playback state machine and both databases.
The assertions are accounting and ordering, not latency: every request ends up in
exactly one bucket, the songs heard are a subsequence of the songs asked for, a
duplicate plays twice, and nothing is left `Playing`. That is the failure mode this
suite exists for, and it does not need 15,000 files to show up.

What is not here: HTTP, SSE, browsing, and the 15,000-song corpus. `test_party_simulation.py`
says so in its skip reason, and the full driver lands with milestone 16.

## Chaos and recovery (SAPRS 14.10, 14.13)

These are no longer planned seams; they are tests that run.

- Kill the mock player (`mpv.crash()`, or `launcher.alive = False` for the monitor) and
  assert `SongFinished(FAILED)` before `PlaybackRecovered`, then queue continuation.
  `tests/unit/test_playback_supervisor.py` and the crash cases in
  `tests/integration/test_queue_playback_and_search.py`.
- Twenty files the engine cannot open, and the queue still empties (the re-entrancy
  limit, `test_twenty_files_the_engine_cannot_open_do_not_nest_twenty_handlers`).
- Restart the service between queue operations: a `PLAYING` row left by a dead process
  returns to `PENDING` and the next guest's request continues the list
  (`test_a_restart_leaves_the_queue_waiting_and_silent`).
- mpv refusing to come back: a bounded retry ladder, and `health()` reporting
  `DEGRADED` rather than the stale `HEALTHY`.
- Deleting a media file mid-session is covered as "a song that left the library" — the
  queue drops it with a warning and plays the rest. A whole `library.db` replacement is
  `LibraryReloaded`'s business and lands with the server.
- Dropping SSE subscribers while publishing events arrives with milestone 13; the bus
  tests already cover a handler that raises and a handler that never finishes.

## Performance

`tests/performance/test_performance_targets.py` encodes the SAPRS 1.8 budgets in
one place. Benchmarks must report p50/p95/p99 and fail on p95 against those
numbers — measure before optimizing (AEP 14).

`tests/performance/test_builder_scale.py` builds a real 1,500-song library and holds
search to the 100 ms budget (measured: ~8 ms p95), and asserts that a second,
incremental build does less work than the first. Build *throughput* is printed rather
than asserted — a wall-clock line that fails on a loaded CI runner teaches people to
disable the suite, and the reuse ratio is the assertion that actually catches a
regression.

`tests/performance/test_runtime_latency.py` measures the two budgets that milestone 3
made real, over both *real* databases rather than fakes, because the cost of a queue
operation is almost entirely SQLite. Measured on the reference machine: enqueue ~11 ms
p95, removal ~23 ms p95, queue advance ~43 ms p95 against 50 ms (a thin margin, said so
in the test rather than smoothed over — the spikes are WAL checkpoints), and playback
start ~18 ms p95.

That last number is Encore's share only. mpv's part of "playback start" — a decoder
opening a file — is not measurable in a suite that SAPRS 14.4 forbids tying to a real
mpv process, so the endpoint is the `SongStarted` event rather than sound from the
speakers. The end-to-end figure is a hardware measurement and belongs in the
Administrator Guide's bring-up; the benchmark here is the half a software change can
slow down. All of it runs only under `--run-slow`.

**Those four tests skip under coverage tracing, deliberately.** The queue's work is
SQLite writes, and instrumented, the same 200 advances that cost 43 ms p95 cost 141 ms.
A budget test that fails because the runner is measuring line coverage argues for
deleting budget tests, so the file checks for a tracer and skips with a reason.
`scripts/check.sh --slow` and the scheduled slow job pass `--no-cov` for that reason, and
get their coverage number from the ordinary gate run instead — the run where a missing
test is what is being looked for. `release.yml` is the one place that asks for both at
once; there the timing tests skip, which is why the scheduled runs matter.

## Guardrails that are tests, not opinions

`tests/integration/test_library_contract.py` and
`tests/unit/test_architecture_guardrails.py` exist to fail a pull request that a
reviewer would approve.

- The first executes every SQL statement in
  `encore/repositories/library/queries.py` against a `library.db` produced by the
  real Builder, so the read side cannot drift from `apps/builder/schema.py` in
  either direction (ADR-009's one mitigation, made automatic).
- The second asserts the import rules of AIG 4 on source files, and asserts that
  importing the core services pulls in no web framework at all — the check that
  keeps "domain services never import FastAPI" true by construction rather than
  by care.

Milestone 3 extended it with four rules specific to the packages it added:
playback may import neither a repository nor a storage driver (so a song's path
arrives as an argument, SAPRS 7.9); playback may import no other service (the
queue→player dependency is one-way, ADR-011); `encore/services/` may not import
`encore.playback` (the coupling is a protocol, not a package); and `encore/search/`
may contain no SQL in any string literal, checked by parsing literals rather than by
grepping text, so that a docstring about `MATCH` does not trip the rule.

## Test-writing conventions

- `assert` on behaviour, never on log strings or private attributes.
- One behaviour per test; name it as a sentence:
  `test_duplicate_song_appends_to_queue`.
- Arrange with fixtures, Act through the public service interface, Assert on
  events plus state.
- Async tests use `pytest-asyncio`; do not spin the event loop by hand.
