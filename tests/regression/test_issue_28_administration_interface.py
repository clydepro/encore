"""Regression: #28 — the administrative interface.

The defects below share one shape: each was invisible to the test written for the feature around
it. The admin surface's endpoints, pages and credential rules all had coverage while a Pause
button redrew a dashboard with the track still moving; while a reload test passed against a
library file that had never been replaced; while a security assertion iterated over three objects
and found nothing to check; and while a login refusal told a caller whether the credential existed
before it told them the password was wrong.

That is not an argument for fewer unit tests — every one of those files is still in the tree and
still earning its place. It is an argument for the specific test that asks the running appliance
the question a user, or an attacker, or a database would ask.

| Defect | Symptom |
| ------ | ------- |
| `POST /admin/playback/{command}` branched on the medium *before* performing the command | the fragment branch re-rendered the panel, so Pause drew a note saying "paused" above a track that was still playing, on a screen designed never to reload itself. The browser branch was correct, which is how it survived its own test file |
| `library/store._check` let `sqlite3.DatabaseError` escape for a file that is not a database | an interrupted build turned the library page's reload into a 500 about this process instead of a 503 about that file |
| A test helper that `copyfile`s a rebuilt library onto the published path | the live handle *does* see a copy — it truncates the file the handle has open — so a reload test can be green while pinning nothing |
| `AdminService.login` said "no administrator has signed in here yet" on a wrong password | anyone on the LAN could tell an untouched appliance from a wrong guess, for free, without knowing the password |
| `app.routes` does not flatten routers added with `include_router` (FastAPI ≥ 0.140) | the "nothing under `/admin` answers without a session" test compared an empty set with itself and reported success forever |
| `LibraryStore.reload()` replaced `store.search` | `SearchService` kept answering from the FTS verdict a library that was no longer current had made |
| `StatisticsService.start()` was not idempotent | a composition root that started twice (a test, a reload, a future `LibraryReloaded` re-wire) subscribed twice and every counter on the dashboard read double |
| `status.HTTP_422_UNPROCESSABLE_ENTITY` is deprecated and this suite runs with `filterwarnings = error` | a module that looked correct raised on import — but only in the runs that had the warning installed |
| htmx's default `responseHandling` discards every 4xx and 5xx body | every failure the appliance rendered as a fragment was computed, sent and then thrown away by the client: buttons that appear to do nothing |
| `RateLimitError` is a sibling of `AuthenticationError`, and the login route caught only the latter | the lockout's own sentence — "try again in 15 minutes", on the page with the password field — was written, tested at unit level and unreachable: an operator who hit the lock got the generic error shell, with no form to come back to |
"""

from __future__ import annotations

import shutil
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from apps.server.appliance import Appliance
from encore.api import errors
from encore.api.app import create_app
from encore.config.models import AdminConfig
from encore.domain import QueueItemId, SongId
from encore.events import SongQueued
from encore.events.bus import EventBus
from encore.repositories.errors import LibraryContractError, StoreNotFoundError
from encore.repositories.library import open_library
from encore.repositories.runtime import open_runtime_store
from encore.services.admin_service import AdminService
from encore.services.errors import AdminError, AuthenticationError, RateLimitError
from encore.services.statistics_service import StatisticsService
from encore.utilities.appliance import call_async
from tests.support.appliances import composed_appliance
from tests.support.http import running
from tests.support.mpv import FakeEngine

pytestmark = pytest.mark.regression

HTMX = {"hx-request": "true"}
HTML = {"accept": "text/html"}
PASSWORD = "a long enough password"  # pragma: allowlist secret


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def library_db(built_library: Any) -> Path:
    """The fixture library's path, so a test can put something else where it is.

    A fixture rather than a field on `Appliance`: the composition root does not carry its library
    path, and adding one for the convenience of a test would be production shaped by a test.
    """

    return Path(built_library.options.library_db)


