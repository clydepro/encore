"""Performance target registry (SAPRS 1.8, AIG 19).

The numbers live in one place so a later benchmark can never "re-tune" a target
to make a slow feature pass. Each entry says which milestone makes it real.

A skipped target here is a commitment with a date, not a green box. There are none
left, which is the point: every budget SAPRS 1.8 states is measured somewhere in this
directory, and `test_registry_accounts_for_every_target` below is what keeps it that
way. When a subsystem lands, its benchmark lands with it; a placeholder that survives
its own subsystem is how a budget quietly stops being enforced.

The `search` budget is measured in `test_builder_scale.py`, against a library of
1,500 songs built by the real Builder rather than against a hand-written fixture:
an FTS5 index's latency is a property of its size, so a benchmark over 20 rows
proves only that 20 rows are fast.
"""

from __future__ import annotations

#: p95 latency budget per operation, in milliseconds.
TARGETS_MS: dict[str, float] = {
    "search": 100.0,
    "queue_operation": 50.0,
    "htmx_navigation": 200.0,
    "playback_start": 250.0,
    "sse_propagation": 1_000.0,
}

#: Which milestone makes each measurement real.
OWNING_MILESTONE: dict[str, str] = {
    "search": "5/6 — measured in test_builder_scale.py",
    "queue_operation": "9 (Queue) — measured in test_runtime_latency.py",
    "htmx_navigation": "11-12 — measured in test_http_latency.py",
    "playback_start": "8 (Playback) — measured in test_runtime_latency.py",
    "sse_propagation": "13 — measured in test_http_latency.py",
}

#: Operations with no code to measure yet. Everything else must have a benchmark.
#:
#: Empty, and it should stay that way. `queue_operation` and `playback_start` left this list
#: with milestone 3 (AIG 21 steps 8-9) and are measured in `test_runtime_latency.py`;
#: navigation and propagation left it with milestone 11, when the server, the templates and
#: the live channel arrived, and are measured in `test_http_latency.py`. A target with no
#: benchmark is a target nobody is late for.
UNIMPLEMENTED: tuple[str, ...] = ()


def test_registry_accounts_for_every_target() -> None:
    """Every budget is either measured or has a milestone that will measure it.

    Fast on purpose (no `slow` marker), because the failure this catches is a bookkeeping
    one: a target added to `TARGETS_MS` with no owner, or an implemented subsystem still
    sitting in the skip list pretending to be unmeasured.
    """

    assert set(TARGETS_MS) == set(OWNING_MILESTONE)
    assert set(UNIMPLEMENTED) <= set(TARGETS_MS)
    measured = set(TARGETS_MS) - set(UNIMPLEMENTED)
    assert measured == set(TARGETS_MS), "a subsystem that landed needs its benchmark written"
