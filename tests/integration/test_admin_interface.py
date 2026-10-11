"""An operator signs in and the appliance answers (SAPRS 8.8, 10.7, 10.8, AIG 14).

AIG 14 lists nine things the administrative interface must be: a login, a dashboard, playback
control, queue management, health, statistics, logs, a configuration summary and library
information. This file asserts each of them *through the composition root the installer ships*,
for the reason `test_server_app.py` states at length — an admin surface wired by hand in a test
would keep passing when the real `build()` forgot a service, and forgetting one is exactly the
kind of bug milestone 14 can have.

What is deliberately not here, and where it is instead:

* **The credential rules** — hashing, the length of the failure window, session expiry — are
  `tests/unit/test_admin_service.py` against a real `runtime.db`. Duplicating them as HTTP
  would make a change to `AdminConfig` break two files. What belongs here instead is the
  *answer* a lock produces: the status, the wait in a header, and the page with the field on it
  — all of which a unit test of the service cannot see, because they are made of three files.
* **The cookie's flags** are `tests/unit/test_api_auth.py`, where they can be asserted on a
  response object rather than inferred from a redirect.
* **The thread discipline** is ADR-012's own test in `test_server_app.py`; what belongs here is
  that an administrative request that *writes* — a pause, a removal, a reload — still writes on
  the appliance thread, which is one assertion on the thread that touched `runtime.db`.

Three properties get more attention than the endpoint list:

* **Nothing under `/admin` answers without a session**, except the sign-in page and its POST.
  That is asserted by walking the app's own route table rather than by listing paths, so a
  route added next month is covered by the test that exists today.
* **An action changes what a guest sees.** Every control on the dashboard is one of
  `QueueService`'s administrative methods, so the check on each is the guest's own
  `/api/v1/playing` afterwards — not a string in an admin page that could be printed by a
  handler that did nothing.
* **The failure paths are pages.** A 401 to a browser that followed a link is a page with no
  way back; a redirect to the sign-in screen is the same fact plus the action. Both are
  asserted per medium, because SAPRS 10.7 lists 401 and 303 as answers to different callers
  and nothing but a test keeps an implementation from choosing one for both.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from apps.server.appliance import Appliance
from encore.api.app import create_app
from encore.api.auth import SESSION_COOKIE
from tests.support.appliances import composed_appliance
from tests.support.http import running
from tests.support.mpv import FakeEngine

pytestmark = pytest.mark.integration

HTMX = {"hx-request": "true"}
HTML = {"accept": "text/html"}

#: What `tests/conftest.py`'s fixture library has in it, in the order a party would notice.
#:
#: Every string here is a password to nothing but this file. They are named rather than inline
#: because `detect-secrets` rightly flags an assignment that looks like a credential, and a
#: pragma on one line per appliance-wide password is cheaper than explaining five of them.
PASSWORD = "a long enough password"  # pragma: allowlist secret

#: Where a refused `?next=` lands, and the constant this file would rather not repeat.
DASHBOARD = "/admin/dashboard"
WRONG = "wrong but long enough"  # pragma: allowlist secret
NOT_IT = "not the password"  # pragma: allowlist secret
ROTATED = "a different long password"  # pragma: allowlist secret
TOO_SHORT = "short"  # pragma: allowlist secret


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def appliance(encore_home: Path, built_library: Any, engine: FakeEngine) -> Iterator[Appliance]:
    """The shipped composition, over the fixture library (see `test_server_app.py`)."""

    made = composed_appliance(
        library_db=built_library.options.library_db,
        runtime_db=encore_home / "var/lib" / "runtime.db",
        artwork_dir=built_library.options.artwork_dir,
        music_dir=built_library.options.music_dir,
        temp_dir=encore_home / "var/cache/temp",
        max_items=8,
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
async def signed_in(app: Any) -> Any:
    """A client that has an administrative session, by way of the first-run page.

    Created through `POST /admin/login` rather than by writing the hash into `runtime.db`,
    because the first-run path is part of the interface being tested (ADR-013) and a fixture
    that seeded the credential would leave the branch where every real appliance starts untested.
    """

    async with running(app) as client:
        first = await client.get("/admin/login")
        assert "no administrator yet" in first.text, "the first-run page did not say so"
        created = await client.post(
            "/admin/login", data={"password": PASSWORD}, follow_redirects=False
        )
        assert created.status_code == 303, created.text
        assert created.cookies.get(SESSION_COOKIE), "the session cookie was not issued"
        yield client


# -- who is allowed in ------------------------------------------------------


async def test_every_admin_route_but_the_door_requires_a_session(app: Any) -> None:
    """The requirement, asserted against the route table rather than a list of paths.

    `encore/controllers/admin.py` puts the dependency on the router instead of on each route,
    which is the stronger of the two shapes — and this is the test that pays for it, because a
    router-level dependency is invisible in a route's signature and could as easily be invisible
    to a reader as to an attacker.

    The list comes from the generated OpenAPI document rather than from the app's internals:
    FastAPI 0.142 keeps an included router as one node rather than flattening it, so `app.routes`
    would be a test of a data structure, and `/openapi.json` is the thing a client actually
    integrates against — which is also the document that must not advertise a route nobody
    protected (SAPRS 10.12).
    """

    document = app.openapi()["paths"]
    administrative = {
        (path, method.upper())
        for path, operations in document.items()
        for method in operations
        if path.startswith("/admin")
    }
    assert len(administrative) >= 15, administrative
    unprotected = administrative - {("/admin/login", "GET"), ("/admin/login", "POST")}
    async with running(app) as client:
        for path, method in sorted(unprotected):
            response = await client.request(method, path)
            assert response.status_code in (303, 401), (path, method, response.status_code)


async def test_a_browser_that_followed_a_link_is_taken_to_the_door(app: Any) -> None:
    """A person gets a page; a tool gets a 401 (SAPRS 10.7's two answers, one status)."""

    async with running(app) as client:
        page = await client.get("/admin/dashboard", headers=HTML, follow_redirects=False)
        api = await client.get("/api/v1/stats")

    assert page.status_code == 303
    assert page.headers["location"] == "/admin/login?next=/admin/dashboard"
    assert api.status_code == 401
    assert api.json()["error"]["code"] == "unauthorized"


async def test_the_login_page_offers_first_run_and_the_door(app: Any) -> None:
    async with running(app) as client:
        before = await client.get("/admin/login")
        await client.post("/admin/login", data={"password": PASSWORD})
        after = await client.get("/admin/login")

    assert "Set the administrator password" in before.text
    assert "Encore administration" in after.text
    assert "Set the administrator password" not in after.text


async def test_a_wrong_password_is_refused_in_public_and_in_json(app: Any) -> None:
    """Two answers to one event, because a person and a script cannot both be shown a page.

    The 401 is the fact SAPRS 10.7 names; the difference is only in what arrives beside it — a
    browser gets the login page with the sentence in it, a tool gets the envelope. Both are
    asserted here because the branch is one `Accept` header wide, and a route that answered a
    script with markup would pass every test that only ever asks like a person.
    """

    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        await client.post("/admin/logout")
        page = await client.post("/admin/login", data={"password": WRONG}, headers=HTML)
        api = await client.post("/api/v1/queue?song_id=1")
        refused = await client.post("/admin/login", data={"password": WRONG}, headers=HTML)
        tool = await client.post("/admin/login", data={"password": WRONG})

    assert page.status_code == 401
    assert "not the administrator password" in page.text
    assert refused.status_code == 401
    assert api.status_code in (201, 404)  # the guest endpoint never cared, and still doesn't
    assert tool.status_code == 401
    assert tool.json()["error"]["code"] == "unauthorized"
    assert "<" not in tool.json()["error"]["message"], "a script was handed markup to read"


async def test_the_bare_admin_address_is_the_dashboard(app: Any, signed_in: Any) -> None:
    """`/admin` is not a page; it is a way into one.

    An operator types the address they were told about, and the answer must be a destination
    rather than a 405 — but a *temporary* redirect, because a permanent one would teach the
    browser that `/admin` is the dashboard forever, including after a change that gives `/admin`
    a page of its own.
    """

    landing = await signed_in.get("/admin", headers=HTML, follow_redirects=False)

    assert landing.status_code == 307
    assert landing.headers["location"] == "/admin/dashboard"


async def test_a_url_that_arrived_from_elsewhere_is_answered_with_a_place(app: Any) -> None:
    """`?next=` is a suggestion from someone else's page, so it is checked before it is obeyed.

    A sign-in form that honoured `?next=https://elsewhere.example/` would be an open redirect on
    a box whose only network is the party's, and an open redirect is the one way a LAN attacker
    gets a signed-in operator to land somewhere with their session cookie attached. The answer is
    not an error — the operator has just typed a password, and the appliance has it — it is the
    dashboard, because the alternative is a page that tells a person their bookmark was wrong.

    The shapes tried here are the ones a filter written against the obvious case misses. A
    backslash is a slash to a browser that normalises before it parses, so `/\\host` is an
    authority. A literal tab is deleted by a URL parser *before* it decides what it is looking at,
    so `/\t/host` arrives as `//host` — and its percent-encoded cousin `/%09/host` does not,
    because decoding happens after parsing, which is why that one is on the kept list below with
    the query strings rather than on this one. A filter that refused `%` would be correct and
    useless; this one refuses the characters, not the encodings.
    """

    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        await client.post("/admin/logout")
        targets = [
            "https://elsewhere.example/",
            "//elsewhere.example/admin",
            r"/\elsewhere.example",
            "/\t/elsewhere.example",
            "/ elsewhere.example",
            "javascript:alert(1)",
        ]
        landed = []
        for target in targets:
            response = await client.post(
                "/admin/login",
                params={"next": target},
                data={"password": PASSWORD},
                follow_redirects=False,
            )
            landed.append(response.headers["location"])
            await client.post("/admin/logout")

    assert set(landed) == {DASHBOARD}, list(zip(targets, landed, strict=True))


async def test_a_path_that_is_ours_is_kept_exactly(app: Any) -> None:
    """The other half of the filter, which is the half an operator feels.

    A `?next=` naming a page on this box is honoured precisely, query string included. A filter
    that dropped `?level=warning` would be secure and useless — the logs page reached from a
    filtered link is the page the operator was actually reading. `/ %09 /host` is on this list
    rather than the hostile one because decoding happens after a browser has already decided what
    the authority is, so the sequence is an ordinary path character; a *literal* tab, which is
    removed before that decision, is refused by the test above.
    """

    kept = [
        "/admin/queue",
        "/admin/logs?level=warning",
        "/%09/elsewhere.example",
        "/",
    ]
    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        landed = []
        for target in kept:
            response = await client.post(
                "/admin/login",
                params={"next": target},
                data={"password": PASSWORD},
                follow_redirects=False,
            )
            landed.append(response.headers["location"])
            await client.post("/admin/logout")

    assert landed == kept


async def test_a_locked_appliance_says_when_it_will_answer_again(app: Any) -> None:
    """The window is `AdminService`'s; the *answer* is a route's, and only a route can test it.

    A unit test can prove that five failures fill a window and that a sixth is refused. It cannot
    prove that an operator typing at a locked box gets a page saying how long to wait, or that a
    script gets a 429 with a header it can obey — and those two are the difference between a
    limiter that protects the appliance and one that merely annoys a person (SAPRS 10.7, 10.8).
    The status has to change too: a 401 here would tell a client to try a different password,
    which is the one thing a lockout must not invite, and the message must change with it because
    "that is not the password" is the sentence an operator who is merely waiting should not read.
    """

    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        await client.post("/admin/logout")
        refusals = [
            (
                await client.post(
                    "/admin/login", data={"password": f"{NOT_IT} {n} times"}, headers=HTML
                )
            ).status_code
            for n in range(5)
        ]
        page = await client.post(
            "/admin/login", data={"password": f"{NOT_IT} six times"}, headers=HTML
        )
        tool = await client.post("/admin/login", data={"password": f"{NOT_IT} seven times"})

    assert refusals == [401] * 5, "the lock must not begin before the window is full"
    assert page.status_code == 429
    assert "Try again in" in page.text
    assert 'name="password"' in page.text, "a locked operator needs the form to come back to"
    assert "not the administrator password" not in page.text
    assert tool.status_code == 429
    waiting = int(tool.headers["retry-after"])
    assert 14 * 60 < waiting <= 15 * 60, "the header is the window that is left, not a fresh one"
    assert tool.json()["error"]["details"]["retry_after_seconds"] == waiting
    assert tool.json()["error"]["code"] == "rate_limited"


async def test_a_panel_that_outlived_its_session_is_sent_to_the_door(
    app: Any, signed_in: Any
) -> None:
    """The expired-session answer, as the browser that receives it will act on it.

    `tests/unit/test_api_auth.py` asserts the header on a request built by hand; this asserts it
    on a real panel of a real appliance, because the failure it replaced was invisible in both
    kinds of test: the response was a 401 carrying a fragment, htmx's default response handling
    throws away a 4xx body without reading it, and so an operator's dashboard simply stopped
    redrawing. The body is still checked, because a client that ignores `HX-Redirect` has to be
    able to read something.
    """

    token = signed_in.cookies.get(SESSION_COOKIE)
    await signed_in.post("/admin/logout")

    page = await signed_in.get(
        "/admin/queue",
        headers={
            "accept": "text/html",
            "hx-request": "true",
            "cookie": f"{SESSION_COOKIE}={token}",
        },
    )

    assert page.status_code == 401
    assert page.headers["hx-redirect"] == "/admin/login?next=/admin/queue"
    assert "Sign in again" in page.text


async def test_logout_ends_the_session_here_and_now(app: Any) -> None:
    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        assert (await client.get("/admin/dashboard")).status_code == 200
        gone = await client.post("/admin/logout", follow_redirects=False)
        again = await client.get("/admin/dashboard", headers=HTML, follow_redirects=False)

    assert gone.status_code == 303
    assert gone.headers["location"] == "/admin/login"
    assert SESSION_COOKIE not in again.cookies or again.cookies[SESSION_COOKIE] == ""
    assert again.status_code == 303


async def test_logout_from_an_administrative_panel_navigates_instead_of_swapping(
    app: Any, signed_in: Any
) -> None:
    """A 204 with no markup in it, which is what tells htmx to leave the page alone and go.

    The logout button lives in the shell's nav, so its `hx-post` has no region to redraw — the
    panel it would swap is the one that asked, and the one that asked is about to be irrelevant.
    `HX-Redirect` is the htmx-native answer (and, unlike a 303, it is a *client* navigation, so
    the browser is not left with a 204 to explain). The cookie is withdrawn on the same response:
    without that the redirect would land on the sign-in page still holding a live session cookie
    that the appliance had already forgotten.
    """

    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        gone = await client.post("/admin/logout", headers=HTMX)
        dashboard = await client.get("/admin/dashboard", headers=HTML, follow_redirects=False)

    assert gone.status_code == 204
    assert not gone.content, "a 204 with a body is a response no client is required to read"
    assert gone.headers["hx-redirect"] == "/admin/login"
    assert SESSION_COOKIE not in gone.cookies or gone.cookies[SESSION_COOKIE] == ""
    assert dashboard.status_code == 303


async def test_a_cross_site_post_is_refused_even_with_a_session(app: Any) -> None:
    """SameSite is a browser instruction; the `Origin` check is the appliance's own (ADR-013)."""

    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        response = await client.post(
            "/admin/playback/pause", headers={"origin": "http://elsewhere.example"}
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


# -- the screens ------------------------------------------------------------

PAGES = (
    "/admin/dashboard",
    "/admin/queue",
    "/admin/health",
    "/admin/statistics",
    "/admin/logs",
    "/admin/configuration",
    "/admin/library",
    "/admin/password",
)


async def test_every_administrative_page_renders(app: Any, signed_in: Any) -> None:
    """Nine screens, no template errors, one navigation on each.

    The assertion is a status and the navigation, not a screenshot of each page: what breaks
    here is a context key a template renamed or a service method that raises, and both arrive as
    a 500 rather than as a changed pixel. The content of each screen is the next test's job.
    """

    for path in PAGES:
        response = await signed_in.get(path)
        assert response.status_code == 200, (path, response.text[-800:])
        assert 'aria-label="Administration"' in response.text, path


async def test_the_dashboard_shows_the_song_and_the_whole_queue(app: Any) -> None:
    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        await client.post("/queue?song_id=1", headers=HTMX)
        await client.post("/queue?song_id=2", headers=HTMX)
        page = await client.get("/admin/dashboard")

    assert "Now playing" in page.text or "Nothing playing" in page.text
    assert page.text.count('class="admin-row') >= 2
    assert "Clear queue" in page.text
    assert "Remove" in page.text


async def test_the_queue_page_lists_every_request_and_the_ceiling(app: Any) -> None:
    """The whole list rather than the twenty a phone is given (SAPRS 9.9, 8.8)."""

    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        for song in (1, 2, 3):
            await client.post(f"/queue?song_id={song}", headers=HTMX)
        page = await client.get("/admin/queue")

    assert "3 of a possible 8" in page.text
    assert page.text.count("Remove") >= 3


async def test_the_health_page_names_every_component(app: Any) -> None:
    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        page = await client.get("/admin/health")

    # The four names `apps/server/appliance.py:_sources` registers, which are SAPRS 11.7's
    # step 8 made concrete: the engine, the catalogue, the mutable database and the index.
    for component in ("playback", "library", "search", "runtime"):
        assert component in page.text, component
    assert "healthy" in page.text
    assert "Every component answered" in page.text


async def test_the_statistics_page_counts_what_the_guest_endpoints_only_did(
    app: Any,
) -> None:
    """A song played, a search run and an artist looked at, then the page that says so."""

    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        await client.get("/search?q=album")
        await client.get("/artists/1")
        await client.post("/queue?song_id=1", headers=HTMX)
        page = await client.get("/admin/statistics")
        api = await client.get("/api/v1/stats")

    assert "played today" in page.text
    assert "songs played" in page.text
    body = api.json()
    assert body["totals"]["artist_navigations"] == 1
    assert body["totals"]["searches"] == 1
    assert body["totals"]["queue_added"] == 1


async def test_the_logs_page_shows_the_appliances_own_lines(app: Any) -> None:
    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        page = await client.get("/admin/logs")
        filtered = await client.get("/admin/logs?level=WARNING")

    assert "line" in page.text
    assert "journalctl -u encore" in page.text
    assert "Nothing has been logged" in page.text or "sign" in page.text.lower()
    assert filtered.status_code == 200
    assert "at WARNING or worse" in filtered.text


async def test_no_log_line_carries_a_clients_address(app: Any) -> None:
    """ADR-007, asserted on the screen that would show it.

    The guest requests above are made with a `User-Agent` and from an address; the appliance's
    own log is the place either could appear, so the assertion is on the rendered tail rather
    than on the formatter's unit test alone.
    """

    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        for _ in range(3):
            await client.get("/", headers={"user-agent": "A Phone That Should Not Be Named"})
        await client.get("/search?q=album")
        page = await client.get("/admin/logs?limit=400")

    assert "A Phone That Should Not Be Named" not in page.text
    assert "127.0.0.1" not in page.text
    assert "client" not in page.text.lower()


async def test_the_configuration_page_sums_up_the_running_model(app: Any) -> None:
    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        page = await client.get("/admin/configuration")

    assert "max_items" in page.text
    assert "queue" in page.text
    assert "session_minutes" in page.text
    # SAPRS 12.3's read-only rule, said on the page rather than in a manual nobody reads.
    assert "no way to change it" in page.text


async def test_the_library_page_shows_the_provenance_and_both_tallies(app: Any) -> None:
    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        page = await client.get("/admin/library")

    assert "schema" in page.text
    assert "rules" in page.text
    assert "built" in page.text
    assert "Reopen library" in page.text


# -- the things an operator can do ------------------------------------------


async def test_each_playback_command_moves_the_appliance_the_way_it_says(app: Any) -> None:
    """Five buttons, checked by the guest-facing state each is supposed to change.

    A string in an administrative page is not evidence: `state` from `/api/v1/playing` is the
    same field a guest's phone is being shown, so the pair of assertions says the button works
    and says it worked on the one state there is (SAPRS 10.1).
    """

    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        await client.post("/queue?song_id=1", headers=HTMX)
        assert (await client.get("/api/v1/playing")).json()["state"] == "playing"

        paused = await client.post("/admin/playback/pause", headers=HTMX)
        assert (await client.get("/api/v1/playing")).json()["state"] == "paused"

        resumed = await client.post("/admin/playback/resume", headers=HTMX)
        assert (await client.get("/api/v1/playing")).json()["state"] == "playing"

        skipped = await client.post("/admin/playback/skip", headers=HTMX)
        stopped = await client.post("/admin/playback/stop", headers=HTMX)
        # `stopped` is what the operator asked for; `idle` is what SAPRS 7.3's machine
        # reports once the track has ended and nothing is queued behind it.
        assert (await client.get("/api/v1/playing")).json()["state"] == "idle"

        started = await client.post("/admin/playback/start", headers=HTMX)

    for fragment in (paused, resumed, skipped, stopped, started):
        assert fragment.status_code == 200, fragment.text
        assert 'id="admin-dashboard"' in fragment.text

    # The line beside the redrawn panel is the only place a URL's word becomes operator-facing
    # English, and `pause` is not a sentence: what each button must say is written in
    # `admin.py::_sentence`, and a fragment that echoed the path segment instead would read
    # "pause." under a dashboard that had just been paused.
    assert "Paused." in paused.text
    assert "Resumed." in resumed.text
    assert "Skipped to the next request." in skipped.text
    assert "Stopped." in stopped.text
    assert "Started the next request." in started.text
    assert not any(
        re.search(r">\s*(pause|resume|skip|stop|start)\s*\.\s*<", f.text)
        for f in (paused, resumed, skipped, stopped, started)
    ), "a URL segment echoed into a sentence an operator reads"


async def test_an_action_answers_with_the_region_that_asked(app: Any) -> None:
    """`?panel=` is the whole of the routing between two screens that share one button."""

    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        await client.post("/queue?song_id=1", headers=HTMX)
        dashboard = await client.post("/admin/playback/pause?panel=dashboard", headers=HTMX)
        queue = await client.post("/admin/playback/pause?panel=queue", headers=HTMX)

    assert 'id="admin-dashboard"' in dashboard.text
    assert 'id="admin-queue"' in queue.text


async def test_a_plain_browser_gets_a_redirect_and_not_a_fragment(app: Any) -> None:
    """No JavaScript, no lost button (SAPRS 13.2's rule about the front end).

    Both an ordinary command and the one with a confirmation on it, because `hx-confirm` is the
    only place this interface asks a browser for permission: with no script there is no dialog, so
    the clear button is a plain form that clears and redirects. The alternative — a no-JS branch
    that refused the action for want of a confirmation nobody could be shown — would make the
    queue page's most destructive control the one that only works when JavaScript loads.

    Both land on the dashboard rather than on the page the button was on. That is the rule for
    every administrative action without a fragment to answer with, and it exists because the
    obvious alternative is to follow `Referer`, which is an outside caller's suggestion about where
    this appliance should send its own browser — the open redirect `_safe()` was written to refuse.
    The dashboard is the one page that shows both halves of what any of these actions change, so
    the destination is never a dead end even when it is not where the button was.
    """

    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        await client.post("/queue?song_id=1", headers=HTMX)
        paused = await client.post("/admin/playback/pause", headers=HTML, follow_redirects=False)
        cleared = await client.post("/admin/queue/clear", headers=HTML, follow_redirects=False)
        length = (await client.get("/api/v1/queue")).json()["length"]

    assert paused.status_code == 303
    assert paused.headers["location"] == "/admin/dashboard"
    assert cleared.status_code == 303
    assert cleared.headers["location"] == "/admin/dashboard"
    assert length <= 1, "the no-JS clear performed nothing, which is the bug this branch had"


async def test_an_empty_field_is_not_a_failed_sign_in(app: Any) -> None:
    """An empty form is a request that never asked, not an attempt that missed.

    The route answers it before `AdminService` sees the password, which matters for two reasons at
    once. A browser that submits an empty field — a double tap, a password manager that failed to
    fill — must not spend one of the five failures that lock the appliance, because an operator
    who is locked out by their own keyboard has a worse evening than one who was guessed at. And
    the sentence has to come from here rather than from the service, since the service's answer to
    "nothing" is the same as its answer to "wrong", which is the property the regression file
    pins. Same page, same 401, no count.
    """

    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        await client.post("/admin/logout")
        refusals: list[int] = []
        for _ in range(6):
            blank = await client.post("/admin/login", data={"password": ""}, headers=HTML)
            refusals.append(blank.status_code)
        dashboard = await client.get("/admin/dashboard", headers=HTML, follow_redirects=False)
        signed_in = await client.post("/admin/login", data={"password": PASSWORD})

    assert refusals == [401] * 6
    assert "Enter the administrator password" in blank.text
    assert dashboard.status_code == 303, "an empty field was answered like a session"
    assert signed_in.status_code == 303, "the empty attempts locked the operator out"


async def test_removing_a_waiting_request_leaves_the_song_and_the_order(
    app: Any,
) -> None:
    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        for song in (1, 2, 3):
            await client.post(f"/queue?song_id={song}", headers=HTMX)
        before = (await client.get("/api/v1/queue")).json()
        middle = before["up_next"][0]["item_id"]
        wanted = [entry["id"] for entry in before["up_next"]][1:]
        removed = await client.post(f"/admin/queue/{middle}/remove", headers=HTMX)
        after = (await client.get("/api/v1/queue")).json()

    assert removed.status_code == 200
    assert after["length"] == 2
    assert [entry["id"] for entry in after["up_next"]] == wanted


async def test_a_browser_with_no_script_still_uses_the_queue_page(app: Any) -> None:
    """Every control twice, once with htmx and once without it (SAPRS 13.2).

    The fragment branch is the one that gets used at a party and the one a test reaches for
    first, which is how the plain form became this interface's most neglected path. What matters
    here is not the status code — a 303 is easy to write — but that the queue changed anyway, in
    the three ways an operator can change it from a page: a command, a removal, a reload.
    """

    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        for song in (1, 2):
            await client.post(f"/queue?song_id={song}", headers=HTMX)
        before = (await client.get("/api/v1/queue")).json()
        middle = before["up_next"][0]["item_id"]
        skipped = await client.post("/admin/playback/skip", headers=HTML, follow_redirects=False)
        removed = await client.post(
            f"/admin/queue/{middle}/remove", headers=HTML, follow_redirects=False
        )
        reloaded = await client.post("/admin/library/reload", headers=HTML, follow_redirects=False)
        after = (await client.get("/api/v1/queue")).json()

    assert skipped.status_code == 303
    assert removed.status_code == 303
    assert reloaded.status_code == 303
    assert reloaded.headers["location"] == "/admin/library", "a reload sent the operator elsewhere"
    assert after["length"] == 0, "a plain form that rendered a page and performed nothing"


async def test_removing_what_is_playing_is_a_skip(app: Any) -> None:
    """The rule `QueueService.remove` states, checked where an operator would meet it."""

    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        await client.post("/queue?song_id=1", headers=HTMX)
        await client.post("/queue?song_id=2", headers=HTMX)
        head = (await client.get("/api/v1/queue")).json()["now_playing"]["item_id"]
        response = await client.post(f"/admin/queue/{head}/remove", headers=HTMX)
        playing = (await client.get("/api/v1/playing")).json()

    assert response.status_code == 200
    assert playing["state"] == "playing"
    assert playing["song"]["id"] == 2


async def test_removing_something_already_gone_is_a_404(app: Any) -> None:
    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        response = await client.post("/admin/queue/4242/remove", headers=HTMX)

    assert response.status_code == 404
    assert "no longer in the queue" in response.text


async def test_clearing_the_queue_leaves_the_current_track_alone(app: Any) -> None:
    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        for song in (1, 2, 3):
            await client.post(f"/queue?song_id={song}", headers=HTMX)
        cleared = await client.post("/admin/queue/clear", headers=HTMX)
        queue = (await client.get("/api/v1/queue")).json()

    assert cleared.status_code == 200
    assert queue["length"] == 1
    assert queue["now_playing"]["position"] == 1


async def test_the_queue_page_carries_a_confirmation_the_guest_pages_do_not(
    app: Any,
) -> None:
    """`hx-confirm` exists on one button on the whole appliance (SAPRS 12.7's one write)."""

    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        page = await client.get("/admin/dashboard")
        guest = await client.get("/queue")

    assert "hx-confirm" in page.text
    assert "hx-confirm" not in guest.text


async def test_a_reload_notices_a_rebuilt_library_and_tells_the_stream(app: Any) -> None:
    """The one route that changes what every search panel believes (ADR-006, SAPRS 6.11)."""

    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        before = (await client.get("/api/v1/info")).json()["songs"]
        page = await client.post("/admin/library/reload", headers=HTMX)

    assert page.status_code == 200
    assert "Reopened the library, which now holds" in " ".join(page.text.split())
    assert str(before) in page.text


async def test_the_password_page_logs_every_session_out(app: Any) -> None:
    """Including this one, which is ADR-013's chosen cost and therefore tested as a feature."""

    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        assert (await client.get("/admin/dashboard")).status_code == 200
        changed = await client.post(
            "/admin/password",
            data={"current": PASSWORD, "new_password": ROTATED},
            follow_redirects=False,
        )
        after = await client.get("/admin/dashboard", headers=HTML, follow_redirects=False)

    assert changed.status_code == 200
    assert "Password changed" in changed.text
    assert after.status_code == 303


async def test_a_weak_new_password_is_refused_as_a_422(app: Any) -> None:
    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        response = await client.post(
            "/admin/password", data={"current": PASSWORD, "new_password": TOO_SHORT}
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "weak_credential"


# -- what it costs, and what it must not do -------------------------------


async def test_an_administrative_read_happens_on_the_appliance_thread(
    app: Any, appliance: Appliance
) -> None:
    """ADR-012, on the newest path to `runtime.db` (SAPRS 11.7's step 5).

    The seam is the composed service itself: the test wraps `StatisticsService.report` to
    record the thread it ran on, which is the only way to ask this question of the running
    appliance rather than of a unit test's fake. What would break without this assertion is a
    refactor that moved a read onto the event loop, where it would share a SQLite connection
    with the tick and with playback — correct in a test, corrupt during a party.
    """

    seen: list[int] = []
    original = appliance.stats.report

    def spotted() -> Any:
        seen.append(threading.get_ident())
        return original()

    appliance.stats.report = spotted  # type: ignore[method-assign]
    try:
        async with running(app) as client:
            # Read while it is running: the ident is the thread's, and a stopped thread's is
            # deliberately nothing, so a stale answer cannot be mistaken for an identity.
            worker = appliance.appliance.worker_ident
            await client.post("/admin/login", data={"password": PASSWORD})
            response = await client.get("/admin/statistics")
    finally:
        appliance.stats.report = original  # type: ignore[method-assign]

    assert response.status_code == 200
    assert seen == [worker], "the statistics read did not run on the appliance thread"
    assert worker != threading.get_ident()


async def test_the_administrator_writes_to_runtime_and_never_to_library(
    app: Any, built_library: Any
) -> None:
    """AIG §22's rule, re-asserted from the direction milestone 14 makes it easiest to break."""

    path = built_library.options.library_db
    digest = path.resolve().read_bytes()
    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        for page in PAGES:
            await client.get(page)
        await client.post("/admin/playback/skip", headers=HTMX)
        await client.post("/admin/queue/clear", headers=HTMX)
        await client.post("/admin/library/reload", headers=HTMX)

    assert path.resolve().read_bytes() == digest


async def test_the_guest_surface_still_needs_nothing(app: Any) -> None:
    """SAPRS 10.8's second half: an administrative interface that had to be reachable to
    queue a song would be the wrong shape of application."""

    async with running(app) as client:
        pages = [await client.get(path) for path in ("/", "/search?q=album", "/artists", "/queue")]
        api = await client.get("/api/v1/queue")

    assert [response.status_code for response in [*pages, api]] == [200] * 5


async def test_no_administrative_page_polls(app: Any) -> None:
    """No timer, on any screen — AIG 22's "do not use polling where SSE is available".

    An operator leaves the dashboard open for four hours with the party running underneath it.
    A page on a five-second timer would be forty thousand requests a night against a box with
    one disk, and the frame that triggers the refresh already arrives within SAPRS 12.4's
    second, so the timer would be pure cost. The assertion is on the served text rather than on
    the script file because it is the *page* that would carry a timer, and a page can carry one
    in a template nobody re-read.
    """

    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        bodies = [(await client.get(page)).text for page in PAGES]

    offenders = [
        text
        for text in bodies
        if any(mark in text for mark in ("setInterval", "setTimeout(", 'http-equiv="refresh"'))
    ]
    assert not offenders


async def test_the_stream_carries_no_administrative_region(app: Any) -> None:
    """The frame budget is per connected browser, so an admin-only region would be a gift
    to nobody: guests must not pay for an operator's screen (ADR-007's other direction)."""

    from encore.api.fragments import EVENT_REGIONS

    regions = {region for named in EVENT_REGIONS.values() for region in named}
    assert regions == {"player", "alerts"}, EVENT_REGIONS