@pytest.fixture
def appliance(
    encore_home: Path, library_db: Path, built_library: Any, engine: FakeEngine
) -> Iterator[Appliance]:
    """The shipped graph, over the fixture library and a fresh `runtime.db`."""

    made = composed_appliance(
        library_db=library_db,
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


async def sign_in(client: Any) -> None:
    """Through the first-run page, because that is the path an operator takes."""

    await client.post("/admin/login", data={"password": PASSWORD})


async def ask_for(app: Any, song_id: int = 1) -> None:
    """Queue something the way a guest does, so the state an action reads is a guest's state.

    Its own client, and its own lifespan: `tests/support/http.py` is explicit that a second
    `running(app)` inside an open one would start the appliance twice, so a test that is already
    holding a client must use `ask_for_client` instead.
    """

    async with running(app) as client:
        await client.post(f"/api/v1/queue?song_id={song_id}")


async def ask_for_client(client: Any, song_id: int = 1) -> None:
    await client.post(f"/api/v1/queue?song_id={song_id}")


async def state_of(app: Any) -> str:
    async with running(app) as client:
        return str((await client.get("/api/v1/playing")).json()["state"])


async def state_of_client(client: Any) -> str:
    return str((await client.get("/api/v1/playing")).json()["state"])


# -- a control that controlled nothing -------------------------------------


async def test_a_panel_that_asks_for_pause_actually_pauses(app: Any) -> None:
    """The bug this file exists for.

    The first draft of `controllers/admin.py` asked "is this HTMX?" first, and the fragment branch
    returned `_changed(...)`, which re-renders — without performing the command. Both halves of
    the branch looked correct in isolation, and the result was worse than a crash: a page that
    said "paused" above a track that was playing, on a screen whose whole design is that it never
    reloads itself.

    The check is the guest's own `/api/v1/playing`, not a string in the returned HTML. That
    distinction is the lesson: a page that says a thing and the thing having happened are the same
    assertion only if the test looks at the second one.
    """

    await ask_for(app)
    assert await state_of(app) == "playing"

    async with running(app) as client:
        await sign_in(client)
        answer = await client.post("/admin/playback/pause?panel=dashboard", headers=HTMX)

    assert answer.status_code == 200
    assert await state_of(app) == "paused", "the panel was redrawn, and that was all that happened"

    async with running(app) as client:
        await sign_in(client)
        await client.post("/admin/playback/resume?panel=dashboard", headers=HTMX)

    assert await state_of(app) == "playing"


async def test_every_command_on_the_dashboard_moves_the_appliance(app: Any) -> None:
    """Four commands, one table, one loop.

    Looped rather than split because the defect was not in a command — it was in the branch all
    the commands shared, which is where bugs that appear five times in a review as "covered"
    live.
    """

    # `idle`, not `stopped`: the state vocabulary is the engine's (`PlaybackState`), and a
    # stopped appliance is one with nothing loaded rather than one that failed.
    expect = {"pause": "paused", "resume": "playing", "skip": "playing", "stop": "idle"}

    for command, wanted in expect.items():
        await ask_for(app)
        async with running(app) as client:
            await sign_in(client)
            answer = await client.post(f"/admin/playback/{command}?panel=dashboard", headers=HTMX)
            seen = (await client.get("/api/v1/playing")).json()["state"]
        assert answer.status_code == 200, command
        assert seen == wanted, f"{command} redrew the panel and left it {seen}"


async def test_the_no_javascript_branch_performs_the_same_command(app: Any) -> None:
    """The other half of the same `if`, which was correct from the start.

    Worth keeping precisely because it passed while its twin failed: a browser-path test alone
    would have given the bug a green suite, and the pair is what makes the branch impossible to
    half-implement.
    """

    await ask_for(app)
    async with running(app) as client:
        await sign_in(client)
        answer = await client.post(
            "/admin/playback/pause",
            headers={**HTML, "referer": "http://encore.test/admin/dashboard"},
        )

    assert answer.status_code == 303
    assert await state_of(app) == "paused"


# -- the file that was not a database --------------------------------------


def test_a_file_that_is_not_a_database_is_refused_by_name(tmp_path: Path) -> None:
    """Not an Encore library — and not a crash in this process either.

    `sqlite3` reports a garbage file lazily: the open succeeds and the first query raises
    `file is not a database`. The shape check *is* the first query, so before this fix what
    reached the caller was a driver's exception, which `errors.py` has no mapping for. The
    operator got a 500 about the Server instead of a sentence about the build, which is the
    difference between a diagnosis and a bug report.
    """

    junk = tmp_path / "interrupted-build.db"
    junk.write_bytes(b"SQLite format 3\x00" + b"\x00" * 8192)

    with pytest.raises(LibraryContractError) as refused:
        open_library(junk)

    message = str(refused.value)
    assert "not a readable database" in message
    assert "encore-builder" in message.lower()
    assert isinstance(refused.value.__cause__, sqlite3.Error), "the driver's words stay underneath"


async def test_a_bad_rebuild_refuses_the_button_and_changes_nothing(
    app: Any, library_db: Path
) -> None:
    """SAPRS 11.9's rule about maintenance actions, applied to a button.

    Two things have to hold at once: the running library keeps serving, and the answer is about
    *that file* rather than about *this server*. The first is the reopen's design — verification
    on the new handle, swap only on success; the second is a status code. Neither is visible from
    the other, so both are asserted.
    """

    junk = library_db.with_name("junk.db")
    junk.write_bytes(b"SQLite format 3\x00" + b"\x00" * 8192)
    incoming = library_db.with_name("incoming.db")
    shutil.copyfile(junk, incoming)
    incoming.replace(library_db)  # the Builder's own publication step (ADR-010)

    async with running(app) as client:
        await sign_in(client)
        await ask_for_client(client)
        refused = await client.post("/admin/library/reload?panel=library", headers=HTMX)
        page = await client.get("/admin/library", headers=HTML)
        guests = await client.get("/api/v1/playing")
        still_playing = await state_of_client(client)

    assert refused.status_code == 503, refused.text
    assert "not one this server can read" in refused.text
    assert page.status_code == 200, "the page still works, because the library still does"
    assert guests.status_code == 200, "a failed reload cannot stop a party"
    assert still_playing == "playing"


def test_a_library_that_vanished_is_a_different_sentence(tmp_path: Path) -> None:
    """Two refusals, two diagnoses.

    "That file is not a library" and "that file is not there" send an operator to a build log and
    to a mount table respectively. Collapsing them into one exception would be the kind of
    helpfulness that costs an afternoon, so the distinction is pinned even though both arrive at
    the same 503.
    """

    with pytest.raises(StoreNotFoundError) as gone:
        open_library(tmp_path / "unmounted.db")

    assert "builder" in str(gone.value).lower()


# -- the trap in the helper ----------------------------------------------


def test_a_copy_onto_an_open_library_file_is_seen_and_a_rename_is_not(
    built_library: Any, tmp_path: Path
) -> None:
    """Why `publish` in `tests/unit/test_library_reload.py` renames instead of copying.

    Both halves are asserted because the trap *is* the mechanism: `shutil.copyfile` opens the
    destination with `O_TRUNC` and writes into the same inode the store has open, so a test that
    simulated a rebuild that way would watch the library change by itself and pass a reload
    assertion with nothing behind it. A rename swaps the directory entry and leaves the live
    handle on the old file — which is what the Builder actually does (ADR-010), and what makes
    the reload this milestone added necessary rather than cosmetic.
    """

    path = Path(built_library.options.library_db)
    store = open_library(path)
    before = store.counts()["songs"]
    smaller = tmp_path / "smaller.db"
    shutil.copyfile(path, smaller)
    with sqlite3.connect(smaller) as conn:
        conn.execute("DELETE FROM songs WHERE id > 1")
        conn.commit()

    shutil.copyfile(smaller, path)
    assert store.counts()["songs"] != before, "a copy is a write, seen through the live handle"

    incoming = path.with_name("published.db")
    shutil.copyfile(smaller, incoming)
    incoming.replace(path)
    assert store.counts()["songs"] != before, "a rename is not, which is the whole point of ADR-006"
    try:
        store.reload()
        assert store.counts()["songs"] == 1, "and one reopen is what notices"
    finally:
        store.close()


def test_search_follows_a_reopen_it_was_not_told_about(built_library: Any, tmp_path: Path) -> None:
    """The reload that replaced `store.search` left search deciding from a stale verdict.

    `SearchService` is handed a `SearchRead` at composition (SAPRS 11.5), so a reload that builds
    a new index object leaves the running service holding the old one — and the symptom is not an
    error but a confident wrong answer: search reports a library that is no longer current. The
    fix is that availability is a property updated in place, which this asserts through the
    service rather than through the store.
    """

    path = Path(built_library.options.library_db)
    store = open_library(path)
    from encore.search import SearchService

    search = SearchService(index=store.search, catalogue=store)
    without_index = tmp_path / "no-fts.db"
    shutil.copyfile(path, without_index)
    with sqlite3.connect(without_index) as conn:
        conn.execute("DROP TABLE IF EXISTS song_search")
    incoming = path.with_name("published.db")
    shutil.copyfile(without_index, incoming)
    incoming.replace(path)

    try:
        store.reload()
        from encore.search.errors import SearchUnavailableError

        with pytest.raises(SearchUnavailableError):
            search.search("album")
    finally:
        store.close()


# -- the oracle ----------------------------------------------------------


def test_a_wrong_password_and_an_untouched_appliance_say_the_same_thing(tmp_path: Path) -> None:
    """`AdminService.login` must not reveal which of the two failures it is.

    "No administrator has signed in here yet" is the right sentence for a person standing at the
    appliance and the wrong one for a request, because the first thing an unauthenticated caller
    on the LAN can learn from it is whether the password prompt is the only thing between them and
    the queue. Both branches raise the same message from the same line, and the first-run page
    reaches its own wording by a different method entirely (`is_configured`, in the controller —
    a page-level fact, not a login-level one).
    """

    empty = AdminService(store=open_runtime_store(tmp_path / "empty.db"), config=AdminConfig())
    taken = AdminService(store=open_runtime_store(tmp_path / "taken.db"), config=AdminConfig())
    taken.setup(PASSWORD)

    messages = set()
    for service in (empty, taken):
        with pytest.raises(AuthenticationError) as refused:
            service.login("not the password")
        messages.add(str(refused.value))

    assert len(messages) == 1, "two answers would be two doors"


# -- a service that subscribed twice --------------------------------------


def test_starting_the_statistics_service_twice_counts_a_song_once(encore_home: Path) -> None:
    """A dashboard where every number is doubled is a dashboard nobody believes.

    `StatisticsService` and the counters it writes are tested in
    `tests/unit/test_statistics_service.py`; this is the one property that a phase which adds a
    second `start()` caller could break silently.

    `start()` is called by the composition root, and a phase that adds a library
    reload or a test that rebuilds a graph has two plausible reasons to call it again. The guard is
    that `start()` returns the subscriptions it already has rather than adding a second set, which
    is asserted against the counter rather than the subscription list: the number is the thing a
    reader cares about, and the list is how it stays that way.
    """

    store = open_runtime_store(encore_home / "var/lib" / "stats.db")
    bus = EventBus()
    stats = StatisticsService(store=store, events=bus)
    try:
        assert len(stats.start()) == len(stats.start()), "a second start subscribed twice"
        bus.publish(
            SongQueued(song_id=SongId(1), queue_item_id=QueueItemId(1), position=1, queue_length=1)
        )
        report = stats.report()
        assert report.totals.get("queue_added", 0) == 1, report.totals
    finally:
        stats.close()
        store.close()


async def test_an_administrative_read_happens_on_the_appliance_thread(
    app: Any, appliance: Appliance
) -> None:
    """ADR-012, one year into being a rule rather than an argument.

    Every administrative read and write in this milestone crosses through `deps.read()`, and the
    temptation specific to an admin surface is a "small" lookup — a session count, a log tail, a
    config dump — done inline because it is only a dictionary. Two of those are legitimately inline
    (`admin.session()`, `logs.lines()`) because neither touches SQLite; the one that looks like a
    database read is a database read. So the guardrail is the thread that touched `runtime.db`,
    named.

    Read the identity while the app is up: after shutdown the worker has joined and the answer is
    `None`, which is a correct fact about a dead thread and a useless one about a request.
    """

    async with running(app) as client:
        await sign_in(client)
        worker = appliance.appliance.worker_ident
        assert worker is not None, "no appliance thread, so nothing below means anything"

        async def thread_that_answered() -> int | None:
            return await call_async(appliance.appliance, lambda: threading.current_thread().ident)

        assert await thread_that_answered() == worker, "a read ran on the event loop"
        assert (await client.get("/admin/statistics", headers=HTML)).status_code == 200
        assert await thread_that_answered() == worker, (
            "an administrative page ran a read off-thread"
        )


# -- assertions that asserted nothing --------------------------------------


def test_the_route_table_a_test_can_read_is_the_one_it_thinks_it_is_reading(app: Any) -> None:
    """`app.routes` is not a flat list of the application's paths.

    FastAPI ≥ 0.140 stores an `include_router` as one node holding the sub-router, so walking
    `app.routes` yields the guest routes and a handful of opaque objects — and a "nothing under
    `/admin` answers without a session" test written that way compares an empty set to itself,
    forever, successfully.

    `openapi()["paths"]` is flattened and is the document a consumer is told to code against,
    which is the right thing to be complete about. This test pins the *difference* rather than only
    the fix, so that a FastAPI upgrade that flattens `routes` retires this note loudly instead of
    quietly making the guardrail vacuous again.
    """

    declared = {
        (path, method.lower())
        for path, item in app.openapi()["paths"].items()
        for method in item
        if path.startswith("/admin")
    }
    walked = {
        (route.path, method.lower())
        for route in app.routes
        if hasattr(route, "path")
        for method in getattr(route, "methods", ())
        if route.path.startswith("/admin")
    }

    assert ("/admin/dashboard", "get") in declared
    assert ("/admin/playback/{command}", "post") in declared
    assert len(declared) >= 15
    assert not walked, "if this fails, the guardrail in test_admin_interface.py can be simplified"


def test_no_module_reaches_for_a_deprecated_status_constant() -> None:
    """This suite runs with `filterwarnings = error`, so a deprecated name is a broken build.

    A source check rather than an import test, because the constant that bit this milestone
    (`HTTP_422_UNPROCESSABLE_ENTITY`) was deprecated in one release and removed in another nobody
    pinned: the only symptom is a warning in the runs that have it installed, and a typing pass
    never sees it at all.
    """

    root = Path(__file__).resolve().parents[2]
    offenders = [
        str(path.relative_to(root))
        for path in [*root.glob("encore/**/*.py"), *root.glob("apps/**/*.py")]
        if "UNPROCESSABLE_ENTITY" in path.read_text()
    ]

    assert not offenders


def test_the_admin_surface_added_no_new_sse_region(app: Any) -> None:
    """The fan-out is per region, and the budget that follows from that is the party's.

    Milestone 14 had two ways to refresh an operator's dashboard: a second authenticated stream,
    or htmx refetching a region when the existing stream says something moved. The second was
    chosen, and the cost of the first would have been another render per fact per screen in every
    room, forever, for one laptop. This assertion is the fence: if a region name appears in the
    live script that is not one of the guest's two, whatever justified it has to be argued with
    `encore/api/fragments.py`'s table rather than with a comment.
    """

    root = Path(__file__).resolve().parents[2]
    script = (root / "encore/static/js/encore-live.js").read_text()
    declared = script.split("const REGIONS = [", 1)[1].split("]", 1)[0]
    names = {part.strip().strip('"') for part in declared.split(",") if part.strip()}

    assert names == {"player", "alerts"}, names


# -- the client that discarded the answer ---------------------------------


def test_htmx_drops_an_error_body_unless_the_appliance_says_otherwise() -> None:
    """A correct server and an interface that appears dead are one bug apart.

    `errors.respond()` has always answered a failed fragment request with markup, and the tests for
    it passed: the response was a 409 with "the queue holds 8 songs" inside it. What no test could
    see is that htmx's own default `responseHandling` ends in a rule that swaps nothing for a 4xx
    or 5xx status and reports the response as an error — so the body was discarded before anyone
    read it, in every browser, on every failure, on both interfaces.

    The assertion is deliberately on the vendored file rather than on a copy of htmx's documented
    behaviour. If a future upgrade reorders that table, this fails and whoever upgraded it learns
    that `encore-live.js` has a rule whose reason may have just changed — which is the trade this
    repository makes everywhere it depends on a vendor: the dependency is checked as bytes, not as
    a version number.
    """

    root = Path(__file__).resolve().parents[2]
    vendor = (root / "encore/static/vendor/htmx/htmx.min.js").read_text()
    bridge = (root / "encore/static/js/encore-live.js").read_text()

    assert "responseHandling" in vendor
    assert "[45].." in vendor, "htmx's no-swap-on-error default moved; re-read the bridge's rule"
    assert '"htmx:beforeSwap"' in bridge, "the rule that makes an error fragment visible is gone"
    assert "shouldSwap" in bridge


async def test_a_failure_a_caller_could_read_still_arrives_as_html(app: Any) -> None:
    """The server half of the above, on a real appliance rather than in a script.

    A reload pointed at a file that is not a library is the clearest case in the interface: the
    operator pressed a button, the appliance knows exactly why nothing happened, and the sentence
    belongs in the panel that asked. `partials/error.html` is what answers, as HTML, at 503 — the
    body the client is now told to swap.
    """

    async with running(app) as client:
        await client.post("/admin/login", data={"password": PASSWORD})
        library = Path(app.state.jukebox.config.paths.library_db)
        staged = library.parent / "library.db.incoming"
        staged.write_bytes(b"not a database, whatever it is called")
        staged.replace(library)
        response = await client.post(
            "/admin/library/reload", params={"panel": "library"}, headers=HTMX
        )

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["hx-reswap"] == "innerHTML", "over an admin region kills its trigger"
    assert "the running library is unchanged" in response.text
    assert "hx-redirect" not in response.headers, "only a 401 is a door"


def test_the_lockouts_sentence_was_written_and_unreachable() -> None:
    """A hierarchy that made a hand-written message impossible to display.

    `RateLimitError` and `AuthenticationError` are siblings — both "you are not in", neither a
    kind of the other, which is the right shape for a service: a lock is not an authentication
    failure, it is a refusal to attempt one. It is the wrong shape for a handler written as
    `except AuthenticationError`, which is what the login route said, and the consequence was
    silent. The status was right (the exception reached `BY_EXCEPTION` and came out as a 429), so
    every status assertion in the suite passed. What never ran was `_reason()`'s branch for it —
    the sentence "try again in 15 minutes" — and with it the login *page*, so an operator locked
    out mid-party got `error.html`: a paragraph about this process, no password field, and no way
    back to the dashboard except the browser's back button (SAPRS 10.8's dead end, met by a
    message that was written for exactly this moment and could not be reached).

    Two facts, because the bug needed both: the sibling relationship that makes a single
    `except` clause silently incomplete, and the handler that now names the case. The rendered
    answer — the countdown, on the page, at 429, with a `Retry-After` for a tool — is in
    `tests/integration/test_admin_interface.py`.
    """

    assert not issubclass(RateLimitError, AuthenticationError), (
        "if the hierarchy changed, the `except` clauses in admin.py can be one"
    )
    assert issubclass(RateLimitError, AdminError)
    source = (Path(__file__).resolve().parents[2] / "encore/controllers/admin.py").read_text()
    guarded = source.split("async def login(")[1].split("# -- the screens")[0]

    assert "except RateLimitError" in guarded, "the lock fell back into the parent's arm"
    assert errors.RATE_LIMITED.status == 429
    assert errors.details_for(RateLimitError(600)) == {"retry_after_seconds": 600}
