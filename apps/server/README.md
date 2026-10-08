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

## Rules that bind this application

- It must never import anything from `apps/builder` (ADR-001).
- It must never write to `library.db` (ADR-006, SAPRS 5.2).
- Guests stay anonymous (ADR-007).
