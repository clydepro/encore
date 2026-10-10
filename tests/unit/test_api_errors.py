"""A failure as three interfaces see it (SAPRS 10.7, 10.8, AEP 15).

`encore/api/errors.py` holds the only table in the HTTP layer a reviewer has to agree with:
which exception becomes which status, and which status becomes which code. Everything else
about an error is inherited from FastAPI, and the thing worth testing is that this is *only*
a mapping — no logic, no guesses, one place.

Three properties a framework's defaults would get wrong:

* **The envelope is the documented shape** — `{"error": {code, message, details}}`, as
  `docs/api/README.md` promises. SAPRS 10.7's requirement is that a client can branch, and
  it branches on `error.code`; a client that has to read a sentence to find out whether to
  retry is a client that will not.
* **The same failure is a fragment for HTMX, JSON for a machine and a page for a person.**
  One header chooses, and SAPRS 10.6's "fragments designed for direct DOM replacement" means
  the HTMX case is markup a swap can use rather than a JSON object rendered as text.
* **Nothing leaks.** A traceback, a SQL fragment or an absolute path in a body is an
  information disclosure on a machine that also holds other people's music, and the case
  where it is most likely is the handler for the errors nobody anticipated.

These are ASGI-level rather than in-process tests, through `tests/support/http.py`, because
the subject is a response: the status, the headers and the body a client receives. Calling
`respond()` directly would test the table and miss the wiring that puts it on an
`ExceptionMiddleware` — which is the half that a FastAPI upgrade breaks.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Response

from encore.api import errors
from encore.domain import SongId
from encore.playback import MpvTimeoutError
from encore.search import SearchUnavailableError
from encore.services.errors import QueueFullError, SongNotAvailableError
from encore.utilities.appliance import ApplianceNotRunning, WrongThreadError
from tests.support.http import running

HTMX = {"hx-request": "true"}
BROWSER = {"accept": "text/html,application/xhtml+xml"}
NEITHER = {"accept": "*/*"}


class Reply:
    """A response, read and closed.

    An `httpx` response is only usable inside the client that made it, and a test that kept
    its assertions inside a context block would be a test that forgot to. This is the
    one-line version of reading everything a test might ask about — status, headers, body —
    before the connection is gone.
    """

    def __init__(self, response: httpx.Response) -> None:
        self.status = response.status_code
        self.headers = dict(response.headers)
        self.text = response.text

    def json(self) -> Any:
        return json.loads(self.text)


async def call(app: FastAPI, method: str = "get", path: str = "/failing", **kwargs: Any) -> Reply:
    async with running(app) as client:
        return Reply(await getattr(client, method)(path, **kwargs))


BROWSER = {"accept": "text/html,application/xhtml+xml"}
NEITHER = {"accept": "*/*"}


def failing(raiser: Any, path: str = "/failing") -> FastAPI:
    """An app whose only route raises, so the mapping is everything under test.

    `state.renderer` is left unset on purpose in some tests and not others: an error path
    that needed templates to exist would be an error path that fails at the worst moment.
    """

    app = FastAPI()
    app.state.renderer = None

    @app.get(path)
    async def route() -> Response:
        raiser()
        return Response("unreachable")  # pragma: no cover - the raiser always raises

    errors.register_error_handlers(app)
    return app


def queue_full() -> None:
    raise QueueFullError(200)


def missing_song() -> None:
    raise SongNotAvailableError(SongId(4242))


def search_down() -> None:
    raise SearchUnavailableError("the library has no search index")


def not_ready() -> None:
    raise ApplianceNotRunning("startup has not finished")


def wrong_thread() -> None:
    raise WrongThreadError("a service read must run on the appliance thread")


def exploded() -> None:
    raise RuntimeError("SELECT * FROM songs WHERE path = '/mnt/music/a.flac'")


def mpv_timeout() -> None:
    raise MpvTimeoutError("get_property did not answer in time")


# -- the table ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raiser", "status", "code"),
    [
        (queue_full, 409, "queue_full"),
        (missing_song, 404, "song_not_available"),
        (search_down, 503, "search_unavailable"),
        (not_ready, 503, "not_ready"),
        (wrong_thread, 503, "not_ready"),
        (exploded, 500, "internal"),
    ],
)
async def test_a_service_failure_becomes_the_status_the_api_reference_promises(
    raiser: Any, status: int, code: str
) -> None:
    """The rows of `docs/api/README.md`, in one parametrised assertion.

    Two places write the same sentence — the reference and `BY_EXCEPTION` — and this is the
    test that keeps them from meaning different things.
    """

    response = await call(failing(raiser))

    assert response.status == status
    assert response.json()["error"]["code"] == code


async def test_the_envelope_is_the_documented_shape() -> None:
    """`{"error": {"code", "message", "details"}}`, and nothing beside it.

    A response that added a top-level `status` or `timestamp` would be a second place to
    look, and a second thing for a client to be wrong about.
    """

    response = await call(failing(queue_full))

    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert isinstance(body["error"]["message"], str)


async def test_queue_full_carries_the_limit_a_guest_can_act_on() -> None:
    """SAPRS 8.4's visible ceiling, in the machine interface too.

    The message says the queue is full; `details.limit` says what full means on this
    appliance, which is the difference between an error a client can explain to someone and
    one it can only show them.
    """

    body = (await call(failing(queue_full))).json()

    assert body["error"]["details"] == {"limit": 200}


async def test_an_unavailable_song_carries_its_identifier() -> None:
    body = (await call(failing(missing_song))).json()

    assert body["error"]["details"] == {"song_id": 4242}


async def test_an_unexpected_failure_says_nothing_about_itself() -> None:
    """The leak test. AEP 15's rule, in a body a client can read.

    The full detail goes to the journal, which milestone 5's log tail will show to an
    authenticated operator; the response says only that the appliance could not do it.
    """

    response = await call(failing(exploded))

    body = response.text
    assert response.status == 500
    for leak in ("SELECT", "/mnt/music", "Traceback", "RuntimeError", "a.flac"):
        assert leak not in body, f"{leak} reached a guest's browser"
    assert response.json()["error"]["code"] == "internal"


async def test_the_unexpected_failure_is_logged_with_its_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Silence is the other half of not leaking.

    A 500 that leaves no record is a defect nobody can reproduce, so the handler logs the
    path at ERROR (AEP 15's pairing) and the response says nothing.
    """

    with caplog.at_level("ERROR", logger="encore.http"):
        await call(failing(exploded))

    assert "unhandled error" in caplog.text
    assert any(record.levelname == "ERROR" for record in caplog.records)


