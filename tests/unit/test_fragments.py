"""Which fact redraws which region, and how many times (SAPRS 9.10, ADR-004, ADR-012).

The table in `encore/api/fragments.py` is the whole of the SSE-to-screen policy, so it is
worth reading as a test would: against the vocabulary it is derived from, against the
templates it names, and against the number of renders a party of forty costs.

Three assertions carry the design:

* **Every fact has a considered answer.** `EVENT_REGIONS` is derived from
  `EVENT_VOCABULARY`, so a proposed ninth fact arrives with an empty region list — visible in
  a diff and deliberate, rather than absent and forgotten.
* **A fact costs one render, not one per client.** `push` reads the appliance once and
  renders per invalidated region. The counter in this test is the difference between a
  party of forty and forty templates, forty JSON serialisations and one socket write each.
* **A region that cannot render does not stop the bus.** SAPRS 11.9's failure isolation is
  not only about the network: a broken panel must not be a song that stops.

The renderer is a stub, and that is the right level: what is under test is the mapping and
the fan-out, not the markup, which `tests/integration/test_htmx_pages.py` renders for real.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from encore.api import fragments
from encore.api.html import Renderer
from encore.domain import PlaybackState, QueueItemId, SongId
from encore.domain.health import HealthStatus
from encore.events import (
    EVENT_VOCABULARY,
    EventBus,
    HealthChanged,
    LibraryReloaded,
    QueueAdvanced,
    SongQueued,
    SongStarted,
)
from encore.services.sse_publisher import SSEPublisher


class CountingRenderer(Renderer):
    """A real `Renderer` that also remembers what it was asked to draw.

    Subclassing rather than duck-typing keeps the production template environment behind it,
    so a fragment that a test never renders still has to resolve. The markup is irrelevant
    here and the call count is the point: `push` could render the player panel once per
    listening client, which would be correct and forty times the work.
    """

    def __init__(self, *, fail_for: str | None = None) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.fail_for = fail_for

    def render(self, name: str, context: Mapping[str, Any]) -> str:
        self.calls.append(name)
        if self.fail_for and name.endswith(self.fail_for):
            raise RuntimeError("a broken panel")
        return f"<{name}:{len(context)}>"


def jukebox(*, renderer: CountingRenderer | None = None) -> Any:
    """A `Jukebox`-shaped object with a real publisher and no services (ADR-012's seam).

    `views.player` is monkeypatched by the tests that need it, so the graph below is
    deliberately incomplete rather than fake: a stub of `QueueService` here would let a
    fragment test pass while the real read was wrong, and that combination is what the
    integration file exists to catch.
    """

    bus = EventBus(logger=logging.getLogger("encore.test.fragments"))
    live = SSEPublisher(events=bus)
    # `start()` is deliberately not called. The publisher's own subscriptions would send
    # every fact as a frame alongside the region frames, which is true in production and
    # would make the counts below about the wrong module: the fact-to-frame mapping is
    # `test_sse_publisher.py`'s subject, and this file's is the region mapping.

    made = SimpleNamespace(
        events=bus,
        live=live,
        logger=logging.getLogger("encore.test.fragments"),
        health=SimpleNamespace(
            snapshot=SimpleNamespace(
                status=HealthStatus.HEALTHY,
                detail="",
                components=(),
                is_ready=True,
            )
        ),
        config=SimpleNamespace(server=SimpleNamespace(domain="jukebox.home.arpa")),
    )
    return made, live


# -- the table ------------------------------------------------------------


def test_every_documented_fact_has_a_considered_region() -> None:
    """The eight facts of AIG 8, each with a region list — including the empty ones.

    `BuildCompleted` and `SongQueued` are deliberately unwired: the first is a Builder's
    fact that a guest's screen has no part in, and the second is answered by the POST that
    caused it. An empty tuple here is a decision; a missing key would be an accident, and
    this assertion is what keeps the two apart.
    """

    wired = {event.__name__: regions for event, regions in fragments.EVENT_REGIONS.items()}

    assert set(wired) == {event.__name__ for event in EVENT_VOCABULARY}
    assert wired["SongStarted"] == (fragments.REGION_PLAYER,)
    assert wired["SongFinished"] == (fragments.REGION_PLAYER,)
    assert wired["QueueAdvanced"] == (fragments.REGION_PLAYER,)
    assert wired["PlaybackRecovered"] == (fragments.REGION_ALERTS,)
    assert wired["HealthChanged"] == (fragments.REGION_ALERTS,)
    assert wired["SongQueued"] == ()
    assert wired["BuildCompleted"] == ()


def test_every_region_names_a_template_that_exists() -> None:
    """A typo in this table is a panel that never updates, in production, silently.

    The templates are the ones in `encore/templates/panels/`, and the files are checked
    rather than the strings because a rename is a change in two places that no type checker
    will notice.
    """

    from encore.api.html import TEMPLATES_DIR

    for region, template in fragments.REGION_TEMPLATES.items():
        path = TEMPLATES_DIR / template
        assert path.is_file(), f"{region} points at {template}, which is not there"


def test_a_frame_is_named_after_the_region_not_the_fact() -> None:
    """A browser subscribes to "the panel changed", not to a verb in the domain.

    Which facts redraw the panel is the appliance's business and changes with the
    presentation; which frame a client listens to should not.
    """

    assert fragments.swap_frame(fragments.REGION_PLAYER) == "swap:player"
    assert fragments.swap_frame(fragments.REGION_ALERTS) == "swap:alerts"


# -- the fan-out ----------------------------------------------------------


def test_one_fact_costs_one_render_however_many_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason this module exists rather than a loop over responses.

    Forty phones, one `SongStarted`, one read of the appliance and one template render. A
    `for client in clients: render(...)` would be indistinguishable in a functional test and
    is the difference between a Pi keeping up and not.
    """

    made, live = jukebox()
    renderer = CountingRenderer()
    streams = [live.connect(loop=_Loop()) for _ in range(40)]
    views = _fake_player(monkeypatch, made)

    fragments.push(made, renderer, SongStarted(song_id=SongId(3), queue_item_id=QueueItemId(1)))

    assert renderer.calls == [fragments.REGION_TEMPLATES[fragments.REGION_PLAYER]]
    assert len(views.reads) == 1, "one read of the appliance for forty clients"
    for stream in streams:
        assert stream.pending == 1, "one region frame per client, from one render"


def test_a_fact_that_touches_two_regions_renders_each_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`LibraryReloaded` changes what is playing and what is worth saying.

    Two renders, one read: the appliance's state is asked for once per fact, because two
    reads of a queue that can change between them is how a panel shows a song that already
    ended (ADR-012's reason for one thread, applied to one template).
    """

    made, _live = jukebox()
    renderer = CountingRenderer()
    _fake_player(monkeypatch, made)

    fragments.push(made, renderer, LibraryReloaded(song_count=12, version="2"))

    assert renderer.calls == [
        fragments.REGION_TEMPLATES[fragments.REGION_PLAYER],
        fragments.REGION_TEMPLATES[fragments.REGION_ALERTS],
    ]


def test_a_fact_with_no_region_renders_nothing() -> None:
    """`SongQueued` is answered in the POST, not here.

    The guest who asked gets their confirmation in the response they are already waiting
    for; everyone else's Up Next changes when the queue actually advances, which is the fact
    that made it true (ADR-011).
    """

    made, _ = jukebox()
    renderer = CountingRenderer()

    fragments.push(
        made,
        renderer,
        SongQueued(song_id=SongId(1), queue_item_id=QueueItemId(1), position=2, queue_length=5),
    )

    assert renderer.calls == []


def test_a_broken_panel_is_reported_and_the_facts_keep_flowing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """SAPRS 11.9's isolation, at the presentation tier.

    A template that raises must not end a song. The exception is logged and the machine
    clients still get their facts, because a party does not stop for markup.
    """

    made, live = jukebox()
    renderer = CountingRenderer(fail_for="player.html")
    _fake_player(monkeypatch, made)
    stream = live.connect(loop=_Loop())

    with caplog.at_level(logging.ERROR, logger="encore.test.fragments"):
        fragments.push(made, renderer, SongStarted(song_id=SongId(1), queue_item_id=QueueItemId(1)))

    assert renderer.calls, "the region was attempted"
    assert stream.pending == 0, "nothing was sent for a region that could not render"
    assert "fragment render failed" in caplog.text


def test_subscribe_registers_one_handler_per_wired_fact() -> None:
    """The subscriptions come back so shutdown can withdraw them.

    A handler left registered outlives the app that made it, and the next `create_app` in
    the same process would render into a renderer nobody owns. `test_a_subscription_can_be_
    withdrawn` is the other half of that sentence.
    """

    made, _live = jukebox()
    renderer = CountingRenderer()

    subscriptions = fragments.subscribe(made, renderer)

    wired = sum(1 for regions in fragments.EVENT_REGIONS.values() if regions)
    assert len(subscriptions) == wired
    for subscription in subscriptions:
        subscription.unsubscribe()


def test_a_subscribed_fact_reaches_the_stream_it_is_wired_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end of the chain: a bus publication on one thread, HTML in a client's queue.

    This is the ADR-012 boundary in miniature — the handler runs inside `publish`, renders,
    and hands the frame to `SSEPublisher`, which is the only thing in the path that knows
    about loops.
    """

    made, live = jukebox()
    renderer = CountingRenderer()
    _fake_player(monkeypatch, made)
    stream = live.connect(loop=_Loop())
    subscriptions = fragments.subscribe(made, renderer)

    made.events.publish(
        QueueAdvanced(finished_queue_item_id=None, now_playing=None, queue_length=2)
    )

    (event, data) = _take(stream)
    assert event == "swap:player"
    assert data.startswith("<")
    for subscription in subscriptions:
        subscription.unsubscribe()


def test_a_withdrawn_subscription_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shutdown's side of the same promise (SAPRS 11.8's step 2)."""

    made, live = jukebox()
    renderer = CountingRenderer()
    _fake_player(monkeypatch, made)
    stream = live.connect(loop=_Loop())
    subscriptions = fragments.subscribe(made, renderer)
    for subscription in subscriptions:
        subscription.unsubscribe()

    made.events.publish(SongStarted(song_id=SongId(1), queue_item_id=QueueItemId(1)))

    assert stream.pending == 0
    assert renderer.calls == []


def test_swap_headers_ask_for_an_out_of_band_update() -> None:
    """One action, two panels, one round trip (SAPRS 10.6).

    A guest's tap changes their own row and everybody else's list. The headers are how the
    response says "and redraw that other thing too", which on a phone three rooms from the
    access point is the difference between one request and three.
    """

    headers = fragments.swap_headers([fragments.REGION_PLAYER])

    assert headers["HX-Retarget"] == "body"
    assert headers["HX-Reswap"] == "outerHTML"
    assert "no-store" in headers["Cache-Control"]


def test_a_health_change_renders_the_alerts_region_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A degraded box is a sentence, not a new song.

    The player panel is untouched: an appliance whose mpv has restarted twice still shows
    the track it is on, and SAPRS 9.8's "update without a reload" is why the strip is a
    separate region rather than a banner inside the panel.
    """

    made, _live = jukebox()
    renderer = CountingRenderer()
    reads: list[int] = []
    monkeypatch.setattr(fragments, "views", _Views(reads))

    fragments.push(
        made,
        renderer,
        HealthChanged(
            status=HealthStatus.DEGRADED,
            previous=HealthStatus.HEALTHY,
            component="playback",
            detail="mpv restarted",
        ),
    )

    assert renderer.calls == [fragments.REGION_TEMPLATES[fragments.REGION_ALERTS]]
    assert reads == [], "the strip is drawn from the fact, not from a second read"


class _Loop:
    """A stand-in for a loop that only ever needs `call_soon_threadsafe` to be inert.

    Every client in these tests is on the same thread as the publication, so the frame lands
    in the queue synchronously; the real cross-thread behaviour is
    `tests/unit/test_sse_publisher.py`'s and the real HTTP stream is
    `tests/integration/test_sse_stream.py`'s.
    """

    def call_soon_threadsafe(self, function: Any, *args: object) -> None:
        function(*args)


def _take(stream: Any) -> tuple[str, str]:
    frame = stream._queue.popleft()
    return frame.event, frame.data


class _Views:
    """A stand-in for `encore.api.views`, counting reads of the appliance.

    A module attribute rather than a function, because `fragments` reaches it as `views.player`
    and a test that patched only the function would leave the module object — and therefore the
    next test — with the production read still in place.
    """

    def __init__(self, reads: list[int]) -> None:
        self._reads = reads

    def player(self, _jukebox: Any) -> Any:
        self._reads.append(1)
        return _view()


def _view() -> Any:
    from encore.api.views import PlayerView

    return PlayerView(
        state=PlaybackState.PLAYING,
        now=None,
        up_next=[],
        length=0,
        max_items=200,
        wait=timedelta(0),
        position=timedelta(0),
        duration=timedelta(0),
        song_id=None,
    )


def _fake_player(monkeypatch: pytest.MonkeyPatch, made: Any) -> SimpleNamespace:
    """One read of the appliance, counted.

    `fragments.push` should ask for the player view at most once per fact; a second read
    would be a second answer, and the two regions would be describing different moments.
    """

    reads: list[int] = []

    monkeypatch.setattr(fragments, "views", _Views(reads))
    return SimpleNamespace(reads=reads, made=made)
