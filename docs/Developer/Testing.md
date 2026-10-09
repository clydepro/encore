# Testing

SAPRS Chapter 14 is the policy; this file is the mechanics.

## Suites

| Directory | Marker | Runs in | Requires |
| --------- | ------ | ------- | -------- |
| `tests/unit/` | `unit` | Validate workflow, every commit | nothing |
| `tests/integration/` | `integration` | Test workflow, PR gate | nothing |
| `tests/regression/` | `regression` | Test workflow + `scripts/check.sh`, every commit | nothing |
| `tests/performance/` | `performance`, `slow` | Scheduled + release | `--run-slow` |
| `tests/party_simulation/` | `party_simulation`, `slow` | Scheduled + release | `--run-slow`, a profile |

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
  real IPC client will send.
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
- `mpv.py` — the mock player: commands, properties, events, `crash()`.
- `media.py` — synthetic media. Writes real, playable-headed MP3 and FLAC
  containers with valid frames, tags them through mutagen, and embeds genuine
  JPEG covers built with Pillow. Duration comes from the container, so a test
  that asserts "213 seconds" is asserting what a probe would report.
  The audio is silence, which is enough for metadata, discovery, deduplication
  and search; it is not enough for decoding, so playback tests still need
  recorded fixtures.
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
deduplication, search, artwork. It is not enough for anything that needs to hear
something.

So playback tests divide in two, and the division is deliberate rather than an
accident of what was convenient:

- **Tested.** mpv's JSON IPC contract, the Supervisor's state machine, gapless and
crossfade *commands*, recovery from a crash. These run against `MockMpv`, and the
mock is not cheating because it accepts the same command objects the real client
serialises — a mismatch in the command vocabulary still fails.
- **Skipped, with a reason.** Anything whose assertion is about sound arriving:
gapless *audibility*, crossfade curve shape, decoder behaviour on odd samples,
true gapless frame boundaries. A test asserting those against silent containers
would be testing the fixture, and a green suite that quietly means nothing is worse
than an honest skip.

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
parties plus the SAPRS baseline (40 guests, one hour, 15,000 songs). The driver
arrives with milestone 16; the loader and the profile assertions already run so
that the vocabulary cannot drift.

## Chaos and recovery (SAPRS 14.10, 14.13)

Planned seams, using the doubles that already exist:

- Kill the mock player (`mpv.crash()`) and assert `PlaybackRecovered` plus
  queue continuation.
- Drop SSE subscribers while publishing events.
- Delete a media file or artwork reference mid-session.
- Restart the service between queue operations.

## Performance

`tests/performance/test_performance_targets.py` encodes the SAPRS 1.8 budgets in
one place. Benchmarks must report p50/p95/p99 and fail on p95 against those
numbers — measure before optimizing (AEP 14).

`tests/performance/test_builder_scale.py` is the one that measures something
today: it generates a 1,500-file corpus, builds a real library, and asserts the
search budget (p95 < 100 ms) and that a second, incremental build does less work
than the first. Build *throughput* is printed rather than asserted — a wall-clock
line that fails on a loaded CI runner teaches people to disable the suite, and the
reuse ratio is the assertion that actually catches a regression. Both run only
under `--run-slow`.

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

## Test-writing conventions

- `assert` on behaviour, never on log strings or private attributes.
- One behaviour per test; name it as a sentence:
  `test_duplicate_song_appends_to_queue`.
- Arrange with fixtures, Act through the public service interface, Assert on
  events plus state.
- Async tests use `pytest-asyncio`; do not spin the event loop by hand.
