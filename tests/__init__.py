"""Test suite for Encore.

Structured by test category (SAPRS 14.3) rather than by module, so that the
cost of writing a regression test never depends on where the code lives.

- `unit/` — isolated service and helper tests; no mpv, no network, no audio.
- `integration/` — collaborating services (queue↔playback, bus↔SSE, library↔search).
- `regression/` — one permanent test per fixed defect (SAPRS 14.14).
- `performance/` — the latency targets in SAPRS 1.8.
- `party_simulation/` — simulated guests against a representative library.

Shared fixtures and doubles live in `support/` and are wired up in
`tests/conftest.py`.
"""
