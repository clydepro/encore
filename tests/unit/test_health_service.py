"""HealthService: the aggregate, and the one sentence it is allowed to say (SAPRS 11.6, 11.7).

The interesting property is not the arithmetic — three components and a `max()` is not a
threshold table — it is **when a change is worth an event**. The appliance polls health once
a second (SAPRS 7.5), and if each poll published, every phone in the room would be handed a
`HealthChanged` frame while nothing had changed and the alert strip would blink on a healthy
box. So the service compares the aggregate with the last one it reported and stays quiet
unless the *word* moved.

Two details that look like preferences and are not:

* **A source that raises is unavailable, not ignored.** An exception swallowed here would
  turn a broken component into a healthy-looking page. The alternative — letting it escape —
  would let one bad probe take down the strip that exists to report it.
* **`check()` is a read of properties, never a command.** A health poll that sent an mpv
  command would make the readiness path depend on the thing it is asking about, and a
  degraded appliance that cannot answer its own probe is a worse story than a degraded
  appliance that can.

`assert_publishes_only_when_the_word_moves` is the test this file exists to have.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from encore.domain.health import ComponentHealth, HealthStatus
from encore.events import HealthChanged
from encore.events.bus import EventBus
from encore.services.health_service import HealthService


class Clock:
    """A clock with a second in it, so `checked_at` and quiet-time behaviour are testable."""

    def __init__(self) -> None:
        self.at = datetime(2026, 6, 1, 21, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


class Source:
    """A `HealthSource` whose report a test controls."""

    def __init__(self, report: ComponentHealth | Exception) -> None:
        self.report = report
        self.calls = 0

    def health(self) -> ComponentHealth:
        self.calls += 1
        if isinstance(self.report, Exception):
            raise self.report
        return self.report


def healthy(component: str = "playback") -> ComponentHealth:
    return ComponentHealth(component=component, status=HealthStatus.HEALTHY)


def service(
    *sources: tuple[str, ComponentHealth | Exception],
    events: EventBus | None = None,
    clock: Clock | None = None,
) -> tuple[HealthService, EventBus, Clock]:
    bus = events or EventBus()
    made = HealthService(
        sources=[(name, Source(report)) for name, report in sources],
        events=bus,
        clock=clock or Clock(),
    )
    return made, bus, clock or Clock()


def test_the_worst_component_is_the_aggregate() -> None:
    """SAPRS 11.6's rule in one line, and the reason there is no threshold table.

    A weighted score would need a number nobody can justify at 11 p.m.; "the worst thing
    that is true" is a sentence an operator can act on.
    """

    made, _, _ = service(
        ("playback", healthy()),
        ("library", ComponentHealth(component="library", status=HealthStatus.UNAVAILABLE)),
        ("search", ComponentHealth(component="search", status=HealthStatus.DEGRADED)),
    )

    assert made.check().status is HealthStatus.UNAVAILABLE


def test_a_healthy_box_reports_healthy_and_says_nothing() -> None:
    made, bus, _ = service(("playback", healthy()), ("library", healthy("library")))
    seen: list[object] = []
    bus.subscribe(HealthChanged, seen.append, name="recorder")

    made.check()

    assert made.snapshot.status is HealthStatus.HEALTHY
    assert seen == [], "a first healthy report is not a change (SAPRS 11.6)"


def test_an_event_is_published_only_when_the_word_moves() -> None:
    """The poll loop is a second; the party's phones are forty; the difference is this test.

    Every `check()` recomputes. Only a change in the aggregate publishes, otherwise a
    healthy hour at a party costs one frame per second per client for nothing, and the
    alert strip blinks on a box that is fine.
    """

    source = Source(healthy())
    bus = EventBus()
    made = HealthService(sources=[("playback", source)], events=bus)
    seen: list[HealthChanged] = []
    bus.subscribe(HealthChanged, seen.append, name="recorder")

    for _ in range(30):
        made.check()
    assert seen == []

    source.report = ComponentHealth(component="playback", status=HealthStatus.DEGRADED)
    made.check()
    made.check()
    assert len(seen) == 1

    source.report = healthy()
    made.check()
    assert len(seen) == 2
    assert [event.status for event in seen] == [HealthStatus.DEGRADED, HealthStatus.HEALTHY]
    assert [event.previous for event in seen] == [HealthStatus.HEALTHY, HealthStatus.DEGRADED]


def test_a_component_that_raises_is_unavailable_rather_than_absent() -> None:
    """The failure this service exists to survive.

    Swallowing the exception and averaging the rest would report a healthy appliance while
    one of its parts had caught fire; raising out of `check()` would mean the strip that
    exists to report the fire is the thing that dies.
    """

    made, _, _ = service(
        ("playback", healthy()),
        ("runtime", RuntimeError("disk full")),
    )

    snapshot = made.check()

    assert snapshot.status is HealthStatus.UNAVAILABLE
    runtime = next(part for part in snapshot.components if part.component == "runtime")
    assert runtime.status is HealthStatus.UNAVAILABLE
    assert "disk full" in runtime.detail


def test_the_detail_names_the_worst_problem_not_every_problem() -> None:
    """`HealthSnapshot.detail` is one line on a banner; `components` is the table behind it.

    A concatenation of four sentences is unreadable on a phone, and the order of
    `components` is the order the sources were declared in, which is the order an operator
    would check them (SAPRS 11.7's step 8).
    """

    made, _, _ = service(
        (
            "playback",
            ComponentHealth(
                component="playback", status=HealthStatus.DEGRADED, detail="mpv restarted twice"
            ),
        ),
        (
            "library",
            ComponentHealth(
                component="library", status=HealthStatus.UNAVAILABLE, detail="no songs"
            ),
        ),
    )

    snapshot = made.check()

    # The unavailable component wins the line even though it was declared second, because
    # the banner and the aggregate have to agree or one of them is lying.
    assert snapshot.detail == "library: no songs"
    assert [part.component for part in snapshot.components] == ["playback", "library"]


def test_ready_is_the_documented_healthy_and_playing_word() -> None:
    """`ready` answers a systemd probe; `status` answers a person.

    SAPRS 13.1's unit wants an unambiguous "is this appliance usable". A degraded box —
    playing, with mpv restarted twice — is usable, and saying `ready=False` there would
    have a timer restart a jukebox that is working.
    """

    degraded, _, _ = service(
        ("playback", ComponentHealth(component="playback", status=HealthStatus.DEGRADED))
    )
    unavailable, _, _ = service(
        ("playback", ComponentHealth(component="playback", status=HealthStatus.UNAVAILABLE))
    )

    assert degraded.check().is_ready is True
    assert unavailable.check().is_ready is False


def test_a_source_is_asked_once_per_check_no_more() -> None:
    """The poll is a second long and four components deep; a probe that read twice per tick
    would be a bug visible only as a slower tick."""

    source = Source(healthy())
    bus = EventBus()
    made = HealthService(sources=[("playback", source)], events=bus)

    for _ in range(5):
        made.check()

    assert source.calls == 5


def test_the_published_fact_names_the_component_behind_the_move() -> None:
    """SAPRS 1.5's "clear health information", as a field rather than a guess.

    `HealthChanged` carries which component moved and what it said, so the dashboard and the
    alert strip can read "degraded: playback" off the fact instead of polling every source
    again on the appliance thread to work out where it came from.
    """

    source = Source(healthy())
    bus = EventBus()
    made = HealthService(sources=[("playback", source)], events=bus)
    seen: list[HealthChanged] = []
    bus.subscribe(HealthChanged, seen.append, name="recorder")

    source.report = ComponentHealth(
        component="playback", status=HealthStatus.DEGRADED, detail="mpv is not running"
    )
    made.check()

    assert seen[0].status is HealthStatus.DEGRADED
    assert seen[0].previous is HealthStatus.HEALTHY
    assert seen[0].component == "playback"
    assert seen[0].detail == "mpv is not running"


def test_the_snapshot_a_caller_reads_is_the_one_the_last_check_computed() -> None:
    """`snapshot` must never re-poll.

    The HTTP layer reads it on every page render; if it recomputed, a page load would be
    issuing mpv commands, and ADR-012's promise that a request never blocks the engine would
    be false in the one place it matters most.
    """

    source = Source(healthy())
    made = HealthService(sources=[("playback", source)], events=EventBus())
    made.check()
    before = source.calls

    snapshot = made.snapshot

    assert source.calls == before
    assert snapshot.status is HealthStatus.HEALTHY


def test_two_components_of_the_same_name_are_reported_separately() -> None:
    """Names are the operator's index (SAPRS 11.6), so a collision must not merge.

    The composition root declares each source with its name; nothing stops a second
    `("playback", …)` arriving by mistake, and the aggregate would silently drop one.
    """

    made, _, _ = service(
        ("playback", healthy()),
        (
            "playback",
            ComponentHealth(
                component="playback", status=HealthStatus.UNAVAILABLE, detail="second one"
            ),
        ),
    )

    snapshot = made.check()

    assert snapshot.status is HealthStatus.UNAVAILABLE
    assert len(snapshot.components) == 2


def test_no_sources_is_unavailable_rather_than_healthy() -> None:
    """A configuration mistake, reported as one.

    `max()` of an empty sequence would raise, and a default of "healthy" would let an
    appliance with no health sources at all advertise itself ready to systemd.
    """

    made = HealthService(sources=(), events=EventBus())

    assert made.check().status is HealthStatus.UNAVAILABLE


def test_checked_at_is_the_clocks_and_not_the_wall() -> None:
    """SAPRS 5's clock injection, for a timestamp a test can assert on."""

    clock = Clock()
    made, _, _ = service(("playback", healthy()), clock=clock)

    first = made.check()
    clock.advance(30)
    second = made.check()

    assert (second.checked_at - first.checked_at) == timedelta(seconds=30)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (HealthStatus.HEALTHY, HealthStatus.HEALTHY),
        (HealthStatus.DEGRADED, HealthStatus.DEGRADED),
        (HealthStatus.UNAVAILABLE, HealthStatus.UNAVAILABLE),
    ],
)
def test_the_severity_order_is_the_documented_one(
    tmp_path: object, status: HealthStatus, expected: HealthStatus
) -> None:
    """Aggregation is `max()` over `severity`, and `severity` is the order SAPRS 11.6 names."""

    made, _, _ = service(
        ("a", ComponentHealth(component="a", status=expected)),
        ("b", healthy("b")),
    )

    assert made.check().status is expected
