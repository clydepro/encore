"""The in-process Event Bus (SAPRS 11.1-11.4, 11.9, 11.10, ADR-004).

Publishers do not know their subscribers and subscribers do not know their
publishers. The Bus is the only permitted channel between runtime services, and
it is deliberately small: no broker, no disk queue, no retry, no exactly-once
promise (ADR-004). Durable state belongs in `runtime.db`.

Delivery model
--------------
* **A handler that returns None ran inline**, in registration order, on the
  publishing thread. Those handlers are expected to be cheap: record a fact,
  update a projection.
* **A handler that returns an awaitable is scheduled** as a task by `publish()`,
  so a slow browser cannot hold up playback (SAPRS 11.4). `publish_async()` awaits
  such handlers instead, in registration order - the right call at shutdown and
  in tests that want completion rather than eventual completion.
* **One handler failing never stops the others** (SAPRS 11.3, 11.9). Failures are
  logged and counted in the returned `DeliveryReport`; `publish()` does not raise
  because something downstream broke.
* **Cascades are bounded.** Services legitimately publish from inside handlers -
  SAPRS 8.7 has the queue advance after `SongFinished` - but a cycle would
  recurse until the process died, so depth is capped and the cycle is named.

Ordering is guaranteed within one publication. Across publications the Bus
preserves the order callers published in, per stream (ADR-004).

The registry is a list scanned per publication. With the handful of subscribers
this appliance will ever have, that is the right data structure; if it stops being
true, measure before changing it (AEP 14).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, TypeVar

from encore.events.base import Event

__all__ = ["MAX_DISPATCH_DEPTH", "DeliveryReport", "EventBus", "EventCycleError", "Subscription"]

E = TypeVar("E", bound=Event)

type Handler[E] = Callable[[E], None] | Callable[[E], Awaitable[None]]
"""A subscriber. Coroutine handlers are detected by what they return."""

#: How deep an event cascade may run before it is treated as a cycle. Generous
#: against real chains (queue -> playback -> SSE), and far below Python's own
#: recursion limit, so a wiring bug produces one readable error instead of a
#: crash.
MAX_DISPATCH_DEPTH = 64

LOGGER = logging.getLogger("encore.events.bus")


class EventCycleError(RuntimeError):
    """Raised when handlers cascade past `MAX_DISPATCH_DEPTH`, i.e. a cycle."""


@dataclass(frozen=True, kw_only=True)
class DeliveryReport:
    """What happened during one publication.

    Attributes:
        event_name: Class name of the event published.
        delivered: Handlers entered without raising. A scheduled handler counts
            when `publish()` starts it, not when its task finishes; a task that
            fails later is logged against its name.
        failed: Handler names that raised.
        skipped: Names of coroutine handlers that could not be scheduled because
            no event loop was running. Never silent - visible here and logged.
    """

    event_name: str
    delivered: int = 0
    failed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        """True when every interested handler ran, or was scheduled, intact."""

        return not self.failed and not self.skipped

    @property
    def subscribers(self) -> int:
        """How many handlers the event reached in any form."""

        return self.delivered + len(self.failed) + len(self.skipped)


@dataclass(slots=True, kw_only=True)
class _Registration:
    """One subscriber, as the registry holds it.

    `Any` here is deliberate and contained: the generic `E` of `subscribe` cannot
    be stored in a heterogeneous list, and the registry is the one place that
    legitimately forgets a handler's event type. Everything callers touch stays
    precise, and `isinstance` in `_matching` is what restores type safety before a
    handler is ever called with the wrong event.
    """

    token: int
    name: str
    event_type: type[Any]
    handler: Callable[[Any], object]


@dataclass(slots=True)
class _Tally:
    """Delivery accounting for one publication."""

    event_name: str
    delivered: int = 0
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def report(self) -> DeliveryReport:
        return DeliveryReport(
            event_name=self.event_name,
            delivered=self.delivered,
            failed=tuple(self.failed),
            skipped=tuple(self.skipped),
        )


class EventBus:
    """In-process publish/subscribe between runtime services.

    Args:
        logger: Where delivery failures are recorded. Defaults to
            ``logging.getLogger("encore.events.bus")``. A service that wants
            failures attributed to itself passes its own logger.
    """

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._logger = logger if logger is not None else LOGGER
        self._lock = threading.Lock()
        self._registrations: list[_Registration] = []
        self._next_token = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self._depth = threading.local()

    # -- subscribing -----------------------------------------------------

    def subscribe[E](
        self, event_type: type[E], handler: Handler[E], *, name: str | None = None
    ) -> Subscription:
        """Register `handler` for `event_type` and its subclasses.

        Subscribing to `Event` itself receives every event, which is what a
        logging tap or an SSE fan-out (milestone 13) wants.

        Args:
            event_type: Event class to listen for.
            handler: Callable, or coroutine function.
            name: Label used in reports and logs. Defaults to the handler's
                qualified name.

        Returns:
            A `Subscription`. Use it as a context manager, or call
            `unsubscribe()`, when the interest ends.
        """

        registration = _Registration(
            token=self._claim_token(),
            name=name or _handler_name(handler),
            event_type=event_type,
            handler=handler,
        )
        with self._lock:
            self._registrations.append(registration)
        return Subscription(bus=self, token=registration.token)

    def unsubscribe(self, subscription: Subscription) -> None:
        """Withdraw a subscription. Later publications never reach it."""

        with self._lock:
            self._registrations = [r for r in self._registrations if r.token != subscription.token]

    @property
    def subscription_count(self) -> int:
        """Handlers currently registered. Observability, never control flow."""

        with self._lock:
            return len(self._registrations)

    # -- publishing ------------------------------------------------------

    def publish(self, event: Event) -> DeliveryReport:
        """Deliver `event` without waiting on any handler.

        Raises:
            EventCycleError: Handlers cascaded past `MAX_DISPATCH_DEPTH`. That is
                a wiring defect, and continuing would exhaust the stack.
        """

        tally = _Tally(event.name)
        with self._dispatching():
            for registration in self._matching(event):
                pending = self._enter(registration, event, tally)
                if pending is not None:
                    self._schedule(pending, registration, event, tally)
        return tally.report()

    async def publish_async(self, event: Event) -> DeliveryReport:
        """Deliver `event`, awaiting handlers that return awaitables, in order.

        A slow handler blocks the caller here, which is exactly what it does not
        do when scheduled by `publish()`. Use this to flush work at shutdown
        (SAPRS 11.8) and in tests; never from the playback path.

        Raises:
            EventCycleError: Handlers cascaded past `MAX_DISPATCH_DEPTH`.
        """

        tally = _Tally(event.name)
        with self._dispatching():
            for registration in self._matching(event):
                pending = self._enter(registration, event, tally)
                if pending is None:
                    continue
                try:
                    await pending
                except EventCycleError:
                    raise
                except Exception as error:
                    tally.failed.append(registration.name)
                    self._log_failure(event, registration, error)
                else:
                    tally.delivered += 1
        return tally.report()

    async def drain(self) -> None:
        """Await coroutine handlers already scheduled by `publish()`.

        Must be called from the loop those tasks were scheduled on.
        """

        with self._lock:
            pending = list(self._tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # -- internals -------------------------------------------------------

    def _claim_token(self) -> int:
        with self._lock:
            token = self._next_token
            self._next_token += 1
        return token

    def _matching(self, event: Event) -> list[_Registration]:
        """Handlers interested in `event`, in registration order.

        Copied under the lock and called outside it, so a handler may subscribe,
        unsubscribe or publish without deadlocking against its own publisher.
        """

        with self._lock:
            return [r for r in self._registrations if isinstance(event, r.event_type)]

    def _enter(
        self, registration: _Registration, event: Event, tally: _Tally
    ) -> Awaitable[None] | None:
        """Call one handler, isolating its failure (SAPRS 11.3).

        Returns the awaitable when the handler is asynchronous, so the caller can
        decide whether to schedule or await it; returns None when the handler is
        done, whether or not it succeeded.
        """

        try:
            result = registration.handler(event)
        except EventCycleError:
            # Re-raised rather than recorded. SAPRS 11.3 asks that a *subscriber's*
            # failure never abort an event; a cascade is not one subscriber's
            # failure, it is the wiring being wrong, and everything dispatched
            # below it is unreliable. Recording it as a failure would let a
            # reflexive loop look like one flaky consumer.
            raise
        except Exception as error:
            tally.failed.append(registration.name)
            self._log_failure(event, registration, error)
            return None

        if inspect.isawaitable(result):
            return result
        tally.delivered += 1
        return None

    def _schedule(
        self,
        awaitable: Awaitable[None],
        registration: _Registration,
        event: Event,
        tally: _Tally,
    ) -> None:
        """Put an asynchronous handler on a running loop, or say plainly it cannot."""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _discard(awaitable)
            tally.skipped.append(registration.name)
            self._logger.warning(
                "coroutine handler needs an event loop",
                extra={"event": event.name, "handler": registration.name},
            )
            return

        task: asyncio.Task[None] = loop.create_task(
            _bridge(awaitable), name=_task_name(event, registration)
        )
        with self._lock:
            self._tasks.add(task)
        task.add_done_callback(lambda finished: self._forget(finished, registration))
        tally.delivered += 1

    def _forget(self, task: asyncio.Task[None], registration: _Registration) -> None:
        """Report a scheduled handler that failed after `publish()` had returned."""

        with self._lock:
            self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._logger.error(
                "async event handler failed",
                extra={"handler": registration.name, "error_type": type(error).__name__},
                exc_info=(type(error), error, error.__traceback__),
            )

    def _log_failure(self, event: Event, registration: _Registration, error: BaseException) -> None:
        self._logger.error(
            "event handler failed",
            extra={
                "event": event.name,
                "handler": registration.name,
                "error_type": type(error).__name__,
            },
            exc_info=(type(error), error, error.__traceback__),
        )

    @contextmanager
    def _dispatching(self) -> Iterator[None]:
        """Bound handler cascade depth on this thread."""

        depth = getattr(self._depth, "value", 0) + 1
        if depth > MAX_DISPATCH_DEPTH:
            raise EventCycleError(
                f"event handlers cascaded {MAX_DISPATCH_DEPTH} deep; the wiring contains a "
                "cycle (SAPRS 11.10 forbids handlers invoking other handlers directly)"
            )
        self._depth.value = depth
        try:
            yield
        finally:
            self._depth.value = depth - 1


class Subscription:
    """Handle for one registration; withdraw it with `unsubscribe()`."""

    __slots__ = ("_bus", "_token")

    def __init__(self, *, bus: EventBus, token: int) -> None:
        self._bus = bus
        self._token = token

    @property
    def token(self) -> int:
        return self._token

    def unsubscribe(self) -> None:
        self._bus.unsubscribe(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exception: object) -> None:
        self.unsubscribe()


def _discard(awaitable: Awaitable[None]) -> None:
    """Close an awaitable that was created and then never started."""

    close = getattr(awaitable, "close", None)
    if callable(close):
        close()


async def _bridge(awaitable: Awaitable[None]) -> None:
    """Turn any awaitable into a coroutine, which is what a Task requires.

    `drain` needs Tasks it can gather, and `loop.create_task` only accepts
    coroutines; this is the adapter, and the only place the Bus assumes a handler
    resolves to None.
    """

    await awaitable


def _task_name(event: Event, registration: _Registration) -> str:
    return f"encore:{event.name}:{registration.name}"


def _handler_name(handler: object) -> str:
    qualified = getattr(handler, "__qualname__", None)
    if isinstance(qualified, str):
        return qualified
    return repr(handler)
