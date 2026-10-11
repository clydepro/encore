"""HTTP and SSE latency, at the seam a guest waits on (SAPRS 1.8, AIG 19).

Two budgets milestone 11 made measurable: **a page a phone asks for** and **a fact a phone is
told about**. Both are measured against the running composition root — the real routes, the
real templates, the real event bus, the real appliance thread (ADR-012) — because the cost
these budgets exist to catch is not SQLite's or Jinja's but the one each layer adds to the
other: a view that reads the queue twice per request, a panel rendered per listener instead of
per fact, a bridge between threads that waits on the wrong thing.

**What these numbers are not.** There is no socket, no TLS and no uvicorn HTTP parser: the
requests go to the ASGI app in-process, so what is timed is Encore's own work and not the
kernel's. The reference figure on a Pi 4 will be higher than the figure here by whatever a
handshake and a `send()` cost, which is why the budgets are the SAPRS's rather than these
tests' — a test that invented a tighter number would only teach someone to loosen it. mpv is
`tests/support/mpv.py`'s double (SAPRS 14.4), so playback latency stays out of these two by
design; it has its own budget in `test_runtime_latency.py`.

The SSE sample is the one to read carefully: a propagation budget of one second is a *room*
budget, and the measurement includes the client's own read. A frame that arrives in 40 ms here
is a frame that arrives in a second on a phone at the edge of the Wi-Fi, and the difference is
not something this file can tell you — only that the appliance was not the reason.

Run with `--run-slow`; a tracer invalidates every figure (see `tests/support/latency.py`).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from apps.server.appliance import Appliance
from encore.domain import QueueItemId, SongId
from encore.events import SongQueued
from tests.support.appliances import composed_appliance
from tests.support.http import Stream, started
from tests.support.latency import p95, require_untraced, timed_async
from tests.support.mpv import FakeEngine

pytestmark = [pytest.mark.slow, pytest.mark.performance]

#: What the operator sets on the first-run page, once, before the samples are taken. A
#: credential in `runtime.db` costs one bcrypt verification at sign-in and nothing afterwards.
OPERATOR_PASSWORD = "a password long enough to satisfy the rule"  # pragma: allowlist secret

#: Samples per budget. A page render is two orders of magnitude more expensive than a queue
#: append, so a hundred of them is a hundred seconds of patience nobody has; 100 is also
#: enough for a nearest-rank p95 to mean something.
REQUESTS = 100


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def appliance(encore_home: Path, built_library: Any, engine: FakeEngine) -> Iterator[Appliance]:
    """The server as `main.py` starts it, over the fixture library.

    `queue.max_items` is the shipped 200 rather than the 8 the other rigs use: a page render
    sampled against a queue that refuses at item nine would be measuring the refusal.
    """

    made = composed_appliance(
        library_db=built_library.options.library_db,
        runtime_db=encore_home / "var/lib" / "runtime.db",
        artwork_dir=built_library.options.artwork_dir,
        music_dir=built_library.options.music_dir,
        temp_dir=encore_home / "var/cache/temp",
        max_items=200,
        launcher=engine,
    )
    try:
        yield made
    finally:
        made.stop()
        made.close()


@pytest.fixture
def app(appliance: Appliance) -> Any:
    from encore.api.app import create_app

    return create_app(appliance)


# -- a page: p95 < 200 ms (SAPRS 1.8, "HTMX navigation") ------------------


@pytest.mark.parametrize(
    ("path", "describe"),
    [
        ("/", "the home page, which is a shell plus the whole player panel"),
        ("/search?q=track", "a search, which is the FTS5 query and then a page of rows"),
        ("/albums/1", "an album, which is every track on it, labelled"),
        ("/fragments/up-next", "a fragment alone, which is what an HTMX swap costs"),
    ],
)
async def test_a_page_meets_its_navigation_budget(app: Any, path: str, describe: str) -> None:
    """SAPRS 1.8's 200 ms, at the four requests a guest actually makes.

    The parametrised cases are not redundant with each other: the home page is the shell's
    worst case (it renders the player *and* the nav), search is the only route that runs a
    query it did not write, an album is the widest row listing, and the fragment is the floor
    everything else pays above. A single number for "navigation" would hide which one moved.

    Where the numbers came from: on the reference machine, untraced, this reads 9.6 ms p95 for
    the home page, 13.6 ms for a search and 9.5 ms for a bare fragment, against a 200 ms
    budget. Twenty-fold headroom is not the point — the point is that a change which added a
    query per row would land here rather than in a party, and that a Pi 4's share of the
    budget (the socket, the TLS, the radio) is not in this number either.
    """

    require_untraced()
    async with started(app) as client:

        async def fetch() -> Any:
            return await client.get(path)

        timings = await timed_async(fetch, repeats=REQUESTS)

    worst = max(timings)
    assert p95(timings) < 200.0, f"{describe}: p95 {p95(timings):.1f} ms"
    assert worst < 600.0, f"{describe}: worst of {len(timings)} was {worst:.1f} ms"


# -- an administrative page: the same 200 ms, plus a session (SAPRS 1.8, AIG 14) ----------


@pytest.mark.parametrize(
    ("path", "describe"),
    [
        (
            "/admin/dashboard",
            "the dashboard, which is the player panel plus queue rows plus health",
        ),
        ("/admin/queue", "the queue page, which is every waiting request"),
        ("/admin/statistics", "statistics, which is counters, a trend and a leaderboard"),
        ("/admin/logs", "the log tail, which is two hundred rendered records"),
    ],
)
async def test_an_operators_page_meets_the_same_budget(
    app: Any, appliance: Appliance, path: str, describe: str
) -> None:
    """SAPRS 1.8's navigation budget, with no separate line for the administrative interface.

    The budget is the same number on purpose: a page an operator opens is a page, and a second
    constant would be a temptation to let the admin surface be slower than the guest's — which
    is the wrong direction, since the pages an operator opens are the ones with the most reads
    behind them. Four of the ten screens are sampled; they are the four that read more than one
    service.

    Two costs the guest's pages do not pay are inside every sample here, and both are worth
    naming because each was a decision rather than an accident:

    * **The session lookup**, which is a dictionary probe in `AdminService` and deliberately not
      a read of `runtime.db` — a hash verification per request would have put bcrypt in the
      middle of the p95 (ADR-013).
    * **The refresh that follows an action**, which is an HTMX `GET` of one panel rather than a
      poll: no administrative page asks for itself on a timer, so nothing here is measured twice
      per second for a room nobody is looking at.

    Where the numbers came from: on the reference machine, untraced, this reads 16.5 ms p95 for
    the dashboard, 13.7 ms for statistics, 10.7 ms for the queue page and 5.3 ms for the log
    tail, against a 200 ms budget — the same order as the guest's pages, which is the reason for
    measuring them at all rather than assuming it. The log tail is the cheapest of the four
    because it reads a ring buffer instead of a database, and the dashboard the dearest because
    it reads the player, the queue and the health aggregate. A change that added a query per row
    would land here before it landed in a party, and a Pi 4's share of the budget (the socket,
    the radio) is not in these numbers either.
    """

    require_untraced()
    async with started(app) as client:
        await client.post("/admin/login", data={"password": OPERATOR_PASSWORD})

        async def fetch() -> Any:
            return await client.get(path, headers={"accept": "text/html"})

        timings = await timed_async(fetch, repeats=REQUESTS)

    worst = max(timings)
    assert p95(timings) < 200.0, f"{describe}: p95 {p95(timings):.1f} ms"
    assert worst < 600.0, f"{describe}: worst of {len(timings)} was {worst:.1f} ms"
    assert client.cookies.get("encore_admin"), "every sample was authenticated, not redirected"


async def test_a_guests_own_request_carries_its_confirmation(app: Any) -> None:
    """The tap, timed as the tap.

    This is the only round trip in the guest interface that *writes*, and the one a guest is
    standing there waiting for: the confirmation has to come back fast enough that a second
    press is not an accident. Everything after it — the other thirty phones — is the SSE
    budget's business, measured below.

    The most expensive request in the suite, at ~35 ms p95 and a 143 ms worst sample on the
    reference machine: it is a write, a renumbering, a song lookup, an mpv command and three
    template renders, and its tail is the WAL checkpoint `test_runtime_latency.py` already
    blames for the queue's own tail. Same cause, same fix, and neither budget is the place to
    argue about it.
    """

    require_untraced()
    song = await _first_song(app)
    async with started(app) as client:
        index = 0

        async def enqueue() -> Any:
            nonlocal index
            index += 1
            return await client.post(
                "/queue", data={"song_id": str(song + (index % 3))}, headers={"hx-request": "true"}
            )

        timings = await timed_async(enqueue, repeats=REQUESTS)

    assert p95(timings) < 200.0, f"queue POST p95 {p95(timings):.1f} ms"


# -- a fact reaching a screen: p95 < 1 s (SAPRS 1.8, "SSE propagation") --


async def test_a_fact_reaches_a_listening_client_inside_its_budget(app: Any) -> None:
    """One publish, one frame, one socket: the whole of SAPRS 1.8's SSE number.

    The measurement starts on the appliance thread's side of the bridge and ends where a
    browser would read it, so it includes the `call_soon_threadsafe`, the per-client queue, the
    generator's wake-up and this task's own scheduling. Those are the four steps a refactor can
    add a wait to, and the reason the budget is a second rather than the few milliseconds a
    direct call would take.

    0.83 ms p95, 1.0 ms worst, on the reference machine — three orders of magnitude inside the
    budget, which is what makes the next test worth writing: the number that matters is not the
    first client's latency but how much of it thirty-nine more clients add.
    """

    require_untraced()
    async with started(app):
        stream = Stream(app, "/events")
        try:
            await stream.next(1)  # the snapshot, which is a baseline rather than a fact
            timings: list[float] = []
            for index in range(REQUESTS):
                started_at = time.perf_counter()
                await _publish(app, index)
                await stream.next(1)
                timings.append((time.perf_counter() - started_at) * 1_000.0)
        finally:
            await stream.close()

    p95_ms = p95(timings)
    assert p95_ms < 1_000.0, f"sse propagation p95 {p95_ms:.1f} ms"
    assert len(timings) == REQUESTS


async def test_the_room_scales_to_forty_screens(app: Any) -> None:
    """SAPRS 8.6's number: forty devices, one fact.

    The budget is the same second, because the budget is a guest's; what this test is watching
    for is the cost of the *fan-out*, which is where a per-client render would show up as a line
    with a slope in it. Forty streams is also the point where an `assert stream.pending` in a
    unit test stops describing a party, so the measurement is made with all of them open.

    8.7 ms for one fact and forty screens, untraced: 0.2 ms per additional screen. The slope is
    the number to watch, not the total — a per-client template render would put it nearer 40 ms
    per screen and the first test in this pair would not move at all.
    """

    require_untraced()
    screens = 40  # SAPRS 8.6's stated party
    async with started(app):
        streams = [Stream(app, "/events") for _ in range(screens)]
        try:
            await asyncio.gather(*(stream.next(1) for stream in streams))
            started_at = time.perf_counter()
            await _publish(app, 0)
            await asyncio.gather(*(stream.next(1) for stream in streams))
            elapsed = (time.perf_counter() - started_at) * 1_000.0
        finally:
            await asyncio.gather(*(stream.close() for stream in streams))

    assert elapsed < 1_000.0, f"{screens} screens, one fact: {elapsed:.1f} ms"


# -- the machinery --------------------------------------------------------


async def _first_song(app: Any) -> int:
    """Any track id, taken from the API rather than assumed.

    The Builder numbers albums in scan order, which is a detail of a different module and a
    thing this file has no business knowing.
    """

    async with started(app) as client:
        found = await client.get("/api/v1/search", params={"q": "track", "limit": 1})
        songs = found.json()["songs"]
    assert songs, "a corpus this file cannot search would make the numbers below meaningless"
    return int(songs[0]["id"])


async def _publish(app: Any, index: int) -> None:
    """One fact, on the appliance thread, as the tick and every POST would publish it.

    Going through `QueueService.enqueue` would measure the queue as well, and the queue has its
    own budget two files away; what is under test here is the bridge from a fact to a frame.
    """

    from encore.utilities.appliance import call_async

    await call_async(
        app.state.jukebox.appliance,
        lambda: app.state.jukebox.events.publish(
            SongQueued(
                song_id=SongId(1 + index % 7),
                queue_item_id=QueueItemId(1 + index),
                position=2,
                queue_length=2,
            )
        ),
    )
