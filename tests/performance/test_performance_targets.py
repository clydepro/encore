"""Performance target registry (SAPRS 1.8, AIG 19).

The numbers live in one place so a later benchmark can never "re-tune" a target
to make a slow feature pass. Until the subsystems exist the tests are skipped —
they are wiring, not wishful thinking.
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
    "search": "7 (Search)",
    "queue_operation": "9 (Queue)",
    "htmx_navigation": "12 (HTMX)",
    "playback_start": "8 (Playback)",
    "sse_propagation": "13 (SSE)",
}


@pytest.mark.parametrize("operation", sorted(TARGETS_MS))
@pytest.mark.slow
def test_latency_budget(operation: str) -> None:
    pytest.skip(
        f"{operation} benchmark lands with milestone {OWNING_MILESTONE[operation]}; "
        f"budget is p95 < {TARGETS_MS[operation]:g} ms"
    )
