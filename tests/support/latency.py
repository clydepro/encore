"""Wall-clock budgets, measured honestly (SAPRS 1.8, AIG 19).

The performance files share two decisions that are easy to get wrong in ways that make a budget
meaningless, so they live here rather than being copied:

* **Percentiles are nearest-rank, not interpolated.** With 200 samples the 190th sorted value is
  what "p95" means. An interpolated percentile hides a tail, and a tail is what a guest feels.
* **A run under a coverage tracer is not a measurement.** Tracing multiplies the cost of every
  Python-level operation and leaves SQLite's alone, which skews exactly the ratio these tests
  exist to watch. The honest answer is to say so and skip, not to widen a budget until it
  passes: a number nobody believes enforces nothing (AEP 14). `scripts/check.sh --slow` and the
  scheduled workflow are where the numbers are enforced, untraced.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Awaitable, Callable
from typing import Final

import pytest

#: Operations per sample set. 200 is `queue.max_items` on the shipped default, so the last
#: operation is measured against a full queue rather than an empty one — the expensive end, and
#: the end a party spends most of its time at.
OPERATIONS: Final = 200


def traced() -> bool:
    """Whether something is stepping through every line we execute."""

    return sys.gettrace() is not None or "coverage" in sys.modules


def require_untraced() -> None:
    """Skip a timing test when a tracer is attached, rather than fail it."""

    if traced():
        pytest.skip("wall-clock budgets are meaningless under a coverage tracer (AEP 14)")


def p95(samples: list[float]) -> float:
    """The nearest-rank 95th percentile, in milliseconds."""

    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(0.95 * (len(ordered) - 1)))]


def timed(operation: Callable[[], object], *, repeats: int = OPERATIONS) -> list[float]:
    """Time `operation` `repeats` times, dropping the first call as warm-up."""

    require_untraced()
    operation()
    timings: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        operation()
        timings.append((time.perf_counter() - started) * 1_000.0)
    return timings


async def timed_async(
    operation: Callable[[], Awaitable[object]], *, repeats: int = OPERATIONS
) -> list[float]:
    """`timed()` for a coroutine operation.

    No idle padding between samples: the awaits inside the operation are the work being measured
    (a template render, an ASGI round trip), and adding a sleep would be adding a number.
    """

    require_untraced()
    await operation()
    timings: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        await operation()
        timings.append((time.perf_counter() - started) * 1_000.0)
    return timings
