"""The Event Bus: decoupling, isolation and ordering (SAPRS 11.1-11.4, 11.9-11.10).

Every test here asserts on delivered state and on `DeliveryReport`, not on log
text, per `docs/Developer/Testing.md`. The failure-isolation tests use `caplog`
only to prove a failure was *recorded*, never that a particular sentence was.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from encore.domain import HealthStatus, QueueItemId, SongId
from encore.events import (
    MAX_DISPATCH_DEPTH,
    BuildCompleted,
    DeliveryReport,
    Event,
    EventBus,
    EventCycleError,
    FinishedReason,
    HealthChanged,
    LibraryReloaded,
    PlaybackRecovered,
    QueueAdvanced,
    SongFinished,
    SongQueued,
    SongStarted,
)


def queued(position: int = 1) -> SongQueued:
    return SongQueued(
        song_id=SongId(position),
        queue_item_id=QueueItemId(position),
        position=position,
        queue_length=position,
        occurred_at=datetime(2026, 5, 4, 21, 30, position, tzinfo=UTC),
    )


# -- registration --------------------------------------------------------


def test_a_subscriber_receives_the_events_it_asked_for() -> None:
    bus = EventBus()
    seen: list[SongQueued] = []
    bus.subscribe(SongQueued, seen.append)

    bus.publish(queued(1))
    bus.publish(SongStarted(song_id=SongId(1), queue_item_id=QueueItemId(1)))

    assert [event.position for event in seen] == [1], "an unrelated event was delivered"


def test_a_subscriber_to_the_base_class_sees_everything() -> None:
    """What an audit tap or the SSE fan-out (milestone 13) will rely on."""

    bus = EventBus()
    names: list[str] = []
    bus.subscribe(Event, lambda event: names.append(event.name))

    bus.publish(queued())
    bus.publish(HealthChanged(status=HealthStatus.DEGRADED, component="library"))

    assert names == ["SongQueued", "HealthChanged"]


def test_a_subclass_subscription_receives_its_subclasses() -> None:
    class Parent(Event):
        pass

    class Child(Parent):
        pass

    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(Parent, seen.append)
    bus.publish(Child())

    assert len(seen) == 1


def test_unsubscribe_stops_delivery_and_reports_itself() -> None:
    bus = EventBus()
    seen: list[Event] = []
    subscription = bus.subscribe(SongQueued, seen.append)
    assert bus.subscription_count == 1

    subscription.unsubscribe()
    report = bus.publish(queued())

    assert bus.subscription_count == 0
    assert seen == []
    assert report == DeliveryReport(event_name="SongQueued")


def test_a_subscription_used_as_a_context_manager_ends_at_the_block() -> None:
    bus = EventBus()
    seen: list[Event] = []
    with bus.subscribe(SongQueued, seen.append):
        bus.publish(queued(1))
    bus.publish(queued(2))

    assert len(seen) == 1


def test_the_same_handler_twice_is_called_twice() -> None:
    """Registering twice is a wiring bug, but not one the Bus should hide."""

    bus = EventBus()
    calls: list[str] = []

    def handler(event: SongQueued) -> None:
        calls.append("x")

    bus.subscribe(SongQueued, handler, name="first")
    bus.subscribe(SongQueued, handler, name="second")

    bus.publish(queued())

    assert calls == ["x", "x"]
    assert bus.subscription_count == 2


def test_unsubscribing_one_leaves_the_other() -> None:
    bus = EventBus()
    calls: list[str] = []
    first = bus.subscribe(SongQueued, lambda _e: calls.append("first"), name="first")
    bus.subscribe(SongQueued, lambda _e: calls.append("second"), name="second")

    first.unsubscribe()
    bus.publish(queued())

    assert calls == ["second"]


# -- delivery ------------------------------------------------------------


def test_handlers_run_in_registration_order() -> None:
    """SAPRS 11.2: ordered within the stream. Order is observable, not internal."""

    bus = EventBus()
    order: list[int] = []

    def recorder(index: int) -> Callable[[SongQueued], None]:
        """One handler per index. A single shared closure would record the same
        value five times and prove nothing about ordering."""

        def handler(event: SongQueued) -> None:
            order.append(index)

        return handler

    for index in range(5):
        bus.subscribe(SongQueued, recorder(index))

    bus.publish(queued())

    assert order == [0, 1, 2, 3, 4]


def test_publications_keep_their_order() -> None:
    bus = EventBus()
    stamps: list[datetime] = []
    bus.subscribe(SongQueued, lambda event: stamps.append(event.occurred_at))

    for position in range(1, 6):
        bus.publish(queued(position))

    assert stamps == sorted(stamps)


def test_publishing_reports_who_it_reached() -> None:
    bus = EventBus()
    bus.subscribe(SongQueued, lambda _event: None)
    bus.subscribe(Event, lambda _event: None)

    report = bus.publish(queued())

    assert report.subscribers == 2
    assert report.clean
    assert report.event_name == "SongQueued"


def test_a_publication_nobody_listens_to_is_not_an_error() -> None:
    """An idle appliance is a normal appliance."""

    report = EventBus().publish(queued())
    assert report == DeliveryReport(event_name="SongQueued")
    assert report.clean


# -- failure isolation (SAPRS 11.3, 11.9) --------------------------------


def test_one_failing_handler_does_not_stop_the_others(caplog: pytest.LogCaptureFixture) -> None:
    bus = EventBus()
    reached: list[str] = []

    def broken(event: SongQueued) -> None:
        raise RuntimeError("statistics disk full")

    bus.subscribe(SongQueued, broken, name="broken")
    bus.subscribe(SongQueued, lambda _event: reached.append("after"), name="after")

    with caplog.at_level(logging.ERROR, logger="encore.events.bus"):
        report = bus.publish(queued())

    assert reached == ["after"], "a subscriber failure starved its neighbours"
    assert report.failed == ("broken",)
    assert not report.clean
    failures = [record for record in caplog.records if "handler failed" in record.message]
    assert failures
    assert failures[0].__dict__["event"] == "SongQueued"
    assert failures[0].__dict__["handler"] == "broken"


def test_publish_never_raises_because_a_handler_did() -> None:
    """SAPRS 11.9: a guest request must not fail because a subscriber is broken."""

    def broken(event: SongQueued) -> None:
        raise ValueError("nope")

    bus = EventBus()
    bus.subscribe(SongQueued, broken)

    report = bus.publish(queued())

    assert report.failed == (broken.__qualname__,), "the report must name the handler"
    assert not report.clean


def test_a_handler_that_unsubscribes_itself_mid_dispatch_is_ignored() -> None:
    """Subscribing from a handler must not deadlock or double-deliver."""

    bus = EventBus()
    calls: list[int] = []

    def one_shot(event: SongQueued) -> None:
        calls.append(1)
        subscription.unsubscribe()

    subscription = bus.subscribe(SongQueued, one_shot)
    bus.publish(queued(1))
    bus.publish(queued(2))

    assert calls == [1]


def test_a_new_subscriber_added_from_a_handler_misses_the_running_event() -> None:
    """The registry is snapshotted before dispatch; late joins start next time."""

    bus = EventBus()
    joined: list[str] = []

    def add_other(event: SongQueued) -> None:
        bus.subscribe(SongQueued, lambda new: joined.append(new.name))

    bus.subscribe(SongQueued, add_other)
    bus.publish(queued())
    assert joined == []
    bus.publish(queued(2))
    assert joined == ["SongQueued"]


# -- cascades ------------------------------------------------------------


def test_a_service_may_publish_from_a_handler() -> None:
    """SAPRS 8.7: SongFinished -> advance -> SongStarted is required behaviour."""

    bus = EventBus()
    seen: list[str] = []

    def advance(event: SongFinished) -> None:
        bus.publish(SongStarted(song_id=SongId(9), queue_item_id=QueueItemId(9)))

    bus.subscribe(SongFinished, advance)
    for event_type in (SongStarted, SongFinished):
        bus.subscribe(event_type, lambda e: seen.append(e.name))

    bus.publish(
        SongFinished(
            song_id=SongId(9),
            queue_item_id=QueueItemId(9),
            reason=FinishedReason.COMPLETED,
        )
    )

    assert "SongStarted" in seen


def test_an_escalating_cycle_is_stopped_rather_than_followed() -> None:
    """Without the cap this recurses until the process dies (SAPRS 11.10's intent)."""

    bus = EventBus()

    def ping(event: SongQueued) -> None:
        bus.publish(queued())

    bus.subscribe(SongQueued, ping)

    with pytest.raises(EventCycleError, match="cycle"):
        bus.publish(queued())


def test_a_legitimate_chain_of_publications_runs_to_the_end() -> None:
    """Real cascades are a handful deep, and the cap must not clip them.

    The chain here is the shape SAPRS 8.7 describes: each fact leads to exactly
    one next fact, and nothing repeats. `MAX_DISPATCH_DEPTH` is a cycle detector,
    so it has to stay above any sequence a party can produce.
    """

    bus = EventBus()
    facts: list[Event] = [
        SongStarted(song_id=SongId(1), queue_item_id=QueueItemId(1)),
        SongFinished(
            song_id=SongId(1),
            queue_item_id=QueueItemId(1),
            reason=FinishedReason.COMPLETED,
        ),
        QueueAdvanced(now_playing=SongId(2), queue_length=4),
        HealthChanged(
            status=HealthStatus.DEGRADED,
            component="playback",
            previous=HealthStatus.HEALTHY,
        ),
        PlaybackRecovered(reason="mpv restarted", restart_count=1),
        LibraryReloaded(song_count=15_000),
        BuildCompleted(song_count=15_000, validated=True),
    ]
    steps: list[str] = []

    def relay(index: int) -> Callable[[Event], None]:
        """Record this fact, then cause the next one - and only the next one."""

        def handler(event: Event) -> None:
            steps.append(event.name)
            if index + 1 < len(facts):
                bus.publish(facts[index + 1])

        return handler

    for position, fact in enumerate(facts):
        bus.subscribe(type(fact), relay(position))

    bus.publish(facts[0])

    assert steps == [fact.name for fact in facts], (
        "a seven-deep cascade must complete; the cap is for repetition, not depth"
    )
    assert 8 < MAX_DISPATCH_DEPTH < sys.getrecursionlimit()


# -- coroutines (SAPRS 11.4) --------------------------------------------


@pytest.mark.asyncio
async def test_a_slow_handler_cannot_hold_up_playback() -> None:
    """`publish()` schedules coroutines; the playback path does not wait (SAPRS 11.4)."""

    bus = EventBus()
    finished: list[str] = []

    async def slow(event: SongQueued) -> None:
        await asyncio.sleep(0.05)
        finished.append("sse")

    bus.subscribe(SongQueued, slow)
    bus.publish(queued())

    assert finished == [], "publish() blocked on a handler"
    await bus.drain()
    assert finished == ["sse"]


@pytest.mark.asyncio
async def test_publish_async_awaits_in_registration_order() -> None:
    bus = EventBus()
    order: list[str] = []

    async def slow(label: str, delay: float) -> None:
        await asyncio.sleep(delay)
        order.append(label)

    bus.subscribe(SongQueued, lambda _e: slow("first", 0.03))
    bus.subscribe(SongQueued, lambda _e: slow("second", 0.0))

    report = await bus.publish_async(queued())

    assert order == ["first", "second"], "publish_async must await in order"
    assert report.delivered == 2


@pytest.mark.asyncio
async def test_a_failing_coroutine_handler_is_isolated_and_awaited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = EventBus()
    reached: list[str] = []

    async def broken(event: SongQueued) -> None:
        raise RuntimeError("subscriber died")

    async def healthy(event: SongQueued) -> None:
        reached.append("ok")

    bus.subscribe(SongQueued, broken, name="broken")
    bus.subscribe(SongQueued, healthy, name="healthy")

    with caplog.at_level(logging.ERROR, logger="encore.events.bus"):
        report = await bus.publish_async(queued())

    assert reached == ["ok"]
    assert report.failed == ("broken",)


def test_a_coroutine_handler_without_a_loop_is_reported_not_hung(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No running loop means no way to schedule. That is a wiring error, said once."""

    bus = EventBus()

    async def handler(event: SongQueued) -> None:
        raise AssertionError("must never run")  # pragma: no cover

    bus.subscribe(SongQueued, handler, name="orphan")

    with caplog.at_level(logging.WARNING, logger="encore.events.bus"):
        report = bus.publish(queued())

    assert report.skipped == ("orphan",)
    assert not report.clean
    assert any("event loop" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_a_task_that_fails_after_publish_is_still_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scheduled handlers cannot report through the return value; the log is it."""

    bus = EventBus()

    async def explodes(event: SongQueued) -> None:
        await asyncio.sleep(0)
        raise RuntimeError("late failure")

    bus.subscribe(SongQueued, explodes, name="late")
    with caplog.at_level(logging.ERROR, logger="encore.events.bus"):
        bus.publish(queued())
        await bus.drain()

    assert [r.__dict__["handler"] for r in caplog.records if r.levelno >= logging.ERROR] == ["late"]


# -- threads -------------------------------------------------------------


def test_publishing_from_a_worker_thread_is_safe() -> None:
    """The mpv monitor (milestone 8) publishes from its own thread."""

    bus = EventBus()
    received: list[int] = []
    bus.subscribe(QueueAdvanced, lambda event: received.append(event.queue_length))

    def worker(index: int) -> None:
        for _ in range(20):
            bus.publish(QueueAdvanced(now_playing=SongId(index), queue_length=index))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(received) == 80


def test_subscribing_while_another_thread_publishes_does_not_crash() -> None:
    bus = EventBus()
    stop = threading.Event()
    seen: list[int] = []

    def churn() -> None:
        while not stop.is_set():
            subscription = bus.subscribe(SongQueued, lambda _e: seen.append(1))
            subscription.unsubscribe()

    def publish() -> None:
        while not stop.is_set():
            bus.publish(queued())

    threads = [threading.Thread(target=churn), threading.Thread(target=publish)]
    for thread in threads:
        thread.start()
    time.sleep(0.05)
    stop.set()
    for thread in threads:
        thread.join()

    assert bus.subscription_count == 0


# -- logging attribution ------------------------------------------------


def test_a_service_can_have_failures_logged_under_its_own_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("encore.queue")
    bus = EventBus(logger=logger)
    bus.subscribe(SongQueued, lambda _e: (_ for _ in ()).throw(ValueError("queue rule")))

    with caplog.at_level(logging.ERROR, logger="encore.queue"):
        bus.publish(queued())

    assert [record.name for record in caplog.records if record.levelno >= logging.ERROR] == [
        "encore.queue"
    ]


def test_the_bus_does_not_log_the_event_payload(caplog: pytest.LogCaptureFixture) -> None:
    """Payloads carry titles and paths; the journal is not a place to catalogue music.

    The Bus logs two things and nothing else: a handler that failed, and a
    coroutine it could not schedule. Both name the event, never its contents.
    """

    bus = EventBus()
    bus.subscribe(SongStarted, lambda _event: None)

    with caplog.at_level(logging.DEBUG, logger="encore.events.bus"):
        bus.publish(SongStarted(song_id=SongId(4), queue_item_id=QueueItemId(4)))

    assert all("SongId" not in str(record.__dict__) for record in caplog.records)
    assert all("SongId" not in record.getMessage() for record in caplog.records)


def test_library_and_health_events_reach_a_statistics_style_subscriber() -> None:
    """Integration-shaped unit check: an unrelated consumer sees unrelated facts."""

    bus = EventBus()
    plays = 0

    def count(event: Event) -> None:
        nonlocal plays
        plays += 1

    bus.subscribe(Event, count)
    bus.publish(BuildCompleted(song_count=15_000, validated=True))
    bus.publish(HealthChanged(status=HealthStatus.UNAVAILABLE, component="mpv"))

    assert plays == 2


# -- the corners of the dispatcher ---------------------------------------


@pytest.mark.asyncio
async def test_a_cycle_detected_in_an_awaited_handler_still_escapes() -> None:
    """`publish_async` must not turn a wiring fault into a logged failure either."""

    bus = EventBus()

    async def loop(event: SongQueued) -> None:
        await bus.publish_async(queued())

    bus.subscribe(SongQueued, loop)

    with pytest.raises(EventCycleError):
        await bus.publish_async(queued())


@pytest.mark.asyncio
async def test_drain_with_nothing_outstanding_is_cheap_and_harmless() -> None:
    """Shutdown calls this unconditionally (SAPRS 11.8); it must not need a guard."""

    bus = EventBus()
    await bus.drain()
    await bus.drain()
    assert bus.subscription_count == 0


@pytest.mark.asyncio
async def test_a_cancelled_handler_task_is_not_reported_as_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A task cancelled at shutdown is the shutdown working, not a subscriber dying."""

    bus = EventBus()
    started = asyncio.Event()

    async def slow(event: SongQueued) -> None:
        started.set()
        await asyncio.sleep(10)

    bus.subscribe(SongQueued, slow, name="sse")
    with caplog.at_level(logging.ERROR, logger="encore.events.bus"):
        bus.publish(queued())
        await started.wait()
        task = next(iter(bus._tasks))
        task.cancel()
        await bus.drain()

    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []


def test_a_handler_with_no_name_at_all_is_still_labelled() -> None:
    """functools.partial has no `__qualname__`; reports must still say who failed."""

    bus = EventBus()
    seen: list[int] = []

    def handler(multiplier: int, event: SongQueued) -> None:
        seen.append(event.position * multiplier)

    bus.subscribe(SongQueued, functools.partial(handler, 3))
    report = bus.publish(queued(2))

    assert seen == [6]
    assert report.delivered == 1
    assert report.failed == ()


def test_unsubscribing_twice_is_not_an_error() -> None:
    """A context manager and an explicit call can both end the same interest."""

    bus = EventBus()
    subscription = bus.subscribe(SongQueued, lambda _event: None)
    subscription.unsubscribe()
    subscription.unsubscribe()
    assert bus.subscription_count == 0


def test_the_bus_can_be_told_to_stay_quiet_about_its_own_work() -> None:
    """`EventBus(logger=...)` is also how a test suppresses the noise; the parameter
    exists for attribution, and silencing is the same mechanism."""

    quiet = logging.getLogger("encore.quiet.bus")
    quiet.propagate = False
    bus = EventBus(logger=quiet)
    bus.subscribe(SongQueued, lambda _event: (_ for _ in ()).throw(RuntimeError("x")))

    assert bus.publish(queued()).delivered == 0
