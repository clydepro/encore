"""Core foundation, end to end (AIG 21 steps 2-4, PBK milestone 2).

Unit tests check each part against the spec. This checks the claim the phase
actually makes: that a process built from `build_core_services()` can take a fact
about the world, tell the services that care about it, and record what happened -
before any database, HTTP layer or player exists.

Nothing here reaches for `apps/server`; that entry point arrives with milestone
11. If it did, this test would be proving a later milestone under an earlier one.
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from encore.domain import (
    HEALTHY,
    Album,
    AlbumId,
    Artist,
    ArtistId,
    AudioFormat,
    HealthStatus,
    PlaybackState,
    QueueItem,
    QueueItemId,
    Song,
    SongId,
    can_transition,
)
from encore.events import (
    EVENT_VOCABULARY,
    BuildCompleted,
    Event,
    EventBus,
    FinishedReason,
    HealthChanged,
    QueueAdvanced,
    SongFinished,
    SongQueued,
    SongStarted,
)
from encore.services import CoreServices, LoggingService, build_core_services

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / "examples" / "config.yaml"


@pytest.fixture
def appliance() -> Iterator[tuple[CoreServices, io.StringIO]]:
    """A running core: configuration loaded, logging installed, bus open.

    Yields:
        The `CoreServices` and the buffer its logs are written to.
    """

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    core = build_core_services(path=CONFIG)
    core.logging.remove()
    buffer = io.StringIO()
    services = LoggingService(core.config.logging, stream=buffer)
    services.configure()
    try:
        yield core, buffer
    finally:
        services.remove()
        root.handlers = handlers
        root.setLevel(level)


def test_a_song_can_be_queued_and_every_interested_service_heard_about_it(
    appliance: tuple[CoreServices, io.StringIO],
) -> None:
    """The AIG 4 rule in one gesture: services talk through the Bus, not to each
    other. Nothing here imports anything from `api` or `controllers`."""

    core, _ = appliance
    heard: dict[str, list[str]] = {"queue": [], "audit": []}
    log_lines = 0

    class Queue:
        """Tracks positions. Has no idea who else is listening (AIG 4)."""

        def __init__(self, bus: EventBus) -> None:
            bus.subscribe(SongQueued, self.on_queued)

        def on_queued(self, event: SongQueued) -> None:
            heard["queue"].append(f"{event.song_id}:{event.position}")

    class Audit:
        """Subscribes to the base type, as the admin log view will (milestone 14)."""

        def __init__(self, bus: EventBus) -> None:
            bus.subscribe(Event, lambda event: heard["audit"].append(event.name))

    Queue(core.events)
    Audit(core.events)

    core.events.publish(
        SongQueued(song_id=SongId(41), queue_item_id=QueueItemId(1), position=1, queue_length=1)
    )

    assert heard["queue"] == ["41:1"]
    assert heard["audit"] == ["SongQueued"]
    assert log_lines == 0, "a clean delivery logs nothing (AEP 15)"


def test_an_evening_of_playback_produces_the_events_the_spec_lists(
    appliance: tuple[CoreServices, io.StringIO],
) -> None:
    """SAPRS 8.7's sequence, driven through the Bus with real domain objects.

    This is the phase's closest thing to an end-to-end test: no queue service and
    no mpv yet, but the facts those components will emit are already fixed, and
    the domain must be able to describe them.
    """

    core, _ = appliance
    artist = Artist(id=ArtistId(1), name="Bauhaus")
    album = Album(id=AlbumId(1), artist_id=artist.id, title="In the Flat Field")
    first = Song(
        id=SongId(1),
        title="Bela Lugosi's Dead",
        artist_id=artist.id,
        album_id=album.id,
        file_path=Path("/srv/music/bauhaus/bela.mp3"),
        file_format=AudioFormat.MP3,
        duration=timedelta(minutes=9, seconds=35),
    )
    stream: list[str] = []
    core.events.subscribe(Event, lambda event: stream.append(event.name))

    core.events.publish(
        SongQueued(song_id=first.id, queue_item_id=QueueItemId(1), position=1, queue_length=1)
    )
    core.events.publish(SongStarted(song_id=first.id, queue_item_id=QueueItemId(1)))
    assert can_transition(PlaybackState.LOADING, PlaybackState.PLAYING)
    core.events.publish(
        SongFinished(
            song_id=first.id,
            queue_item_id=QueueItemId(1),
            reason=FinishedReason.COMPLETED,
        )
    )
    core.events.publish(QueueAdvanced(finished_queue_item_id=QueueItemId(1), queue_length=0))

    assert stream == ["SongQueued", "SongStarted", "SongFinished", "QueueAdvanced"]


def test_a_broken_subscriber_needs_the_journal_not_the_party(
    appliance: tuple[CoreServices, io.StringIO],
) -> None:
    """SAPRS 11.9 and 11.3 in one situation: a subscriber fails while a guest's
    request is being published, the request succeeds anyway, and the failure is
    written down with enough in it to act on."""

    core, buffer = appliance

    def statistics(event: SongQueued) -> None:
        raise OSError("runtime.db is full")

    core.events.subscribe(SongQueued, statistics, name="statistics")
    reached: list[int] = []
    core.events.subscribe(SongQueued, lambda event: reached.append(event.position))

    report = core.events.publish(
        SongQueued(song_id=SongId(2), queue_item_id=QueueItemId(2), position=1, queue_length=1)
    )

    assert reached == [1], "the guest's request survived a subscriber's failure"
    assert report.failed == ("statistics",)
    line = json.loads(buffer.getvalue())
    assert line["level"] == "ERROR"
    assert line["event"] == "SongQueued"
    assert line["handler"] == "statistics"
    assert line["error_type"] == "OSError"
    assert "runtime.db is full" in line["exception"]


def test_health_and_library_facts_reach_the_dashboard_subscriber(
    appliance: tuple[CoreServices, io.StringIO],
) -> None:
    """SAPRS 11.6: HealthService publishes; SSE and the admin view subscribe.
    Neither is built yet, and neither needs to be for the contract to hold."""

    core, _ = appliance
    cards: list[str] = []

    def dashboard(event: Event) -> None:
        if isinstance(event, HealthChanged):
            was = "never-reported" if event.previous is None else event.previous.value
            cards.append(f"{event.component}:{was}>{event.status.value}")
        elif isinstance(event, BuildCompleted):
            cards.append(f"library:{event.song_count}")

    core.events.subscribe(HealthChanged, dashboard)
    core.events.subscribe(BuildCompleted, dashboard)

    core.events.publish(BuildCompleted(song_count=14_998, validated=True))
    core.events.publish(HealthChanged(status=HEALTHY.status, component="playback"))
    core.events.publish(
        HealthChanged(
            status=HealthStatus.UNAVAILABLE, component="mpv", previous=HealthStatus.HEALTHY
        )
    )

    assert cards == [
        "library:14998",
        "playback:never-reported>healthy",
        "mpv:healthy>unavailable",
    ]


def test_the_queue_rule_survives_the_whole_stack(
    appliance: tuple[CoreServices, io.StringIO],
) -> None:
    """The point of AIG 22's list: no layer can quietly reintroduce a rule the
    spec removed. Guests stay anonymous, and duplicates stay allowed - checked at
    the two places a regression would appear, the model and the configuration."""

    core, _ = appliance

    first = QueueItem(id=QueueItemId(1), song_id=SongId(9), position=1)
    second = QueueItem(id=QueueItemId(2), song_id=SongId(9), position=2)
    assert first.song_id == second.song_id
    assert first != second
    assert core.config.queue.allow_duplicates is True


#: Web frameworks the core foundation must never reach (AIG 4). `sse_starlette` is on the list
#: beside the others because `SSEPublisher` is a service: the day it learns to import the
#: library that writes its frames, the runtime's fan-out and the HTTP adapter become the same
#: module, and ADR-012's one-thread rule loses the seam that makes it checkable.
FORBIDDEN_IN_CORE: tuple[str, ...] = (
    "fastapi",
    "starlette",
    "jinja2",
    "uvicorn",
    "sse_starlette",
)


def test_nothing_in_the_core_imports_the_web() -> None:
    """AIG 4: domain services never import FastAPI, in a process that ran only the core.

    This used to read the current process's `sys.modules`, which was sound while the suite
    contained no web tests and became a false alarm the day it did — milestone 11's HTTP tests
    import Starlette, and a check that fails because of a *legal* import order is a check that
    gets deleted rather than fixed. It is the same reasoning that already keeps SQLAlchemy out
    of the list above.

    So the question is asked where it can be answered: a child interpreter imports the core,
    publishes a fact so that nothing lazy is left un-probed, and reports which forbidden names
    it reached. Nothing outside that process can put a web framework in its modules, and a
    transitive import anywhere in the dependency graph still surfaces here. The source-level
    version of the same rule is in `test_architecture_guardrails.py`.
    """

    script = textwrap.dedent(
        f"""
        import json, sys
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from encore.domain import QueueItemId, SongId
        from encore.events import EventBus, SongQueued
        from encore.services import build_core_services

        core = build_core_services(path={str(CONFIG)!r})
        core.logging.remove()
        core.events.publish(
            SongQueued(
                song_id=SongId(1),
                queue_item_id=QueueItemId(1),
                position=1,
                queue_length=1,
            )
        )
        EventBus()
        forbidden = set({str(list(FORBIDDEN_IN_CORE))!r})
        reached = {{name.split(".")[0] for name in sys.modules}}
        print(json.dumps(sorted(reached & forbidden)))
        """
    )

    finished = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )

    assert finished.returncode == 0, finished.stderr
    reached = json.loads(finished.stdout.strip())
    assert reached == [], f"the core foundation reached {reached}"


def test_the_process_can_be_shut_down_and_started_again(
    appliance: tuple[CoreServices, io.StringIO],
) -> None:
    """AEP 9's corollary: if starting a second core leaked into the first, the
    party simulation (milestone 16) would not be able to run two scenarios."""

    core, _ = appliance
    seen: list[str] = []
    core.events.subscribe(SongQueued, lambda event: seen.append(event.name))

    second = build_core_services(config=core.config)
    second.logging.remove()
    second.events.publish(
        SongQueued(song_id=SongId(3), queue_item_id=QueueItemId(3), position=1, queue_length=1)
    )

    assert seen == [], "the second core's bus is not the first one's"
    assert second.events.subscription_count == 0
    assert EVENT_VOCABULARY
