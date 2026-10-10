"""The appliance thread: one worker, one order, and a guard that bites (ADR-012).

SAPRS 11.4 asks for two things that pull against each other — long work must not block
playback, and dozens of held-open streams must not block anything — and the answer this
module implements is to stop pretending the services are concurrent and put them on one
thread. What is under test here is not "does a thread run" but the three properties that
make the arrangement safe:

* submissions run in the order they arrived, which is what lets `QueueService` promise a
  position by arrival (SAPRS 8.5);
* the event loop is never the thread that touches mpv, which is what lets a request that
  plays a song coexist with a request that streams;
* and a call made from the wrong side of the boundary **fails** rather than working by
  accident, because the accident is a deadlock that shows up at a party.
"""

from __future__ import annotations

import threading
import time
from functools import partial

import pytest

from encore.utilities.appliance import (
    ApplianceNotRunning,
    ApplianceThread,
    WrongThreadError,
    call_async,
)


def _wait_for(flag: threading.Event, timeout: float = 2.0) -> None:
    assert flag.wait(timeout), "the appliance thread never reached the point"


def test_work_runs_on_a_thread_that_is_not_the_caller() -> None:
    """The name is the assertion: a `py-spy dump` of a stuck appliance says which thread.

    `ThreadPoolExecutor` suffixes the prefix it is given, so the check is a prefix match —
    and the prefix is the whole point, because the default is `ThreadPoolExecutor-0_0`.
    """

    appliance = ApplianceThread(name="encore-test")
    appliance.start()
    try:
        where = appliance.run(lambda: threading.current_thread().name)
    finally:
        appliance.stop()

    assert where.startswith("encore-test")
    assert where != threading.current_thread().name


def test_submissions_run_in_the_order_they_arrived() -> None:
    """SAPRS 8.5's "position by arrival" rests on this, and on nothing else.

    Two requests that reach `QueueService` 200 µs apart must append in submission order.
    One worker makes that true by construction; two would make it true by scheduling, which
    is not a thing a test can assert and a party can certainly feel.
    """

    appliance = ApplianceThread()
    appliance.start()
    order: list[int] = []
    gate = threading.Event()
    blocked = threading.Event()

    def work(value: int) -> None:
        if value == 0:
            blocked.set()
            gate.wait(2.0)
        order.append(value)

    try:
        futures = [appliance.call(partial(work, number)) for number in range(6)]
        _wait_for(blocked)
        # Every later submission is taken while the first is still holding the worker, so
        # their order here is the order `submit` put them in and nothing else.
        time.sleep(0.05)
        gate.set()
        for future in futures:
            future.result(timeout=2.0)
    finally:
        appliance.stop()

    assert order == [0, 1, 2, 3, 4, 5]


def test_a_submission_from_the_appliance_thread_raises_instead_of_deadlocking() -> None:
    """The failure mode ADR-012 predicts, made loud.

    With one worker, a task that waits on a task queued behind itself never runs. The
    alternative to raising is a process that serves requests and plays nothing, with no
    exception in the journal — which is the worst bug this milestone could ship.
    """

    appliance = ApplianceThread()
    appliance.start()
    try:

        def nested() -> object:
            return appliance.run(lambda: "never runs")

        with pytest.raises(WrongThreadError, match="wait behind itself"):
            appliance.run(nested)
    finally:
        appliance.stop()


def test_a_service_read_from_the_loop_is_refused_by_the_guard() -> None:
    """`assert_current` is the doorway `api/deps.read()` walks through.

    A handler that called `queue.enqueue()` inline would pass every unit test in the tree
    and stall every stream in the room. This is the assertion that makes that impossible to
    write without noticing.
    """

    appliance = ApplianceThread()
    with pytest.raises(WrongThreadError, match="must run on the appliance thread"):
        appliance.assert_current("a service read")


def test_work_submitted_before_start_fails_rather_than_queueing() -> None:
    """A request that arrives during startup is "not ready", not "silently later".

    The 503 `encore/api/errors.py` maps `ApplianceNotRunning` to is the answer a probe
    should get: systemd's `ExecStartPost` and an admin's reload both mean the same thing.
    """

    appliance = ApplianceThread()

    with pytest.raises(ApplianceNotRunning):
        appliance.call(lambda: None)


def test_stop_finishes_the_work_in_flight_and_drops_the_rest() -> None:
    """SAPRS 8.7's step 2, at the thread's level: a begun song gets its history row.

    `wait=True, cancel_futures=True` is the whole policy. The task that had started runs to
    its end; the ones nobody was waiting for are dropped rather than executed during
    shutdown, because a request that arrived after the last guest left should not be the
    reason stopping takes another three seconds.
    """

    appliance = ApplianceThread()
    appliance.start()
    running = threading.Event()
    release = threading.Event()
    done: list[str] = []

    def first() -> None:
        running.set()
        release.wait(2.0)
        done.append("finished")

    appliance.call(first)
    _wait_for(running)
    appliance.call(lambda: done.append("cancelled work ran"))

    stop = threading.Thread(target=appliance.stop)
    stop.start()
    time.sleep(0.05)
    release.set()
    stop.join(3.0)

    assert not stop.is_alive(), "stop waited forever for the worker"
    assert done == ["finished"], "the begun task must finish and the queued one must not"


@pytest.mark.asyncio
async def test_call_async_hands_the_exception_back_to_the_awaiting_handler() -> None:
    """A service that raises must raise *in the request*, not in a fire-and-forget log.

    `encore/api/errors.py` maps a `QueueFullError` to a 409 by seeing it travel up the
    await. A future whose exception nobody reads would turn a refusal into a 200 with no
    queue item in it.
    """

    appliance = ApplianceThread()
    appliance.start()
    try:
        with pytest.raises(ValueError, match="no"):
            await call_async(appliance, lambda: (_ for _ in ()).throw(ValueError("no")))
    finally:
        appliance.stop()


@pytest.mark.asyncio
async def test_call_async_returns_the_result_to_the_loop_it_awaited_on() -> None:
    """The round trip is a value, not a copy: the handler's thread is not the worker's."""

    appliance = ApplianceThread()
    appliance.start()
    try:
        loop_thread = threading.current_thread()
        where = await call_async(appliance, threading.current_thread)
    finally:
        appliance.stop()

    assert where is not loop_thread


def test_the_worker_is_created_on_first_use_not_at_import() -> None:
    """AEP 9's no-module-level-instance rule, enforced by an observable.

    A graph built by a unit test should not pay for a live thread it never uses — and a
    module-level `ApplianceThread()` would make the whole suite one shared worker, whose
    ordering bugs would appear in whichever test happened to run second.
    """

    def names() -> set[str]:
        return {thread.name for thread in threading.enumerate()}

    before = names()

    appliance = ApplianceThread()
    appliance.start()
    idle = names()
    appliance.run(lambda: None)
    working = names()
    appliance.stop()
    after = names()

    assert not any("encore-appliance" in name for name in before | idle), (
        "constructing an appliance started a thread nobody asked for"
    )
    assert any("encore-appliance" in name for name in working)
    assert not any("encore-appliance" in name for name in after - before), (
        "stop left the worker running"
    )
