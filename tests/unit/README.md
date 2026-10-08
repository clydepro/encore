# tests/unit

Isolated tests for one class, function or service at a time.

- Doubles come from `tests/support/` (mock mpv, temporary SQLite).
- Must pass without network access, audio hardware or a running server.
- Target coverage: domain services, queue and search at 95% (SAPRS 14.16).

Every other category owns its own directory; put cross-cutting behaviour there,
not here.
