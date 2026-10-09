"""Performance target registry (SAPRS 1.8, AIG 19).

The numbers live in one place so a later benchmark can never "re-tune" a target
to make a slow feature pass. Each entry says which milestone makes it real.

A skipped target here is a commitment with a date, not a green box: the reason
`search` is measured rather than skipped is that the search layer exists, and the
reason the others are not is that they do not. When you implement a subsystem,
replace its skip with the benchmark — leaving the skip in place after the code
lands is how a budget quietly stops being enforced.

The `search` budget is measured in `test_builder_scale.py`, against a library of
1,500 songs built by the real Builder rather than against a hand-written fixture:
an FTS5 index's latency is a property of its size, so a benchmark over 20 rows
proves only that 20 rows are fast.
"""

from __future__ import annotations

import pytest

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
    "htmx_navigation": "12 (HTMX)",
    "playback_start": "8 (Playback) — measured in test_runtime_latency.py",
    "sse_propagation": "13 (SSE)",
}

#: Operations with no code to measure yet. Everything else must have a benchmark.
#:
#: `queue_operation` and `playback_start` left this list with milestone 3 (AIG 21 steps 8-9)
#: and are measured in `test_runtime_latency.py`. Only the two that need a request and a
#: browser remain: HTMX navigation and SSE propagation, which cannot be measured before
#: milestones 11 and 13 put an HTTP server in front of them.
UNIMPLEMENTED = ("htmx_navigation", "sse_propagation")


@pytest.mark.parametrize("operation", sorted(UNIMPLEMENTED))
@pytest.mark.slow
def test_latency_budget_is_not_forgotten(operation: str) -> None:
    """A placeholder that outlives its subsystem is a lying placeholder.

    This exists so that adding a service without adding its benchmark fails here:
    the check is whether the operation is still listed as unimplemented, and
    `test_registry_accounts_for_every_target` below keeps the two lists honest.
    """

    assert operation in OWNING_MILESTONE
    pytest.skip(
        f"{operation} benchmark lands with milestone {OWNING_MILESTONE[operation]}; "
        f"budget is p95 < {TARGETS_MS[operation]:g} ms"
    )


def test_registry_accounts_for_every_target() -> None:
    """Every budget is either measured or has a milestone that will measure it.

    Fast on purpose (no `slow` marker), because the failure this catches is a bookkeeping
    one: a target added to `TARGETS_MS` with no owner, or an implemented subsystem still
    sitting in the skip list pretending to be unmeasured.
    """

    assert set(TARGETS_MS) == set(OWNING_MILESTONE)
    assert set(UNIMPLEMENTED) <= set(TARGETS_MS)
    measured = set(TARGETS_MS) - set(UNIMPLEMENTED)
    assert measured == {"search", "queue_operation", "playback_start"}, (
        "a subsystem that landed needs its benchmark written"
    )
