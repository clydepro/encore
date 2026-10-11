"""An operator's day: first run, a party happening, and the fixes an appliance needs (SAPRS 8.8, AIG 14, AIG 18).

The guest journey in this directory drives one long walk through the room's side of the product.
This is the same kind of test for the other side, in the order an operator actually meets it: a
fresh box that asks for a password, a room filling up while they watch, something stuck, a
rebuild published while the party is going, and a password changed before the venue closes. The
walks are separate tests rather than one long one because each has to be readable when it fails —
and what they share is not a story but the same continuity that matters: the appliance is never
restarted between steps, so the queue step 3 built is the queue step 5 fixes.

Everything below the HTTP layer is the shipped composition root, over the real Builder's output
and a real `runtime.db`. The engine is the mpv double, because no test server has speakers
(SAPRS 14.4).

What is deliberately *not* here, and where it is instead:

* The credential rules, the lockout and session expiry — `tests/unit/test_admin_service.py`.
* The cookie's flags and the origin check — `tests/unit/test_api_auth.py`.
* "Nothing under `/admin` answers without a session" — asserted against the OpenAPI document in
  `tests/integration/test_admin_interface.py`, where a route added next month is caught by the
  test that exists today rather than by this one.

One asymmetry is asserted on purpose rather than worked away, in step 4 of the first walk. A
**pause reaches a guest who asks and not a guest who watches**: pausing is not among the facts
Encore publishes (`SongQueued`, `SongStarted`, `SongFinished`, `QueueAdvanced`,
`PlaybackRecovered`, `LibraryReloaded`, `HealthChanged` — ADR-004's vocabulary, which milestone 14
did not extend), so a room staring at a paused track sees a progress bar that has stopped moving
rather than a "Paused" badge, until the next fact redraws the panel. The Administrator Guide says
so in the same breath as the button; this file records that the limit is a decision.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from apps.builder.pipeline import BuildOptions, run
from apps.server.appliance import Appliance
from encore.api.app import create_app
from tests.support.appliances import composed_appliance, title_ids
from tests.support.http import Stream, running, started
from tests.support.media import AlbumSpec, generate_albums
from tests.support.mpv import FakeEngine

pytestmark = pytest.mark.e2e

HTMX = {"hx-request": "true"}
BROWSER = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
ADMIN = "the password for the venue's operator"
NEW_ADMIN = "what it became at close of business"


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def appliance(encore_home: Path, built_library: Any, engine: FakeEngine) -> Iterator[Appliance]:
    """The shipped graph over the fixture library, with a small room so the ceiling is reachable."""

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


@pytest.fixture
def library_db(built_library: Any) -> Path:
    """The published file, so a test can put a different library where it is."""

    return Path(built_library.options.library_db)


async def sign_in(client: Any, password: str = ADMIN) -> None:
    """Through the first-run page, because that is the path an operator takes.

    Not through `AdminService.setup`, and not through `ENCORE_ADMIN_PASSWORD`: the installer's
    shortcut has its own test, and what a journey is for is the form a person fills in.
    """

    await client.post("/admin/login", data={"password": password})


def publish_a_rebuild(library_db: Path, tmp_path: Path) -> None:
    """Build a smaller library and rename it into place under a running appliance.

    The rename is the point, not the build: ADR-010 publishes atomically, and a live SQLite
    handle keeps the inode it opened, which is the entire reason the reload button this milestone
    added is a mechanism rather than a cosmetic one.
    """

    music = tmp_path / "late-delivery"
    generate_albums([AlbumSpec.with_tracks("Added Later", "Added Later", count=3)], music)
    staged = tmp_path / "late.db"
    run(
        BuildOptions(
            music_dir=music,
            library_db=staged,
            artwork_dir=tmp_path / "late-artwork",
            temp_dir=tmp_path / "late-builder",
            library_version="late-delivery",
        )
    )
    incoming = library_db.with_name("library.db.incoming")
    shutil.copyfile(staged, incoming)
    incoming.replace(library_db)


# -- the walk --------------------------------------------------------------


async def test_an_operator_opens_the_venue(
    *, app: Any, songs: dict[str, int], engine: FakeEngine
) -> None:
    """First run to a paused room, in the order it happens.

    Four steps, each asserted where a guest can also see it: the pause in step 4 is checked
    against `/api/v1/playing` rather than against a string in the dashboard, because a page that
    says a thing and a thing that happened are the same assertion only when the test looks at the
    second one.
    """

    async with started(app) as client:
        stream = Stream(app, "/events")
        try:
            # 1. First run. The page asks for one password, not two, because there is nothing to
            #    verify against yet (ADR-013).
            opening = await client.get("/admin/login", headers=BROWSER)
            assert "Set the administrator password" in opening.text, opening.text[:400]
            assert 'name="current"' not in opening.text, "first run has nothing to verify against"

            # 2. The operator sets it and arrives at a dashboard that is honest about an empty
            #    appliance. A 303 rather than a 200: the POST has no body worth reading (SAPRS
            #    10.7's answer to a form).
            created = await client.post(
                "/admin/login", data={"password": ADMIN}, follow_redirects=False
            )
            assert created.status_code == 303
            dashboard = await client.get("/admin/dashboard", headers=BROWSER)
            assert dashboard.status_code == 200
            assert "Nothing playing" in dashboard.text

            # 3. A guest queues, twice. The operator's panel is the same read the guest's screen
            #    used, drawn from the same services, so both names appear without the operator
            #    having asked for anything.
            await client.post(
                "/queue", data={"song_id": str(songs["First Album Track 1"])}, headers=HTMX
            )
            await stream.until("SongStarted")
            await client.post(
                "/queue", data={"song_id": str(songs["First Album Track 2"])}, headers=HTMX
            )
            await stream.until("SongQueued")
            panels = await client.get("/admin/dashboard", headers=HTMX)
            assert "First Album Track 1" in panels.text, "what is on the speakers"
            assert "First Album Track 2" in panels.text, "and what is waiting"

            # 4. Pause. The guest's fragment reads the live state and says "paused"; the guest's
            #    *stream* says nothing, because a pause is not a fact (module docstring).
            paused = await client.post("/admin/playback/pause?panel=dashboard", headers=HTMX)
            assert paused.status_code == 200
            assert (await client.get("/api/v1/playing")).json()["state"] == "paused"
            assert (
                'data-state="paused"' in (await client.get("/fragments/player", headers=HTMX)).text
            )
            quiet = [name for name, _ in await stream.next(2)]
            assert set(quiet) <= {"progress"}, f"a pause is not a fact, got {quiet}"
        finally:
            await stream.close()


async def test_an_operator_fixes_a_stuck_room(
    *, app: Any, songs: dict[str, int], engine: FakeEngine
) -> None:
    """A skip, heard by the room, and the two queue operations an operator has and a guest does not.

    The skip is asserted twice over, on purpose: once in the frames a guest's connection received
    and once in the song the appliance names afterwards. Either one alone could pass while the
    other was wrong — a fact published about a song that never started, or a state change that
    reached nobody's screen.
    """

    async with started(app) as client:
        stream = Stream(app, "/events")
        try:
            await sign_in(client)
            for title in ("First Album Track 1", "First Album Track 2"):
                await client.post("/queue", data={"song_id": str(songs[title])}, headers=HTMX)
            await stream.until("SongStarted")

            await client.post("/admin/playback/skip?panel=dashboard", headers=HTMX)
            told, _ = await stream.names("QueueAdvanced")
            assert "SongFinished" in told, told
            assert "swap:player" in told, "the room was told twice, not once"
            assert engine.count("loadfile") == 2, "and the second song reached the engine"
            assert (await client.get("/api/v1/playing")).json()["song"]["title"] == (
                "First Album Track 2"
            )

            # One request taken out of the middle, by id, through the same `QueueService.remove`
            # the JSON API's DELETE reaches rather than a second implementation of it (ADR-011).
            third = songs["First Album Track 3"]
            await client.post("/queue", data={"song_id": str(third)}, headers=HTMX)
            await stream.until("SongQueued")
            queue_page = await client.get("/admin/queue", headers=BROWSER)
            assert "First Album Track 2" in queue_page.text, "on the speakers"
            assert "First Album Track 3" in queue_page.text, "and waiting"
            waiting = (await client.get("/api/v1/queue")).json()["up_next"]
            removed = await client.post(
                f"/admin/queue/{waiting[0]['item_id']}/remove?panel=queue", headers=HTMX
            )
            assert removed.status_code == 200
            assert (await client.get("/api/v1/queue")).json()["length"] == 1

            # And the one action with no undo. The current track is not the queue's to stop
            # (SAPRS 8.4's "clear" is about requests, not about silencing a party).
            cleared = await client.post("/admin/queue/clear?panel=queue", headers=HTMX)
            assert cleared.status_code == 200
            after = (await client.get("/api/v1/queue")).json()
            assert after["up_next"] == [], "the waiting requests are gone"
            assert after["now_playing"] is not None, "and the music stayed on"
        finally:
            await stream.close()


async def test_every_screen_answers_with_something_true_on_it(*, app: Any) -> None:
    """Five pages, each checked for a fact rather than a title.

    A page name in a loop is the weakest assertion this suite writes, and it is worth exactly one
    thing: that the route renders through the real composition root rather than against a test's
    own wiring. The evidence strings are chosen so a template that drew an empty shell would fail
    — a component name on the health page, a counter label on statistics, the journal's own
    command on the log page.

    Two guest requests happen first so that health and statistics have something to be about. A
    page reporting zeroes is correct about an appliance that has done nothing, and this is not
    that appliance.
    """

    async with running(app) as client:
        await sign_in(client)
        await client.post("/api/v1/queue?song_id=1")
        await client.get("/search", params={"q": "track"}, headers=BROWSER)

        for path, evidence in (
            ("/admin/health", "playback"),
            ("/admin/statistics", "searches"),
            ("/admin/logs", "journalctl"),
            ("/admin/configuration", "queue"),
            ("/admin/library", "library.db"),
        ):
            page = await client.get(path, headers=BROWSER)
            assert page.status_code == 200, path
            assert evidence in page.text.lower(), (path, evidence)


async def test_a_rebuild_published_mid_party_reaches_the_room(
    *, app: Any, library_db: Path, tmp_path: Path
) -> None:
    """A rebuild, noticed without a restart, by an appliance with a song on the speakers.

    The mechanism half of this is `tests/unit/test_library_reload.py`. What belongs in a journey
    is the part two components disagree about unless something coordinates them: the operator's
    panel redraws from a `POST`, and the room finds out from a fact on a stream. A reload that
    published nothing would leave forty phones showing a catalogue that no longer exists.
    """

    async with running(app) as client:
        stream = Stream(app, "/events")
        try:
            await sign_in(client)
            await client.post("/api/v1/queue?song_id=1")
            await stream.until("SongStarted")

            publish_a_rebuild(library_db, tmp_path)
            answer = await client.post("/admin/library/reload?panel=library", headers=HTMX)

            assert answer.status_code == 200
            assert "late-delivery" in answer.text, "the panel names the library it reopened"
            told, _ = await stream.names("LibraryReloaded")
            assert "LibraryReloaded" in told, told

            found = await client.get("/search", params={"q": "Added Later"}, headers=BROWSER)
            assert "Added Later" in found.text, "the room can find the rebuild's own music"
            assert (await client.get("/api/v1/playing")).status_code == 200, "nothing stopped"
        finally:
            await stream.close()


async def test_the_password_change_ends_the_day(*, app: Any) -> None:
    """Rotation revokes every session, including the one that asked (ADR-013).

    Asserted as a journey with two clients rather than as a service call because the observable
    promise is about *both* laptops: the operator who changed the password is signed out, and so
    is a second one still holding an old cookie. A unit test can watch the dictionary shrink;
    only two clients can watch neither of them get through.
    """

    async with running(app) as first, running(app, lifespan=False) as second:
        await sign_in(first)
        await sign_in(second, ADMIN)
        assert (await first.get("/admin/dashboard", headers=BROWSER)).status_code == 200

        changed = await first.post(
            "/admin/password", data={"current": ADMIN, "new_password": NEW_ADMIN}
        )
        assert changed.status_code in (200, 303)
        assert (await second.get("/admin/dashboard", headers=BROWSER)).status_code == 303, (
            "the other laptop was signed out too"
        )
        assert (await first.get("/admin/dashboard", headers=BROWSER)).status_code == 303

        again = await first.post(
            "/admin/login", data={"password": NEW_ADMIN}, follow_redirects=False
        )
        assert again.status_code == 303
        assert (await first.get("/admin/dashboard", headers=BROWSER)).status_code == 200

        refused = await first.post(
            "/admin/login", data={"password": ADMIN}, headers=BROWSER, follow_redirects=False
        )
        assert refused.status_code == 401, "a wrong password is a page with a refusal on it"


async def test_signing_out_leaves_nothing_behind(*, app: Any) -> None:
    """Logout ends the session and clears the cookie, in that order.

    The order is the point: a cookie-only logout leaves a token replayable from a shell history
    file, and a session-only logout leaves a browser holding a dead token that the next request
    quietly discards — which is the same bug reported as a feature. The check afterwards is a
    dashboard request rather than a status on the logout itself.
    """

    async with running(app) as client:
        await sign_in(client)
        out = await client.post("/admin/logout", follow_redirects=False)
        assert out.status_code == 303
        assert "encore_admin=" in str(out.headers.get("set-cookie", "")), "the cookie was withdrawn"
        back = await client.get("/admin/dashboard", headers=BROWSER, follow_redirects=False)
        assert back.status_code == 303
        assert "/admin/login" in back.headers["location"]


async def test_the_guest_never_sees_the_administrative_surface(*, app: Any) -> None:
    """Anonymity has a second half: the room's page must not know the operator exists (ADR-007).

    A guest's HTML carries no admin link, no session cookie and no hint that counters are being
    kept. That is the difference between an administrative interface and a second audience on the
    same privacy promise, and the cookie assertion is the one that fails first if `session_cookie`
    ever is attached to a guest response.
    """

    async with running(app) as client:
        home = await client.get("/", headers=BROWSER)

    assert "/admin" not in home.text, "the guest's page does not advertise the back door"
    assert not home.cookies, "a guest is given nothing to present later"
    assert "Searches" not in home.text
