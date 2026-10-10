"""The appliance thread: one thread owns the runtime (ADR-012).

SAPRS 11.4 asks two things of one process: long-running work must not block
playback, and dozens of held-open SSE connections must not block anything. The
services built to satisfy the first half — a synchronous Event Bus (ADR-004), a
queue that advances inline (ADR-011), an mpv client holding one unlocked socket —
are all single-threaded, and none of them becomes safe without a lock added to
code phase 3 tested and reasoned about as sequential.

This module is the seam that lets both halves be true. Everything that can reach
mpv, the bus or `runtime.db` runs on exactly one worker thread, in the order it
was asked for. Everything that only moves bytes — HTTP, SSE, template rendering —
runs on the event loop and never touches those objects.

Two properties are load-bearing, so they are stated precisely:

* **Serialization, not concurrency.** One worker means submissions run FIFO. With
  two, the queue would still enforce its ceiling correctly (that lives in the
  transaction) but two requests arriving 200 µs apart would be ordered by which
  thread got there first, and SAPRS 8.5 promises position by arrival.
* **A call submitted from the appliance thread is an error, not a delay.** With one
  worker, waiting on a task queued behind your own is a guaranteed deadlock.
  `ApplianceThread.call()` raises instead, which is the failure mode ADR-012's
  costs section predicts and the one a later contributor is most likely to
  reintroduce.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TypeVar

__all__ = ["ApplianceNotRunning", "ApplianceThread", "WrongThreadError", "call_async"]

T = TypeVar("T")


class WrongThreadError(RuntimeError):
    """Work was asked of the appliance from inside the appliance.

    The deadlock this prevents is real and silent: a single-worker pool cannot run
    a task that is waiting for the task that submitted it.
    """


class ApplianceNotRunning(RuntimeError):  # noqa: N818 - named for the state, not the failure
    """A service call arrived before the runtime started, or after it stopped."""


class ApplianceThread:
    """A single-worker executor with a name, a start and a stop.

    Args:
        name: Thread name prefix, which is what a `py-spy dump` of a stuck
            appliance shows. Worth the argument for exactly that reason.
        logger: Where a task that raised is reported. `Future` stores an exception
            until someone reads it, and for a fire-and-forget submission nobody may.

    The worker is created lazily on the first call rather than in the constructor.
    A graph built by a test should not pay for a live thread it never uses, and
    AEP 9 rules out the module-level instance that would otherwise be the
    alternative.
    """

    def __init__(
        self, *, name: str = "encore-appliance", logger: logging.Logger | None = None
    ) -> None:
        self._name = name
        self._logger = logger or logging.getLogger("encore.appliance")
        self._executor: ThreadPoolExecutor | None = None
        self._ident: int | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Allow work to be submitted. Idempotent, so a lifespan and a test both can."""

        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=self._name)

    def stop(self) -> None:
        """Finish what is queued, then end the worker.

        Waiting rather than cancelling is the point: an item the queue had already
        begun deserves its history row (SAPRS 8.7 step 2), and a shutdown that
        abandons a `stop` command mid-flight leaves a `PLAYING` row for a process
        that is going away. Anything still queued behind it is cancelled — nobody
        is waiting for those, by definition.
        """

        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        self._ident = None

    @property
    def running(self) -> bool:
        return self._executor is not None

    # -- the boundary -----------------------------------------------------

    def call(self, work: Callable[[], T]) -> Future[T]:
        """Run `work` on the appliance thread and hand back its future.

        Takes a zero-argument callable rather than a function plus arguments, so the
        caller can close over what it needs and a type checker sees the result type
        at the definition site instead of at this one.
        """

        if self.is_current:
            raise WrongThreadError(
                "work cannot be submitted from the appliance thread: it would wait behind itself"
            )
        executor = self._executor
        if executor is None:
            raise ApplianceNotRunning("the appliance is not running; startup has not finished")
        return executor.submit(self._marked(work))

    def run(self, work: Callable[[], T]) -> T:
        """`call()` and wait for the result. For a caller with no event loop."""

        return self.call(work).result()

    def assert_current(self, what: str = "this call") -> None:
        """Refuse a service read that reached a thread other than the appliance's.

        The HTTP layer calls it on the way in, so a handler that forgot to submit
        first fails in a test rather than in a party.
        """

        if not self.is_current:
            raise WrongThreadError(f"{what} must run on the appliance thread")

    @property
    def worker_ident(self) -> int | None:
        """The thread that owns the services, or None before the first task ran.

        Diagnostics and tests ask "who did that work"; production asks it of the log line a
        stuck appliance produces, and an ident is the only answer that survives a restart.
        """

        return self._ident

    @property
    def is_current(self) -> bool:
        """Whether the caller is the appliance thread.

        False until the first submitted task has begun, which is correct: before
        then there is no appliance work to be inside of.
        """

        return self._ident is not None and self._ident == threading.get_ident()

    def _marked(self, work: Callable[[], T]) -> Callable[[], T]:
        """Record this thread as the appliance, and report what nobody awaited."""

        def run() -> T:
            self._ident = threading.get_ident()
            try:
                return work()
            except Exception:
                self._logger.exception("appliance work failed")
                raise

        return run


async def call_async[T](appliance: ApplianceThread, work: Callable[[], T]) -> T:
    """Await `work` having run on the appliance thread.

    The only place in Encore that bridges asyncio and the serialized runtime, and
    deliberately this thin: hand the callable to the pool, await its future on the
    caller's loop, and let any exception arrive as though the call were local.
    """

    return await asyncio.wrap_future(appliance.call(work))
