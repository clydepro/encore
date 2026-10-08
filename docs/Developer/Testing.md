# Testing

SAPRS Chapter 14 is the policy; this file is the mechanics.

## Suites

| Directory | Marker | Runs in | Requires |
| --------- | ------ | ------- | -------- |
| `tests/unit/` | `unit` | Validate workflow, every commit | nothing |
| `tests/integration/` | `integration` | Test workflow, PR gate | nothing |
| `tests/regression/` | `regression` | Both | nothing |
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
- `media_dir` — a directory for synthetic audio.
- `sqlite_fts5` — skips cleanly when the interpreter's SQLite lacks FTS5.

## Doubles (`tests/support/`)

- `sqlite.py` — temp databases, read-only connections, FTS5 probe.
- `mpv.py` — the mock player: commands, properties, events, `crash()`.
- `media.py` — synthetic media hooks. **Placeholder:** it raises
  `SyntheticMediaUnavailableError` rather than pretending to work, until milestone 6.
- `party_profiles.py` — loader for the load profiles below.

## What unit tests may not do (SAPRS 14.4)

- Require audio hardware or a real mpv process.
- Make network calls, including to MusicBrainz.
- Read a developer's actual music library or `/etc/encore/config.yaml`.
- Depend on wall-clock timing; use the injected clock once it exists.

## Regression tests (SAPRS 14.14, AEP 13)

Every fixed defect gets a permanent test in `tests/regression/`, named
`test_issue_<number>_<slug>.py`, with `Regression: #<number>` in the docstring.
Write the test that fails on the old behaviour *first*; if it cannot fail, you
have not reproduced the bug.

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

## Test-writing conventions

- `assert` on behaviour, never on log strings or private attributes.
- One behaviour per test; name it as a sentence:
  `test_duplicate_song_appends_to_queue`.
- Arrange with fixtures, Act through the public service interface, Assert on
  events plus state.
- Async tests use `pytest-asyncio`; do not spin the event loop by hand.
