# apps/server

Entry point for the **Encore Jukebox Server** — the continuously running
appliance described in SAPRS 2.1 and AIG Chapter 6.

## Responsibilities

- Loads `library.db` (read-only) and `runtime.db` (read-write).
- Starts the HTTP server, Event Bus, playback supervisor, SSE publisher and the
  administrative interface.
- Runs forever under `systemd`; recovers from expected failures.

## Status

Intentionally empty during the bootstrap phase (PBK 1: "No application
functionality is implemented during this phase"). The server entry point
(`apps/server/main.py`) and its dependency-injection wiring arrive with
milestone 11 (FastAPI) and milestone 12 (HTMX) in AIG Chapter 21.

## Decided: this is where the stores are constructed

`apps/server/` opens both databases and hands them to the services that need them.
`encore/services/container.py::build_core_services()` deliberately does not, and
will not.

The reason is testability rather than taste. Opening a store requires the file to
exist, so a composition root that builds them forces every configuration and
domain test to create two SQLite databases before it can assert that a queue
rejects a negative duration. Keeping storage in the application keeps the core
importable by anything.

The shape when it lands: `open_library(paths.library_db)` and
`open_runtime_store(paths.runtime_db)` called once at startup, passed into
`LibraryService`, `SearchService` and `QueueService` by constructor, and closed on
the shutdown path alongside the Event Bus and the mpv supervisor. Both fail loudly at
startup rather than booting an appliance with no music: `open_library()` raises
`StoreNotFoundError` for a missing file and `LibraryContractError` for a database that
is not an Encore library or is from an unreadable schema revision (ADR-009's shape
check), and `open_runtime_store()` migrates forward on open or raises
`RuntimeContractError`. A typo in `paths.library_db` must never produce a running
server with zero songs.

## Rules that bind this application

- It must never import anything from `apps/builder` (ADR-001).
- It must never write to `library.db` (ADR-006, SAPRS 5.2).
- Guests stay anonymous (ADR-007).
