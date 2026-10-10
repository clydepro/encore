# apps/server

Entry point for the **Encore Jukebox Server** — the continuously running
appliance described in SAPRS 2.1 and AIG Chapter 6.

## Responsibilities

- Opens `library.db` (read-only) and `runtime.db` (read-write).
- Composes the services, the Event Bus, playback, the SSE publisher and the HTTP
  interface into one object graph, and starts it.
- Runs forever under `systemd`; recovers from expected failures.

## Status

Implemented through AIG 21 milestone 13 (#25): configuration, both stores, search,
playback with its supervisor, the queue, health, the guest HTML surface, the
`/api/v1` JSON API and `/events` SSE. The administrative interface (milestone 14)
and the installer (milestone 15) are not here yet.

## Running it

```bash
uv run python -m apps.server.main --config /etc/encore/config.yaml
```

That is the whole command, and `scripts/run-server.sh` is the wrapper `systemd`
calls. Useful flags:

| Flag | Effect |
| ---- | ------ |
| `--config PATH` | The YAML file `ConfigurationService` loads; required |
| `--host`, `--port` | Override `server.host` / `server.port` |
| `--no-playback` | Compose everything except mpv. The page works, nothing makes sound |
| `--version` | Print the version and exit 0 |

Exit codes follow `sysexits(64)`-style convention where it applies: `0` clean
shutdown, `2` a configuration file or flag the appliance will not run on, `78`
(`EX_CONFIG`) a database it could not open. A typo in `--port` is treated as the
same class of problem as a typo in the YAML, because it is: the override is
validated through the same model, so `--port 0` is refused by the code that
refuses `port: 0` in a file.

Startup is loud on purpose. `open_library()` raises `StoreNotFoundError` for a
missing file and `LibraryContractError` for a database that is not an Encore
library or is from an unreadable schema revision (ADR-009's shape check);
`open_runtime_store()` migrates forward on open or raises `RuntimeContractError`.
All four reach the operator as a message plus a traceback and exit `2` — a typo in
`paths.library_db` must never produce a running server with zero songs.

## Two files, and why they are separate

`apps/server/main.py` is the CLI: parse, load configuration, install logging,
build, serve, return a code. `apps/server/appliance.py` is the composition root:
it knows which service needs which repository and in what order they must start
and stop. The split exists so a test can build the real graph with a fake engine
launcher — every test in `tests/integration/`, `tests/e2e/` and much of
`tests/performance/` goes through `composed_appliance()`, which calls this
`build()` and not a copy of it. A graph assembled only inside `main()` would make
the shipped wiring the one thing no test exercises.

`build()` takes `launch_engine=False` for a machine with no audio, which is what
`--no-playback` sets, and a `launcher` for a test that wants to watch the mpv
protocol without mpv.

The order inside `Appliance.start()` is a claim about causality, and it is
asserted in `tests/`, not commented only: the event subscriptions go up before the
queue re-adopts what a restart left waiting, the queue before the player, and the
player before the tick loop begins polling. `stop()` unwinds it.

## One thread

`encore/utilities/appliance.py::ApplianceThread` owns the thread that touches
SQLite and mpv. HTTP handlers never touch either directly: they await
`call_async(appliance, work)` (ADR-012). That is the whole of the rule, it lives
in one function, and `tests/unit/test_appliance_thread.py` plus
`tests/integration/test_server_app.py::test_the_guardrail_reports_the_thread_that_did_the_work`
are what keep it from eroding — the second one asks a served request which thread
answered it, so the assertion is about the shipped graph rather than about a
helper someone could stop calling.

## Rules that bind this application

- It must never import anything from `apps/builder` (ADR-001).
- It must never write to `library.db` (ADR-006, SAPRS 5.2).
- Guests stay anonymous (ADR-007).
- No module under `encore/` may import this package; the dependency points here.
