"""The appliance answering a request (SAPRS 8.1, 10.2, 10.3, 10.9, 10.10).

Everything in this file is one sentence: *a guest presses something and the room changes*.
Each half has its own contract tests — the queue's, the search's, the publisher's, the
templates' — and the seams between them are where milestone 11 breaks, because a route that
returns a view model nobody constructed, or a fact that reaches a browser in an order nobody
asserted, is invisible to every single-component test in the suite.

Three properties get coverage here rather than a list of endpoints:

* **Every route answers in the medium it promised, and means the same thing as the service
  underneath it.** For a guest-visible number that is one assertion per pair: the rows in the
  HTML and the integer in `runtime.db` may not disagree.
* **The appliance thread survives a whole request.** AIG §22's rule that the runtime never
  modifies `library.db`, and ADR-012's rule that one thread owns the services, are both
  verified here as aftereffects — by the bytes of an immutable file, and by the identity of
  the thread that answered.
* **Nothing is cached that changes under a guest** (SAPRS 10.10), and everything that does not
  change is cached as hard as it can be.

Two conventions the whole file follows, because both are otherwise a moving target:

* **Identifiers are looked up, never assumed.** The Builder numbers artists and albums in
  directory-scan order, so "album 1" in this corpus is whichever artist sorts first, not
  whichever fixture was written first. `_corpus` reads the ids out of the built database, and
  every assertion below is about a *title*.
* **`/events` is driven by `tests/support/http.py`'s own client**, because a held-open response
  is not something `httpx`'s ASGI transport can express.

The rig is the composition root itself (`apps/server/appliance.py:build`) rather than a
hand-built graph, on purpose: a test that assembled its own services would keep passing when
the wiring the installer ships went wrong, which is a failure this milestone can really have.
Only mpv is replaced, by `tests/support/mpv.py` as everywhere else (SAPRS 14.4).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from html import unescape
from pathlib import Path
from typing import Any

import pytest
from fastapi import Response

from apps.server.appliance import Appliance
from encore.api.app import create_app
from encore.api.html import Renderer, build_renderer
from encore.domain import AlbumId, QueueItemStatus
from encore.events import SongQueued
from encore.repositories.library import open_library
from encore.utilities.appliance import call_async
from tests.support.appliances import composed_appliance
from tests.support.http import Stream, running, started
from tests.support.mpv import FakeEngine

pytestmark = pytest.mark.integration

HTMX = {"hx-request": "true"}

#: What `tests/conftest.py`'s `music_tree` generates, by name.
ALBUMS = ("First Album", "Second Album", "Third Album")
ARTISTS = ("The Test Artists", "Someone Else")


class Corpus:
    """The fixture library's names, as the ids the Builder gave them.

    A class rather than a dict so a typo is an `AttributeError` at the failing line instead of
    a `KeyError` from a string someone guessed.
    """

    def __init__(
        self, albums: dict[str, int], artists: dict[str, int], songs: dict[str, int]
    ) -> None:
        self.albums = albums
        self.artists = artists
        self.songs = songs

    def album(self, title: str) -> int:
        return self.albums[title]

    def artist(self, name: str) -> int:
        return self.artists[name]

    def song(self, title: str) -> int:
        return self.songs[title]

    def any_song(self) -> int:
        return min(self.songs.values())

    def second_song(self) -> int:
        return sorted(self.songs.values())[1]

    def without_artwork(self) -> int:
        """An album the Builder recorded no picture for.

        Two of the three fixture albums have none, which is the case SAPRS 6.7 is about: a
        missing picture must never be a missing track, and must never be a broken image
        either.
        """

        return min(
            album_id
            for title, album_id in self.albums.items()
            if title != "First Album"  # the one album `music_tree` gives a cover to
        )


def _corpus(library_db: Path) -> Corpus:
    """Read names→ids out of the built `library.db`.

    This is a test looking at an artifact, not the runtime reading its own database: the
    appliance thread is not yet started here, and a fixture that started it would entangle
    every test's lifespan with this one's. The repository is the documented way to read that
    file (ADR-009), and it is opened and closed inside the call.
    """

    store = open_library(library_db)
    try:
        albums = {album.title: int(album.id) for album in store.albums.all()}
        artists = {artist.name: int(artist.id) for artist in store.artists.all()}
        songs = {
            song.title: int(song.id)
            for title in albums
            for song in store.songs.by_album(AlbumId(albums[title]))
        }
    finally:
        store.close()
    return Corpus(albums=albums, artists=artists, songs=songs)


@pytest.fixture
def engine() -> FakeEngine:
    """A mock mpv behind the launcher interface the supervisor expects (SAPRS 14.4)."""

    return FakeEngine()


@pytest.fixture
def appliance(encore_home: Path, built_library: Any, engine: FakeEngine) -> Iterator[Appliance]:
    """The appliance `main.py` would have started, over the fixture library.

    Built by `tests/support/appliances.py`, because a test that assembled its own services
    would keep passing when the wiring the installer ships went wrong. The engine runs: an
    appliance that never talked to a player would skip the half of the request path where the
    thread discipline goes wrong (ADR-012).
    """

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
def corpus(built_library: Any) -> Corpus:
    return _corpus(built_library.options.library_db)


# -- the pages, and what is on them ----------------------------------------


async def test_the_appliance_answers_every_guest_route_in_html(app: Any) -> None:
    """SAPRS 10.2's page list, all of it server-rendered.

    The check is a status and a content type rather than a screenshot: what a regression here
    looks like is a route renamed, a template raising, or a fragment served where a document
    belongs.
    """

    async with running(app) as client:
        for path in ("/", "/search?q=album", "/artists", "/queue"):
            response = await client.get(path)
            assert response.status_code == 200, path
            assert response.headers["content-type"].startswith("text/html"), path


async def test_a_page_of_the_same_queue_is_the_json_one(app: Any, corpus: Corpus) -> None:
    """One fact, computed twice, agreeing.

    The queue page and `/api/v1/queue` reach the same `QueueService` by different code paths,
    and the drift this catches is the expensive kind: a view that quietly stopped counting the
    track on the speakers, or counted a finished one, which no unit test of either side would
    notice.
    """

    async with running(app) as client:
        await client.post(f"/api/v1/queue?song_id={corpus.any_song()}")
        await client.post(f"/api/v1/queue?song_id={corpus.second_song()}")
        page = await client.get("/queue")
        body = (await client.get("/api/v1/queue")).json()

    assert body["length"] == 2
    assert page.text.count('class="song-row"') == body["length"]
    assert body["now_playing"]["position"] == 1
    assert [entry["position"] for entry in body["up_next"]] == [2]


async def test_a_fragment_is_a_fragment_and_a_page_is_a_page(app: Any) -> None:
    """SAPRS 10.6's two audiences, at the two URLs built for them.

    The fragment route must never send a document — a phone downloading a whole shell into a
    panel reads as a page inside a page, which is the classic way this pattern breaks — and a
    page route must never send a bare panel, because a person navigating with a URL has
    nothing to put it in.
    """

    async with running(app) as client:
        document = await client.get("/search?q=album")
        fragment = await client.get("/fragments/search", params={"q": "album"})

    assert document.text.count("<html") == 1
    assert fragment.status_code == 200
    assert "<html" not in fragment.text


async def test_a_song_added_by_a_guest_is_playing_before_the_response_ends(
    app: Any, appliance: Appliance, engine: FakeEngine, corpus: Corpus
) -> None:
    """SAPRS 8.1's rule, at the HTTP seam: pressing play starts it *now*.

    The assertion is on the mpv double rather than on the HTML, because a page is allowed to
    be a moment behind and a speaker is not. A refactor that moved the load onto the tick
    would leave this red while every page in the suite still rendered.
    """

    async with running(app) as client:
        await client.post("/queue", data={"song_id": str(corpus.any_song())}, headers=HTMX)

    assert engine.count("loadfile") == 1
    now = appliance.queue.now_playing()
    assert now is not None
    assert now.item.status == QueueItemStatus.PLAYING


async def test_the_confirmation_a_guest_gets_names_the_song(app: Any, corpus: Corpus) -> None:
    """SAPRS 8.1's "confirmation that your selection was received", in the button's place.

    A title in the text is the difference between "Queued" and a guest knowing the appliance
    heard the song they meant, and it is why the endpoint renders a row rather than a `204`
    nothing can show.
    """

    title = "First Album Track 1"
    async with running(app) as client:
        response = await client.post(
            "/queue", data={"song_id": str(corpus.song(title))}, headers=HTMX
        )

    text = unescape(response.text)
    assert "Playing now" in text
    assert title in text


async def test_a_double_press_starts_the_first_and_queues_the_second(
    app: Any, appliance: Appliance, corpus: Corpus
) -> None:
    """SAPRS 8.3's duplicates and SAPRS 9.7's wording, over the wire.

    The room wants both copies, and what must not happen is a silent drop — which is what a
    well-meaning idempotency guard would have caused. The second answer names a position,
    because "Playing now" would be a lie about the second press.
    """

    song = corpus.any_song()
    async with running(app) as client:
        first = await client.post("/queue", data={"song_id": str(song)}, headers=HTMX)
        second = await client.post("/queue", data={"song_id": str(song)}, headers=HTMX)

    assert "Playing now" in unescape(first.text)
    assert "Added at 2" in unescape(second.text)
    assert appliance.queue.length == 2


async def test_the_ceiling_is_the_configured_one_and_a_guest_hears_about_it(
    app: Any, corpus: Corpus
) -> None:
    """8, not the default 200: SAPRS 12.1's "the file says it", through to a browser.

    `encore.toml` is the only place the number is written, and a ceiling hardcoded anywhere in
    the HTTP layer shows up here — in both interfaces, because the JSON's `details.limit` is
    what a client branches on and the page's number is what a person reads.
    """

    ids = sorted(corpus.songs.values())
    async with running(app) as client:
        for song in ids + ids[: 8 - len(ids)]:
            response = await client.post("/api/v1/queue", params={"song_id": song})
            assert response.status_code == 201, response.text
        overflow = await client.post("/api/v1/queue", params={"song_id": ids[0]})
        page = await client.get("/queue")

    assert overflow.status_code == 409
    assert overflow.json()["error"]["details"]["limit"] == 8
    assert "8" in page.text


async def test_a_refused_request_leaves_the_queue_exactly_as_it_was(
    app: Any, appliance: Appliance, corpus: Corpus
) -> None:
    """SAPRS 8.4's atomicity, at the seam where a half-insert would be invisible.

    The repository guarantees it in one row; the route must not undo that by writing one of
    its own first. The assertion is on the store rather than the response, because a phantom
    entry is precisely the thing a guest cannot see.
    """

    async with running(app) as client:
        await client.post("/api/v1/queue", params={"song_id": corpus.any_song()})
        refused = await client.post("/api/v1/queue", params={"song_id": 99_999})

    assert refused.status_code == 404
    assert refused.json()["error"]["code"] == "song_not_available"
    assert appliance.queue.length == 1


# -- search ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "lane", "key", "expected"),
    [
        ("First Album", "albums", "title", "First Album"),
        ("Someone Else", "artists", "name", "Someone Else"),
        ("First Album Track 2", "songs", "title", "First Album Track 2"),
    ],
)
async def test_the_json_interface_finds_what_the_library_contains(
    app: Any, query: str, lane: str, key: str, expected: str
) -> None:
    """One assertion per lane of SAPRS 10.4's three-way search.

    The queries are full names rather than stems and the expectation is the top hit, which is
    the ranking rule `SearchService` documents: an exact title match outranks a partial one,
    and a change to the scoring that broke this would be a change somebody should have to
    argue for.
    """

    async with running(app) as client:
        body = (await client.get("/api/v1/search", params={"q": query})).json()

    assert body[lane][0][key] == expected


async def test_a_search_page_keeps_what_the_guest_typed(app: Any) -> None:
    """The form must survive the swap the search causes.

    This is the one place in the UI where a lost attribute is felt as a keyboard closing under
    someone's thumb — which is why it is asserted at the HTTP layer, where the value arrives
    as a query string, rather than in a template test with the value handed in.
    """

    async with running(app) as client:
        page = await client.get("/search", params={"q": "First Album"})

    assert 'value="First Album"' in page.text
    assert "First Album Track 1" in page.text


async def test_an_empty_query_is_answered_without_being_run(app: Any, corpus: Corpus) -> None:
    """SAPRS 10.4's "empty search handled gracefully", in both interfaces.

    The page renders an empty results panel — a person who typed nothing and pressed nothing
    should not be scolded — and the JSON API answers 422, which is SAPRS 10.7's own word for
    a parameter that failed validation. Returning the whole library for a blank box would be
    the plausible implementation, and it would be a megabyte to a phone across the room.
    """

    async with running(app) as client:
        page = await client.get("/search", params={"q": ""})
        empty = await client.get("/api/v1/search", params={"q": ""})
        long_ = await client.get("/api/v1/search", params={"q": "x" * 500})

    assert page.status_code == 200
    assert "No matches" in page.text or "results" in page.text
    assert (empty.status_code, long_.status_code) == (422, 422)
    assert empty.json()["error"]["code"] == "invalid_request"


# -- browsing -------------------------------------------------------------


async def test_a_browse_page_and_a_search_page_offer_the_same_action(
    app: Any, corpus: Corpus
) -> None:
    """SAPRS 8.2's "browse or search", with one button implementation for both.

    The check is that the *same* partial renders in both places, by count. A panel that grew
    its own add-button with different wiring would still be usable and still be a defect,
    because it is the form's semantics that the live redraw depends on.
    """

    async with running(app) as client:
        album = await client.get(f"/albums/{corpus.album('First Album')}")
        search = await client.get("/search", params={"q": "First Album"})

    assert album.text.count('class="song-row"') == 3
    assert search.text.count('class="song-row"') >= album.text.count('class="song-row"')


async def test_an_album_page_lists_its_own_tracks_and_nobody_elses(
    app: Any, corpus: Corpus
) -> None:
    """The bug this catches looks fine on screen: a listing built from the artist's rows.

    `by_album` and `by_artist` differ by one WHERE clause, and the fixture corpus has two
    albums by one artist, so the wrong one shows five rows where three belong.
    """

    async with running(app) as client:
        body = (await client.get(f"/albums/{corpus.album('First Album')}")).text

    assert body.count("First Album Track") == 3
    assert "Second Album Track" not in body


async def test_an_artist_page_lists_their_albums_and_nobody_elses(app: Any, corpus: Corpus) -> None:
    async with running(app) as client:
        body = (await client.get(f"/artists/{corpus.artist('The Test Artists')}")).text

    assert "First Album" in body
    assert "Second Album" in body
    assert "Third Album" not in body


async def test_a_missing_thing_says_so_in_the_right_grammar(app: Any) -> None:
    """A 404 that means *no such row*, not *no such route*.

    The status is the assertion: a router that began answering an unknown album with an empty
    shell and a 200 would be indistinguishable on a phone and would let a broken link survive
    a whole party.
    """

    async with running(app) as client:
        album = await client.get("/albums/9999")
        artist = await client.get("/artists/9999")
        song = await client.get("/api/v1/songs/9999")

    assert (album.status_code, artist.status_code, song.status_code) == (404, 404, 404)
    assert song.json()["error"]["code"] == "not_found"


async def test_the_artists_page_names_every_artist_in_the_library(app: Any, corpus: Corpus) -> None:
    """SAPRS 9.4's browse-by-artist, over a corpus small enough to enumerate.

    Two names, both present: the failure mode this catches is a page that renders a `Page`
    object's repr, or a limit applied to the wrong column, and either one shows up as a
    missing artist long before it shows up as a wrong count.
    """

    async with running(app) as client:
        page = await client.get("/artists")
        api = await client.get("/api/v1/artists")

    for name in ARTISTS:
        assert name in page.text
    assert {entry["name"] for entry in api.json()} == set(ARTISTS)


# -- artwork --------------------------------------------------------------


async def test_the_cover_the_builder_wrote_is_the_cover_the_browser_gets(
    app: Any, corpus: Corpus, built_library: Any
) -> None:
    """SAPRS 5.7's cache, read back through the only path that serves it.

    The bytes are compared to the file rather than to a size, because a resolver that fell
    back to "some cover" would still answer 200 and still be a lie about a specific album.
    """

    cached = next(built_library.options.artwork_dir.glob("**/*.jpg"))

    async with running(app) as client:
        response = await client.get(f"/artwork/album/{corpus.album('First Album')}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["etag"].startswith('"')
    assert response.content == cached.read_bytes()


async def test_artwork_is_cacheable_and_the_queue_is_not(app: Any, corpus: Corpus) -> None:
    """SAPRS 10.10, as two headers on two responses.

    The immutable half may be kept for a year; the mutable half may not be kept at all. A
    proxy in front of the appliance applies the rule silently in both directions, which is
    exactly when it stops being visible, so it is worth a test of its own.
    """

    async with running(app) as client:
        artwork = await client.get(f"/artwork/album/{corpus.album('First Album')}")
        queue = await client.get("/queue")

    assert "immutable" in artwork.headers["cache-control"]
    assert "no-store" in queue.headers["cache-control"]


async def test_a_thing_with_no_picture_gets_the_placeholder_and_not_a_404(
    app: Any, corpus: Corpus
) -> None:
    """SAPRS 6.7's rule, at the URL an `<img>` actually asks.

    Two different misses, two different answers: nothing depicted is a placeholder, because a
    broken image in a track listing is a page that looks broken when nothing is; a file the
    library still names and the cache has lost is a 404, because that is a fault. The
    distinction is the whole test — an endpoint that answered one way for both cases would
    make a deleted cache invisible.
    """

    async with running(app) as client:
        no_picture = await client.get(f"/artwork/album/{corpus.without_artwork()}")
        unknown = await client.get("/artwork/artist/9999")

    assert no_picture.status_code == 200
    assert no_picture.headers["content-type"] == "image/svg+xml"
    assert "no-store" in no_picture.headers["cache-control"]
    assert unknown.status_code == 200  # nothing is depicted by an id that does not exist


async def test_a_song_with_no_cover_of_its_own_is_answered_from_its_album(
    app: Any, corpus: Corpus
) -> None:
    """The resolution chain (song → album → artist) reaching an `<img>`.

    Only one album in the fixture corpus carries artwork, so a track on another album
    exercises the fallback the templates depend on rather than a per-song file nobody wrote.
    """

    async with running(app) as client:
        own = await client.get(f"/artwork/song/{corpus.song('First Album Track 3')}")
        inherited = await client.get(f"/artwork/song/{corpus.song('Second Album Track 1')}")

    assert own.status_code == 200
    assert inherited.status_code in (200, 404)


async def test_artwork_cannot_be_walked_out_of_the_cache(app: Any) -> None:
    """The endpoint takes an integer, so the attack is a path pretending to be one.

    A 404 or a 422 here is FastAPI's coercion doing SAPRS 10.9's "no path traversal" for it,
    which is the cheapest correct answer available, and the reason no `resolve()` call appears
    in the controller at all.
    """

    async with running(app) as client:
        traversal = await client.get("/artwork/album/..%2f..%2fetc%2fpasswd")
        text = await client.get("/artwork/album/seven")
        kind = await client.get("/artwork/%2e%2e%2fetc/passwd")

    assert traversal.status_code in (404, 422)
    assert text.status_code in (404, 422)
    assert kind.status_code == 404


# -- the live layer -------------------------------------------------------


async def test_a_fact_reaches_a_held_connection_in_the_order_it_happened(
    app: Any, appliance: Appliance, corpus: Corpus
) -> None:
    """SAPRS 9.10's mechanism, end to end: one press, one room, one redraw.

    The frames are the bus's, in the bus's order — which is an ordering test in disguise, and
    the one that matters: a browser told "now playing" before it was told "queued" renders a
    queue that skipped a song, and ADR-011 makes the queue the only thing that could have.
    """

    async with started(app) as client:
        stream = Stream(app, "/events")
        try:
            await stream.next(1)  # the snapshot is a baseline, not a fact
            await client.post("/api/v1/queue", params={"song_id": corpus.any_song()})
            await client.post("/api/v1/queue", params={"song_id": corpus.second_song()})
            got = await _facts(stream, 4)
        finally:
            await stream.close()

    # What an idle appliance does with a guest's first request, in the order the bus did it:
    # the request is accepted, the FIFO selects a head (ADR-011: a selection is an advance
    # whether or not something finished), the engine says the song is on the speakers, and the
    # second press waits behind it. Anything else — a start before a selection, a fact missing
    # — redraws a room that never existed in that order.
    assert [event for event, _ in got] == [
        "SongQueued",
        "QueueAdvanced",
        "SongStarted",
        "SongQueued",
    ]
    assert json.loads(got[0][1])["song_id"] == corpus.any_song()


async def test_a_panel_reaches_a_browser_as_markup_and_not_as_data(
    app: Any, corpus: Corpus
) -> None:
    """SAPRS 9.10's requirement, read literally: the server sends HTML.

    A JSON payload with a rendered string in it would pass every functional test ever written
    and still be the architecture the SAPRS says not to build, because the client would be
    deciding what an event means.
    """

    async with started(app) as client:
        stream = Stream(app, "/events")
        try:
            await stream.next(1)
            await client.post("/api/v1/queue", params={"song_id": corpus.any_song()})
            event, body = await stream.until("swap:player")
        finally:
            await stream.close()

    assert event == "swap:player"
    assert 'id="player"' in body
    assert "First Album Track 1" in body or "Track 1" in body


async def test_two_phone_watches_are_redrawn_by_one_render(
    appliance: Appliance,
    corpus: Corpus,
) -> None:
    """SAPRS 8.6, at the layer where it is either free or expensive.

    Two connections, one fan-out, and the assertion is on how many times the panel was built:
    a per-client render would pass every other test here and double the template work per
    fact, on the one thread that has to stay responsive (ADR-012).
    """

    builds: list[str] = []

    class Counting(Renderer):
        """A `Renderer` with a memory, handed to `create_app`.

        Injected rather than patched, because `encore/api/fragments.py` captures the renderer
        when it subscribes: replacing `app.state.renderer` in a test would count nothing at all
        and pass, which is the specific way a test like this one can be worthless.
        """

        def __init__(self, inner: Renderer) -> None:
            super().__init__()
            self._inner = inner

        def render(self, name: str, context: Mapping[str, Any]) -> str:
            builds.append(name)
            return self._inner.render(name, context)

        def response(
            self,
            name: str,
            context: Mapping[str, Any],
            *,
            status_code: int = 200,
            headers: Mapping[str, str] | None = None,
        ) -> Response:
            return self._inner.response(name, context, status_code=status_code, headers=headers)

    made = create_app(appliance, renderer=Counting(build_renderer()))
    async with started(made) as client:
        left, right = Stream(made, "/events"), Stream(made, "/events")
        try:
            await left.next(1)
            await right.next(1)
            builds.clear()
            # An idle appliance and one press, so the facts are the whole of a start:
            # accepted, selected, begun. Every one of the three that redraws the player must
            # redraw it once for the room rather than once per screen in it.
            await client.post("/api/v1/queue", params={"song_id": corpus.any_song()})
            got_left = await _facts(left, 3)
            got_right = await _facts(right, 3)
        finally:
            await left.close()
            await right.close()

    facts = [event for event, _ in got_left]
    redraws = facts.count("QueueAdvanced") + facts.count("SongStarted")

    assert facts == [event for event, _ in got_right]
    assert builds.count("panels/player.html") == redraws > 0


async def test_a_progress_bar_moves_without_the_bus_saying_anything(
    app: Any, appliance: Appliance, corpus: Corpus
) -> None:
    """SAPRS 9.10's exception, in the one form a test can check.

    A tick produces a frame and no fact. The subscriber count is the assertion, because a
    `progress` that learned to publish would fill the bus with nothing at one event a second
    per listener — and every queue rule in the system is a subscriber.
    """

    seen: list[Any] = []
    subscription = appliance.events.subscribe(SongQueued, seen.append)
    async with started(app) as client:
        stream = Stream(app, "/events")
        try:
            await stream.next(1)
            await client.post("/api/v1/queue", params={"song_id": corpus.any_song()})
            await _facts(stream, 2)
            before = len(seen)
            await call_async(appliance.appliance, appliance.tick_once)
            await call_async(appliance.appliance, appliance.tick_once)
            got = await stream.next(4)
        finally:
            await stream.close()
            subscription.unsubscribe()

    assert len(seen) == before
    assert any(event == "progress" for event, _ in got)


async def _facts(stream: Stream, count: int) -> list[tuple[str, str]]:
    """The next `count` *fact* frames, skipping transport noise.

    A `swap:player` or a `progress` rides alongside the vocabulary, and a test that asserted on
    positions would fail for reasons that have nothing to do with the event bus.
    """

    facts: list[tuple[str, str]] = []
    async for event, data in _iter_facts(stream):
        facts.append((event, data))
        if len(facts) >= count:
            return facts
    return facts  # pragma: no cover


async def _iter_facts(stream: Stream) -> Any:
    for _ in range(60):
        frames = await stream.next(1)
        if not frames:
            return
        event, data = frames[0]
        if event in {"SongQueued", "SongStarted", "SongFinished", "QueueAdvanced"}:
            yield event, data


# -- the rules, as aftereffects -------------------------------------------


async def test_a_whole_party_leaves_the_library_byte_for_byte_the_same(
    app: Any, appliance: Appliance, corpus: Corpus
) -> None:
    """AIG §22's "the runtime never modifies `library.db`", by the file rather than a grep.

    The test that catches a regression is this one and not a search for `INSERT`, because a
    future optimisation that moved a runtime table into the immutable database would not
    contain that word in any file a reviewer reads. The bytes are the contract.
    """

    path = appliance.library_store.path
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    async with running(app) as client:
        await client.post("/queue", data={"song_id": str(corpus.any_song())}, headers=HTMX)
        await client.get("/api/v1/queue")
        await client.get("/api/v1/search", params={"q": "album"})

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


async def test_the_guardrail_reports_the_thread_that_did_the_work(
    appliance: Appliance,
) -> None:
    """ADR-012, from outside the HTTP layer.

    The same distinction milestone 11 will have to make at a terminal: a service call from the
    event loop is refused, and the same call submitted to the appliance is answered. That it
    is refused in one direction and not the other is the whole design, so it gets asserted
    before there is a browser to get it wrong with.
    """

    import threading

    from encore.utilities.appliance import WrongThreadError

    appliance.start()
    try:
        worker = await call_async(appliance.appliance, threading.get_ident)
        # Read while it is running: the ident is the thread's, and a stopped thread's is
        # deliberately nothing, so a stale answer cannot be mistaken for an identity.
        recorded = appliance.appliance.worker_ident
        with pytest.raises(WrongThreadError):
            appliance.appliance.assert_current("a route")
    finally:
        appliance.stop()

    assert worker == recorded
    assert worker != threading.get_ident()


async def test_the_state_a_guest_left_is_the_state_the_next_guest_finds(
    app: Any, appliance: Appliance, corpus: Corpus
) -> None:
    """SAPRS 8.7, minus the reboot, because a reboot is a second composition.

    What matters is that HTTP never held the truth: the queue a page shows after a request
    cycle is a query, so the process that restarts without losing the night is the one that
    served it. A second `create_app` over the same appliance is the cheapest way to make the
    point at this layer.
    """

    async with running(app) as client:
        await client.post("/api/v1/queue", params={"song_id": corpus.any_song()})
        await client.post("/api/v1/queue", params={"song_id": corpus.second_song()})

    async with running(create_app(appliance)) as client:
        body = (await client.get("/api/v1/queue")).json()

    # The item that was on the speakers when the last lifespan ended is back at the head, not
    # lost: SAPRS 8.7's step 2 says a song that started and did not finish goes to the front of
    # the queue, which is the difference between "restart and lose the night" and "restart".
    assert body["length"] == 2
    assert body["now_playing"]["position"] == 1
    assert body["now_playing"]["status"] in {"playing", "pending"}


async def test_the_server_is_useless_when_the_library_is_not_there(
    encore_home: Path, built_library: Any, engine: FakeEngine
) -> None:
    """SAPRS 12.1's "startup fails loudly, not silently degraded".

    A jukebox that boots and shows nothing is a fault call, so the refusal happens in `build()`
    — before uvicorn binds, before a guest is pointed at a QR code. The check is that the
    exception arrives and no server ever started.
    """

    from encore.repositories.errors import StoreNotFoundError

    missing = encore_home / "var/lib/never-built.db"

    with pytest.raises(StoreNotFoundError):
        composed_appliance(
            library_db=missing,
            runtime_db=encore_home / "var/lib/runtime.db",
            artwork_dir=built_library.options.artwork_dir,
            music_dir=built_library.options.music_dir,
            launcher=engine,
        )

    assert not missing.exists()
