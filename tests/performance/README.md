# tests/performance

Measures the SAPRS 1.8 targets rather than asserting them casually. The budgets live in
one dict in `test_performance_targets.py`, so a benchmark cannot re-tune a target to make
a slow feature pass.

| Operation              | Target (p95)  | Measured in                                         |
| ---------------------- | ------------- | --------------------------------------------------- |
| Search                 | < 100 ms      | `test_builder_scale.py` (~8 ms, 1,500 songs)        |
| Queue operation        | < 50 ms       | `test_runtime_latency.py` (~11-43 ms)               |
| HTMX navigation        | < 200 ms      | not until milestone 12                              |
| Playback start         | < 250 ms      | `test_runtime_latency.py` (~18 ms, Encore's share)  |
| SSE state propagation  | < 1 s         | not until milestone 13                              |

Guidance:

- Measure against real stores and a real built library. A queue benchmark over a fake store
  measures Python, not SQLite, and SQLite is what a queue operation costs.
- Record with `time.perf_counter`, report p50/p95/p99, and fail on p95 (nearest-rank: with
  200 samples the 190th sorted value is the budget, and interpolation hides a tail).
- Measure before optimizing (AEP 14); a green benchmark is documentation.
- **Never trace while timing.** Coverage instrumentation costs the queue ~3x, which is a
  false failure rather than a slow one; `test_runtime_latency.py` skips when it detects a
  tracer, and `scripts/check.sh --slow` runs it with `--no-cov`.
- These tests carry the `performance` and `slow` markers and never run in the per-commit
  gate.
