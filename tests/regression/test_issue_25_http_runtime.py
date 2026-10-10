"""Regression: #25 — the runtime layer: composition, HTTP, HTMX and SSE.

The defects below were all found the same way: by asking the running appliance for something
rather than asking a component about itself. Every one of them passed a unit test. That is worth
stating precisely, because the lesson is not "write more unit tests" — the queue's contract
tests, the publisher's, the templates' and the repositories' all existed and all were green
while `/queue` returned a 500 for a guest who had queued a song.

| Defect | Symptom |
| ------ | ------- |
| `views.queue_page` read `.item` off a `QueueItem` | the queue page worked empty and died at the first request |
| The confirmation's wording came from the player, not the item | a second press said "Playing now" about a song that had not started |
| `/artwork/*` answered "nothing is depicted" with a 404 | every row of a library without embedded covers drew a broken image |
| The SSE generator polled `request.is_disconnected()` | the poll competed with the response's own listener and could eat the disconnect, leaving a client registered forever |
| Error responses carried no `Cache-Control` | a proxy in front of the appliance could hold "the queue is full" in front of a guest who had already been served |
| `Accept: */*` counted as a browser | `curl` in a setup script received an HTML page from a JSON endpoint |
| The HTTPException handler was registered on FastAPI's subclass | a 405 arrived as `{"detail": …}`, the one failure that is raised by the router rather than by us |
| `create_app`'s lifespan started the worker thread but not the graph | every service read raised `ApplianceNotRunning` until the first fact happened to start something |
| `now_playing`'s progress bar published clock text as `aria-valuenow` | a screen reader announced an attribute value that was not a number |
| A health source read `runtime.current` and `search.available` | neither exists; the probe raised on the way to reporting health |
| `hx-swap="find .row-status"` in a row template | `find` is a target keyword, not a swap style: the row's confirmation would never appear, in a browser, quietly |

The first, the third and the ninth were invisible to a component test by construction: each is
a disagreement between two modules about a name, and a unit test of either module alone agrees
with itself. The fourth is a library's behaviour, not ours, and was found by a test that held a
connection open and then let go of it. The tenth was found by starting the real composition root
against a real database, which is the sentence `apps/server/appliance.py` exists to make cheap.

The eleventh was found while writing documentation for the vendored htmx: a sentence justified
an attribute by naming its feature, and the feature was in a different attribute. It is here
rather than only in `tests/unit/test_templates.py` because the unit file is the permanent guard
and this row is the record of what was believed until something was read carefully.

What each test below does is the same twice over: demonstrate the original failure, then the
behaviour that replaced it. A regression test that only asserts the new behaviour is a test of
the feature, and this file is for the ones the features already cover.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from html import unescape
from pathlib import Path
from typing import Any

import pytest

from apps.server.appliance import Appliance
from encore.api.app import create_app
from tests.support.appliances import composed_appliance, title_ids
from tests.support.http import Stream, running, started
from tests.support.mpv import FakeEngine

HTMX = {"hx-request": "true"}

#: The templates, as files. A markup check that renders them would only see the ones a Python
#: path reached, and the defect below was in a line no route test parsed.
TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "encore" / "templates"
ANY = {"accept": "*/*"}
BROWSER = {"accept": "text/html,application/xhtml+xml"}


@pytest.fixture
def appliance(encore_home: Path, built_library: Any) -> Iterator[Appliance]:
    """The real composition root, over the real Builder output (ADR-012's graph)."""

    made = composed_appliance(
        library_db=built_library.options.library_db,
        runtime_db=encore_home / "var/lib" / "runtime.db",
        artwork_dir=built_library.options.artwork_dir,
        music_dir=built_library.options.music_dir,
        temp_dir=encore_home / "var/cache/temp",
        launcher=FakeEngine(),
    )
    try:
        yield made
    finally:
        made.stop()
        made.close()


@pytest.fixture
def app(appliance: Appliance) -> Any:
    return create_app(appliance)


@pytest.fixture
def ids(built_library: Any) -> dict[str, int]:
    """Title → id, as the Builder numbered them, which is not the order they were written.

    Delegates to the shared lookup: the Builder numbers by scan order, and an integer hardcoded
    here would be a second copy of a fact `encore/repositories/library` owns.
    """

    return title_ids(built_library.options.library_db)


# -- the page that only worked when nothing was queued --------------------


async def test_the_queue_page_survives_its_first_song(app: Any, ids: dict[str, int]) -> None:
    """The original failure: an empty queue rendered, and item one raised.

    `views.queue_page` filtered `queue.active()` with `entry.item.status`, a shape belonging to
    `QueueEntry` rather than to the raw `QueueItem` that method returns. With no items the
    comprehension never evaluated the attribute, so every page test that had ever run — all of
    them against an empty queue — passed.

    A 500 on the page whose subject is "did my song make it" is the worst possible place in the
    interface to be right only when nothing has happened (SAPRS 9.9).
    """

    async with running(app) as client:
        empty = await client.get("/queue")
        queued = await client.post("/api/v1/queue", params={"song_id": ids["First Album Track 1"]})
        page = await client.get("/queue")

    assert empty.status_code == 200
    assert queued.status_code == 201
    assert page.status_code == 200
    assert "First Album Track 1" in unescape(page.text)


async def test_a_second_press_says_added_and_not_playing(app: Any, ids: dict[str, int]) -> None:
    """The answer used to come from the room. SAPRS 9.7 wants it from the item.

    `view.queue_state` describes the speakers, and while the first song played the second
    request's confirmation read "Playing now" — a sentence about a different song. The queue
    knows which item it made the head (ADR-011); the handler now asks it.

    The wording is not decoration. A guest who is told "playing now" and hears the previous
    track concludes the jukebox is broken, and there is no screen left that says otherwise.
    """

    song = ids["First Album Track 2"]
    async with running(app) as client:
        first = await client.post("/queue", data={"song_id": str(song)}, headers=HTMX)
        second = await client.post("/queue", data={"song_id": str(song)}, headers=HTMX)

    assert "Playing now" in unescape(first.text)
    assert "Playing now" not in unescape(second.text)
    assert "Added at 2" in unescape(second.text)


# -- artwork, which is allowed to be missing ------------------------------


async def test_a_library_with_no_covers_has_no_broken_images(app: Any, ids: dict[str, int]) -> None:
    """The 404 was correct per request and wrong per page.

    The endpoint refused any miss, and a row's `<img>` asks whether or not the library names a
    picture, so an album with no embedded art rendered as a broken image icon in every track
    listing. SAPRS 6.7's sentence — bad artwork is a missing picture, never a missing track — is
    about exactly this difference.

    Nothing depicted is a placeholder now. What the test pins is the *status*: a page full of
    404s looks healthy to every monitor in the suite and is not.
    """

    bare = ids["album:Second Album"]  # `music_tree` gives only First Album a cover
    async with running(app) as client:
        response = await client.get(f"/artwork/album/{bare}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert b"<svg" in response.content


async def test_a_cache_that_lost_a_file_still_says_so(
    app: Any, ids: dict[str, int], built_library: Any
) -> None:
    """The other half of the split, and the one that keeps the first honest.

    A row that names an artwork the cache no longer holds is a deleted directory or a library
    copied without its pictures — a fault, and one a placeholder would hide completely. The
    distinction between the two misses is the whole design of the endpoint, so both halves are
    asserted here or the first test proves nothing.
    """

    covered = ids["album:First Album"]
    files = list((built_library.options.artwork_dir).glob("**/*.jpg"))
    assert files, "the fixture library lost the cover this test depends on"
    for path in files:
        path.unlink()

    async with running(app) as client:
        response = await client.get(f"/artwork/album/{covered}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# -- a connection that goes away -------------------------------------------


async def test_a_browser_that_lets_go_is_forgotten(app: Any, appliance: Appliance) -> None:
    """The generator used to poll `request.is_disconnected()`.

    `sse-starlette` runs a listener of its own for exactly that message, and two readers of one
    channel means the poll can take the disconnect and the response never ends — a `Stream`
    with a queue of rendered panels, registered, for the rest of the night. Forty phones that
    wander out of range is forty leaked queues (SAPRS 8.6, 9.10).

    The test holds a connection, hangs it up, and asks the publisher how many clients it thinks
    are in the room. The answer has to be zero without anyone being asked to wait.
    """

    async with started(app):
        stream = Stream(app, "/events")
        await stream.next(1)
        assert appliance.live.clients == 1
        await stream.close()

    assert appliance.live.clients == 0


async def test_one_stalled_phone_does_not_stop_the_room(
    app: Any, appliance: Appliance, ids: dict[str, int]
) -> None:
    """A client that stops reading must not be a tick that stops running.

    The bridge between the appliance thread and a browser's queue is `call_soon_threadsafe`,
    which never blocks the caller — and the case worth guarding is the one where it could have:
    a phone at the far edge of the Wi-Fi, its window full, while the party carries on. The tick
    is what advances the queue, so a stalled reader that reached it would be a song that never
    ends.
    """

    async with started(app) as client:
        stream = Stream(app, "/events")
        await stream.next(1)
        stream.pause()
        await client.post("/api/v1/queue", params={"song_id": ids["First Album Track 1"]})
        await asyncio.sleep(0.2)  # several ticks, with nobody reading this socket
        await stream.resume()
        events = [event for event, _ in await stream.next(6)]
        await stream.close()

    assert "SongQueued" in events
    assert appliance.live.clients == 0


# -- the shape of a failure -----------------------------------------------


async def test_a_failure_is_never_cacheable(app: Any) -> None:
    """All three error faces carry `no-store`.

    The rule is SAPRS 10.10's and applies to a failure exactly as it applies to a queue: the
    state that produced it has already changed by the time the response arrives. A proxy that
    kept "the queue is full" would serve it to the next guest, whose queue has three songs in it
    and no explanation for the message.

    Three faces because the mapping chooses between them by header, and a header added to one
    branch and not the others is the way this regressions reads as "mostly fixed".
    """

    async with running(app) as client:
        missing = await client.get("/api/v1/songs/999999", headers=ANY)
        fragment = await client.get("/albums/999999", headers=HTMX)
        page = await client.get("/albums/999999", headers=BROWSER)

    for response in (missing, fragment, page):
        assert response.status_code == 404
        assert "no-store" in response.headers["cache-control"], response.headers


async def test_a_tool_that_asks_for_anything_is_answered_in_json(app: Any) -> None:
    """`Accept: */*` used to count as a browser.

    It does not: a browser names `text/html` first because it is about to render it, and
    anything that says "anything" is a script. `curl -f` in a provisioning check that receives a
    styled page for its `404` is the failure this line prevents, and it was in the first cut of
    the error mapping (SAPRS 10.6's three audiences, minus one).
    """

    async with running(app) as client:
        response = await client.get("/api/v1/health", headers=ANY)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")


async def test_a_route_that_does_not_answer_a_method_still_uses_the_envelope(app: Any) -> None:
    """The 405 comes from the router, and the handler was registered on the wrong class.

    Registering `fastapi.HTTPException` misses `MethodNotAllowed`, which inherits from
    Starlette's — so the one failure a guest can cause by reloading at the wrong moment arrived
    in a shape no other error uses. SAPRS 10.7's premise is that an error is branchable; a
    second grammar is the way that premise dies.
    """

    async with running(app) as client:
        response = await client.delete("/", headers=ANY)

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


# -- being ready, and saying so -------------------------------------------


async def test_the_appliance_is_ready_before_anyone_asks(app: Any, appliance: Appliance) -> None:
    """Startup used to start the thread and nothing that runs on it.

    The difference is invisible until a request arrives: the queue had not subscribed, the
    engine had not launched, and the first thing each guest did was a read of a graph that was
    composed but not begun. The lifespan now calls `jukebox.start()`, which begins the graph on
    the thread (ADR-012) — so the state a browser sees on its first request is the state the
    appliance is in, not the state a first request would have to provoke.
    """

    async with running(app) as client:
        playing = await client.get("/api/v1/playing")
        queue = await client.get("/api/v1/queue")

    assert playing.status_code == 200
    assert queue.status_code == 200
    assert appliance.queue.length == queue.json()["length"]


async def test_a_status_a_screen_reader_can_read(app: Any, ids: dict[str, int]) -> None:
    """`aria-valuenow` held clock text ("1:23"), because the template reused a filter.

    A progress bar's value has to be a number against a maximum, and the strings that look right
    under a thumb are not what an assistive technology is told (SAPRS 9.2's accessibility
    requirements are the reason the panel has these attributes at all). The three numeric
    attributes are seconds; the words stay in `aria-valuetext`, which is the attribute for them.
    """

    async with running(app) as client:
        await client.post("/api/v1/queue", params={"song_id": ids["First Album Track 1"]})
        panel = await client.get("/fragments/now-playing")

    numbers = dict(re.findall(r'aria-value(min|max|now)="([^"]*)"', panel.text))

    assert set(numbers) == {"min", "max", "now"}, numbers
    assert all(value.isdigit() for value in numbers.values()), numbers


async def test_the_health_strip_names_the_thing_that_hurt(appliance: Appliance) -> None:
    """A probe raised on its way to reporting health, and health said nothing.

    The runtime source read `runtime.current` and the search source `search.available`; neither
    name exists, and an exception inside a source is the one failure mode a health check must
    not have — it is the component that is meant to be reporting. The verdict is now
    `unavailable` with a sentence, which is what SAPRS 11.7 asks for and what an operator can
    act on.
    """

    appliance.start()
    try:
        snapshot = await started_read(appliance)
    finally:
        appliance.stop()

    assert snapshot.status.value in {"healthy", "degraded"}
    assert {component.component for component in snapshot.components} == {
        "playback",
        "library",
        "runtime",
        "search",
    }


async def started_read(appliance: Appliance) -> Any:
    """One health check, on the thread that owns the components."""

    from encore.utilities.appliance import call_async

    return await call_async(appliance.appliance, appliance.health.check)


# -- the markup a browser would have quietly ignored ----------------------


def test_the_rows_feedback_lands_somewhere_htmx_understands() -> None:
    """`find` is real, and it is not a swap style.

    `hx-swap="find .row-status"` reads like the htmx it imitates: a selector, a word, and the
    intent — update one line of feedback beside the button — is a sensible thing to want. htmx's
    dispatcher has a fixed list of styles, `find` is not among them, and what it does with an
    unknown one is call any registered extension strategy, find none, and swap nothing. The
    button would still queue the song; the row would go quiet.

    No test in this repository could have noticed by running the template, because no test
    parses htmx. The check is on the vocabulary instead, and it is in
    `tests/unit/test_templates.py` for every file; this one is here because it was a defect.
    """

    row = (TEMPLATES_DIR / "partials/song_row.html").read_text(encoding="utf-8")

    assert 'hx-swap="innerHTML"' in row, row
    assert 'hx-target="find .row-status"' in row, row
    assert 'hx-swap="find' not in row, "`find` belongs to hx-target, never to hx-swap"
