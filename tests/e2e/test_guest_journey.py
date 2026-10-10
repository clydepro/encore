"""A guest arrives on a phone, asks for a song, and the room changes (SAPRS 9, AIG 18).

This file is the one place that reads the product the way a party does: a browser GET, an HTMX
POST from the same button, a long-lived `/events` connection that redraws two regions, and an
mpv that is a double because no test server has speakers (SAPRS 14.4). Everything below the
HTTP layer is the shipped composition root, so the graph being driven is the graph
`apps/server/appliance.py` builds on a Raspberry Pi — which is the difference between this file
and `tests/integration/test_server_app.py`, where the assertions are about individual routes.

Three things are deliberately *not* here:

* **Admin** — milestone 14, and the guest surface has no session to borrow.
* **QR codes** — also milestone 14's installer work.
* **Real audio** — `tests/integration/test_real_mpv.py` owns that, slow and opt-in.

The queue ceiling is 6, from `composed_appliance`'s non-default configuration: a journey that
fills the room is a journey that ends at the policy, and the policy's number is configuration
(SAPRS 12.1), not a constant.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from html import unescape
from pathlib import Path
from typing import Any

import pytest

from apps.server.appliance import Appliance
from encore.api.app import create_app
from encore.utilities.appliance import call_async
from tests.support.appliances import composed_appliance, title_ids
from tests.support.http import Stream, started
from tests.support.mpv import FakeEngine

pytestmark = pytest.mark.e2e

HTMX = {"hx-request": "true"}
BROWSER = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


@pytest.fixture
def engine() -> FakeEngine:
    """The mpv double, kept by the test so it can say what reached the speakers."""

    return FakeEngine()


@pytest.fixture
def appliance(encore_home: Path, built_library: Any, engine: FakeEngine) -> Iterator[Appliance]:
    """The real graph: real Builder output, real SQLite, fake engine, small room."""

    made = composed_appliance(
        library_db=built_library.options.library_db,
        runtime_db=encore_home / "var/lib" / "runtime.db",
        artwork_dir=built_library.options.artwork_dir,
        music_dir=built_library.options.music_dir,
        temp_dir=encore_home / "var/cache/temp",
        max_items=6,
        launcher=engine,
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
def songs(built_library: Any) -> dict[str, int]:
    return title_ids(built_library.options.library_db)


# -- the one long walk ---------------------------------------------------


async def test_a_guest_fills_the_room_and_the_room_sees_it(
    app: Any, appliance: Appliance, songs: dict[str, int], engine: FakeEngine
) -> None:
    """Browse, search, queue, watch. One assertion per step, in the order a guest meets them.

    Written as a sequence rather than as five tests because the thing under attack is the
    hand-off: a route that works alone and leaks between steps is exactly what a party notices
    and what a per-route test cannot see.
    """

    async with started(app) as client:
        stream = Stream(app, "/events")
        try:
            # 1. The landing page: an empty appliance, and it says so.
            home = await client.get("/", headers=BROWSER)
            assert home.status_code == 200
            assert "Nothing playing" in home.text, home.text[:600]

            # 2. The first song is queued from the page the guest landed on, and the page
            #    answers with what changed rather than with a reload.
            first = songs["First Album Track 1"]
            queued = await client.post(
                "/queue", data={"song_id": str(first)}, headers=HTMX, follow_redirects=False
            )
            assert queued.status_code == 200
            assert "Playing now" in queued.text, queued.text[:600]
            assert engine.count("loadfile") == 1, "the queue reached mpv, not just SQLite"

            # 3. The listener's connection saw the same moment the button printed, in the
            #    order it happened: `snapshot` first because it is what makes a late join
            #    correct, then the facts named by their class (SAPRS 10.5). Reading to
            #    `SongStarted` here rather than peeking at two frames also drains the press,
            #    which is what lets step 7 assert on the next sequence rather than this one.
            seen, _ = await stream.names("SongStarted")
            assert seen[0] == "snapshot", seen
            assert "SongQueued" in seen, seen
            assert "QueueAdvanced" in seen, seen

            # 4. Someone else searches, and finds the track by its artist.
            found = await client.get("/api/v1/search?q=Test", headers=BROWSER)
            assert found.status_code == 200
            body = found.json()
            assert body["songs"], "a search of a populated library returned nothing"
            assert any(
                (hit["artist"] or {}).get("name") == "The Test Artists" for hit in body["songs"]
            ), body["songs"]

            # 5. The second guest asks for a different track: it waits, and the answer says so.
            second = songs["Second Album Track 1"]
            added = await client.post(
                "/queue", data={"song_id": str(second)}, headers=HTMX, follow_redirects=False
            )
            assert "Added at 2" in added.text, added.text[:600]

            state = (await client.get("/api/v1/queue")).json()
            assert state["now_playing"]["title"] == "First Album Track 1"
            assert [entry["position"] for entry in state["up_next"]] == [2]
            assert state["length"] == 2

            # 6. The guest at the back of the room reloads the panel, not the page.
            panel = await client.get("/fragments/up-next", headers=HTMX)
            assert panel.status_code == 200
            assert "Second Album Track 1" in unescape(panel.text)

            # 7. The first track runs out — nobody pressed anything. mpv says so, the tick
            #    notices, and the room changes: SAPRS 8.7's step 3 with no admin in sight.
            engine.mpv.properties["eof-reached"] = True
            await call_async(appliance.appliance, appliance.tick_once)

            ended, _ = await stream.names("SongStarted")
            assert "SongFinished" in ended, ended
            assert "QueueAdvanced" in ended, ended

            after = (await client.get("/api/v1/queue")).json()
            assert after["now_playing"]["title"] == "Second Album Track 1"
            assert after["up_next"] == []
            assert engine.count("loadfile") == 2, "the next song was loaded, not guessed"
        finally:
            await stream.close()


# -- what the guest is told ----------------------------------------------


async def test_the_button_tells_the_truth_about_where_the_song_went(
    app: Any, songs: dict[str, int]
) -> None:
    """SAPRS 9.1's confirmation, and the case that makes it worth testing.

    A guest who is told "Playing now" for a song that is twelfth in line learns nothing about
    their own song and something wrong about the room. The wording therefore has to come from
    the queue item, and the test has to put an item in front of it, which is why it is here in
    a journey rather than in a fragment unit test.
    """

    async with started(app) as client:
        first = songs["First Album Track 1"]
        second = songs["First Album Track 2"]
        await client.post("/queue", data={"song_id": str(first)}, headers=HTMX)

        answer = await client.post("/queue", data={"song_id": str(second)}, headers=HTMX)

        assert "Playing now" not in unescape(answer.text)
        assert "Added at 2" in unescape(answer.text), answer.text[:600]


async def test_a_refused_song_leaves_the_queue_alone(app: Any, songs: dict[str, int]) -> None:
    """The room is full, and "full" must not be paid for in lost requests (SAPRS 9.7).

    Six is the configured ceiling here, so the seventh press is the policy: a message, a queue
    that did not move, and a song that will play when the queue forgets it was asked for.
    """

    async with started(app) as client:
        ids = [songs[key] for key in sorted(songs) if key.startswith("First Album Track")]
        assert len(ids) == 3
        for song_id in ids:
            await client.post("/queue", data={"song_id": str(song_id)}, headers=HTMX)
        extra = [songs[key] for key in sorted(songs) if not key.startswith("First Album")]
        for song_id in extra[:3]:
            await client.post("/queue", data={"song_id": str(song_id)}, headers=HTMX)

        before = (await client.get("/api/v1/queue")).json()
        assert before["length"] == 6, "the configured ceiling, not a hardcoded one"

        refused = await client.post(
            "/queue", data={"song_id": str(extra[0])}, headers={"hx-request": "true"}
        )
        assert refused.status_code == 409, refused.text[:400]
        after = (await client.get("/api/v1/queue")).json()
        assert after["length"] == 6
        assert [entry["item_id"] for entry in after["up_next"]] == [
            entry["item_id"] for entry in before["up_next"]
        ]


# -- what the room sees without asking -------------------------------------


async def test_the_room_redraws_itself(app: Any, songs: dict[str, int]) -> None:
    """SAPRS 9.10: a screen that nothing touched still changed.

    The frames carry the HTML rather than a "something happened" ping, which is the difference
    between SSE as a notification and SSE as the UI. A guest's phone that is backgrounded
    mid-song therefore needs no catch-up logic (SAPRS 10.5's reasoning for the `snapshot`).
    """

    async with started(app) as client:
        stream = Stream(app, "/events")
        try:
            await stream.next(1)  # the snapshot every connection is given first

            await client.post(
                "/queue", data={"song_id": str(songs["First Album Track 1"])}, headers=HTMX
            )

            _seen, player = await stream.names("swap:player")
            assert "First Album Track 1" in unescape(player), player[:600]
        finally:
            await stream.close()


async def test_two_connections_see_the_same_party_once(app: Any, songs: dict[str, int]) -> None:
    """Forty phones, one render of the panel (SAPRS 8.6, ADR-012).

    Two here, not forty, because the assertion is about the count of renders and a second
    connection is enough to make it mean something; the forty-client number is measured in
    `tests/performance/test_http_latency.py`.
    """

    async with started(app) as client:
        left, right = Stream(app, "/events"), Stream(app, "/events")
        try:
            await left.next(1)
            await right.next(1)

            await client.post(
                "/queue", data={"song_id": str(songs["First Album Track 1"])}, headers=HTMX
            )

            first, second = await left.until("SongQueued"), await right.until("SongQueued")
            assert json.loads(first[1])["position"] == 1
            assert json.loads(second[1])["position"] == 1
        finally:
            await left.close()
            await right.close()


# -- the appliance as a physical thing ------------------------------------


async def test_the_room_survives_a_restart(
    app: Any, songs: dict[str, int], encore_home: Path
) -> None:
    """A power cut at a party is a reboot, and the queue is the thing worth keeping (ADR-009).

    Stopping and rebuilding the appliance is the same pair of calls `systemd` makes. What has
    to come back is the waiting list and the library; what must not come back is an assumption
    about what was on the speakers.
    """

    async with started(app) as client:
        await client.post(
            "/queue", data={"song_id": str(songs["First Album Track 1"])}, headers=HTMX
        )
        await client.post(
            "/queue", data={"song_id": str(songs["First Album Track 2"])}, headers=HTMX
        )
        before = (await client.get("/api/v1/queue")).json()

    assert before["length"] == 2

    paths = app.state.jukebox.config.paths
    second = composed_appliance(
        library_db=paths.library_db,
        runtime_db=paths.runtime_db,
        artwork_dir=paths.artwork_dir,
        music_dir=paths.music_dir,
        temp_dir=encore_home / "var/cache/temp",
        max_items=6,
        launcher=FakeEngine(),
    )
    # A reboot may lose a track and must not lose a night: the item that was on the speakers
    # is back at the head rather than in history (SAPRS 8.7's step 2). It is `pending`, not
    # playing, on purpose — `QueueService.start` says a box that makes noise before a guest
    # reaches the page is answering someone's hallway, and this is the test that keeps it honest.
    try:
        async with started(create_app(second)) as client:
            restored = (await client.get("/api/v1/queue")).json()
            assert restored["length"] == 2
            assert restored["now_playing"] is None, "restored, and silent"
            assert [entry["title"] for entry in restored["up_next"]] == [
                "First Album Track 1",
                "First Album Track 2",
            ]
            assert [entry["position"] for entry in restored["up_next"]] == [1, 2]

            # The first press after the reboot is what turns the room back on. It starts the
            # head of the list, not the song that was asked for (SAPRS 8.2's FIFO), and it says
            # so about the guest's own song: third in line, and honest about it.
            woken = await client.post(
                "/queue",
                data={"song_id": str(songs["Third Album Track 1"])},
                headers=HTMX,
                follow_redirects=False,
            )
            assert "Added at 3" in unescape(woken.text), woken.text[:400]
            room = (await client.get("/api/v1/queue")).json()
    finally:
        second.close()

    assert room["length"] == 3
    assert room["now_playing"]["title"] == "First Album Track 1"