@pytest.mark.parametrize(
    ("raiser", "code"),
    [(not_ready, "not_ready"), (search_down, "search_unavailable"), (mpv_timeout, "internal")],
)
async def test_a_dependency_fault_is_reported_as_one(raiser: Any, code: str) -> None:
    """`MpvTimeoutError` has no row in the table, and should not.

    A command that timed out means the appliance is in trouble in a way a caller cannot act
    on, and giving it a friendly code would be a guess about recovery that only the playback
    supervisor is entitled to make. It arrives as `internal`, and it is logged in full.
    """

    body = (await call(failing(raiser))).json()

    assert body["error"]["code"] == code


# -- the three faces ------------------------------------------------------


async def test_an_htmx_request_is_answered_with_a_fragment() -> None:
    """SAPRS 10.6, at the one moment it matters.

    A swap that arrived as a JSON object would replace a panel with `{"error": …}` on
    screen. The fragment case is rendered markup, and the header that chooses is the one
    HTMX sends by itself.
    """

    app = failing(queue_full)
    from encore.api.html import build_renderer

    app.state.renderer = build_renderer()

    response = await call(app, "get", "/failing", headers=HTMX)

    assert response.status == 409
    assert response.headers["content-type"].startswith("text/html")
    assert "The queue holds 200 songs" in response.text
    assert "no-store" in response.headers["cache-control"]


async def test_an_api_request_is_json_even_when_the_client_says_nothing() -> None:
    """The path decides, because a script does not always say what it speaks.

    `Accept: */*` against `/api/…` should get the envelope: the caller asked for the JSON
    interface by naming it in the URL, which is the only signal a `curl` reliably sends.
    """

    response = await call(
        failing(queue_full, "/api/v1/failing"), path="/api/v1/failing", headers=NEITHER
    )

    assert response.json()["error"]["code"] == "queue_full"


async def test_a_persons_request_gets_a_page_that_explains_itself() -> None:
    """A browser navigating to `/albums/9999` is not an HTMX request.

    It gets a document with a stylesheet and a way home, rather than a fragment dropped into
    a page that never asked for one (SAPRS 10.8, for a reader with no JavaScript).
    """

    app = failing(missing_song)
    from encore.api.html import build_renderer

    app.state.renderer = build_renderer()

    response = await call(app, "get", "/failing", headers=BROWSER)

    assert response.status == 404
    assert "<!doctype html>" in response.text.lower()
    assert "Back to the jukebox" in response.text


async def test_a_fragment_answer_works_without_templates_at_all() -> None:
    """An app built for a unit test of the mapping must still answer correctly.

    `create_app` always sets a renderer; the fallback in `errors._render` exists so that a
    test of the table is not also a test of Jinja. The status and code are unchanged, which
    is all any client can see.
    """

    response = await call(failing(queue_full), headers=HTMX)

    assert response.status == 409
    assert "queue" in response.text.lower()


async def test_a_validation_failure_is_json_because_it_is_not_a_guests_problem() -> None:
    """`fallback="json"`, explained at the one call site that uses it.

    A malformed request is a client bug in any of the three interfaces: a person's form
    would not have produced it, and a swap target has nothing to draw. The field list is
    what a developer needs, so the envelope is where it goes.
    """

    app = FastAPI()
    app.state.renderer = None

    @app.get("/api/v1/thing")
    async def route(limit: int) -> Response:
        return Response(str(limit))

    errors.register_error_handlers(app)

    response = await call(app, "get", "/api/v1/thing", params={"limit": "plenty"})

    assert response.status == 422
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["details"]["fields"][0]["location"].endswith("limit")


async def test_an_http_exception_says_the_same_kind_of_word() -> None:
    """A 404 raised by a router, not by a service, still arrives with a `code`.

    Without the status table a client would see the envelope for one kind of missing thing
    and Starlette's `{"detail": …}` for another, which is the inconsistency SAPRS 10.7
    exists to prevent.
    """

    app = FastAPI()
    app.state.renderer = None

    @app.get("/gone")
    async def route() -> Response:
        raise HTTPException(status_code=404, detail="that album is not in the library")

    errors.register_error_handlers(app)

    body = (await call(app, headers=NEITHER, path="/gone")).json()

    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"] == "that album is not in the library"


async def test_a_method_the_route_does_not_have_is_reported_as_saprs_names_it() -> None:
    """405 comes from Starlette rather than from us, and still gets the envelope.

    The guest-facing case is a phone's bookmark of `/queue` opened with a POST after a
    reboot of the routing table; the answer should look like every other answer.
    """

    app = FastAPI()
    app.state.renderer = None

    @app.get("/only-get")
    async def route() -> Response:
        return Response("ok")

    errors.register_error_handlers(app)

    response = await call(app, "post", "/only-get", headers=NEITHER)

    assert response.status == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


async def test_details_carry_numbers_and_not_sentences() -> None:
    """`details` is for a client to branch on, so it holds values, not prose.

    The check is on the two exceptions that have anything to say: a limit and an identifier.
    A `details` dictionary with a message in it would be a second `message` field, and the
    one a client would read by mistake.
    """

    full = (await call(failing(queue_full))).json()["error"]["details"]
    missing = (await call(failing(missing_song))).json()["error"]["details"]

    assert all(isinstance(value, int) for value in full.values())
    assert all(isinstance(value, int) for value in missing.values())
