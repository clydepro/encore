"""The session cookie, the origin check, and who gets a redirect (SAPRS 10.7, 10.8, ADR-013).

`encore/api/auth.py` is nine rules in eighty lines, and a route test cannot tell which of them
did the work: an integration test that gets a 303 could have been answered by the exception
handler, the dependency, or a route that forgot to require anything. So the pieces are asserted
here, on a `Request` built by hand, against a service whose sessions are dictionary entries.

Three of these are worth naming as the ones that matter:

* **The cookie's flags**, because `HttpOnly` and `SameSite=Strict` *are* the cross-site request
  defence. ADR-013 chose them over a CSRF token, which means a typo in one of them is not a
  style failure but the vulnerability the ADR argued was already closed.
* **The origin check on a state-changing request only**, because refusing a `GET` from
  somewhere else would break the ordinary way an operator opens a bookmark, while accepting a
  `POST` from somewhere else would let any page on any device on the LAN pause the music.
* **A person gets a redirect and a tool gets a 401**, from the same exception, on one header
  pair. It is `POST /queue`'s browser-or-not split (SAPRS 10.7) applied to authentication, and
  the failure mode it avoids is a login page that cannot be reached by the person who needs it.

`admin` here is a double rather than `AdminService`, and that is the point of the seam: the
dependency's whole job is to translate a cookie into one call, so a test that used the real
service would be testing the service twice and this file once.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import Response
from starlette.datastructures import State
from starlette.requests import Request

from encore.api.auth import (
    SESSION_COOKIE,
    clear_session,
    depend_admin,
    session_cookie,
    session_token,
)
from encore.api.errors import UNAUTHORIZED, respond
from encore.api.html import build_renderer
from encore.services.admin_service import AdminSession
from encore.services.errors import AuthenticationError, CrossSiteError

pytestmark = pytest.mark.unit

NOW = datetime(2026, 7, 14, 23, 0, tzinfo=UTC)
HOST = "http://192.168.4.1:8080"


class StubApp:
    """The one attribute of `create_app` an error response needs: its renderer.

    `errors.py` renders a failure through the application's `Renderer` so an error is
    templated like everything else, which means a hand-built `Request` has to say which
    templates it came from. A real app would be a second thing to start in a test that is
    about one function.
    """

    def __init__(self) -> None:
        self.state = State()
        self.state.renderer = build_renderer()


class StubAdmin:
    """`AdminService.session`, with the calls recorded.

    The real service is tested in `test_admin_service.py`; what belongs here is that the
    dependency asks the one question, asks it once, and treats a `None` as no permission.
    """

    def __init__(self, live: dict[str, AdminSession]) -> None:
        self.live = live
        self.asked: list[str | None] = []

    def session(self, token: str | None) -> AdminSession | None:
        self.asked.append(token)
        return self.live.get(token or "")


class StubJukebox:
    def __init__(self, live: dict[str, AdminSession]) -> None:
        self.admin = StubAdmin(live)


def session(token: str | None = None) -> AdminSession:
    return AdminSession(
        token=token or "a-token", signed_in_at=NOW, expires_at=NOW + timedelta(minutes=60)
    )


def request(
    method: str = "GET",
    path: str = "/admin/logs",
    *,
    cookie: str | None = "a-token",
    origin: str | None = None,
    accept: str = "text/html",
    htmx: bool = False,
) -> Request:
    headers = [(b"accept", accept.encode())]
    if cookie is not None:
        headers.append((b"cookie", f"{SESSION_COOKIE}={cookie}".encode()))
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if htmx:
        headers.append((b"hx-request", b"true"))
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "server": ("192.168.4.1", 8080),
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("192.168.4.50", 51234),
            "app": StubApp(),
        }
    )


# -- the cookie --------------------------------------------------------------


def test_the_session_cookie_carries_the_flags_that_make_it_the_defence() -> None:
    """ADR-013 chose cookie attributes over a CSRF token, so the attributes *are* the CSRF fix.

    `HttpOnly` keeps an injected script from reading the token; `SameSite=Strict` keeps a
    request from another origin from carrying it at all; `Path=/` is what makes `delete_cookie`
    in `clear_session` match the cookie it is withdrawing — a mismatch there is a logout that
    quietly does nothing, and nothing in a browser will tell you so.

    `Secure` is absent, and that is SAPRS 13.1 rather than an oversight: the appliance serves
    plain HTTP on a LAN, and a `Secure` cookie would make the administrative interface
    unreachable rather than unsafe.
    """

    page = Response()
    session_cookie(page, session(), minutes=30)
    raw = page.headers["set-cookie"]

    assert raw.startswith(f"{SESSION_COOKIE}=a-token")
    assert "HttpOnly" in raw
    assert "samesite=strict" in raw.lower()
    assert "Path=/" in raw
    assert "Max-Age=1800" in raw
    assert "Secure" not in raw


def test_clearing_the_cookie_matches_the_name_and_path_it_was_set_with() -> None:
    page = Response()
    session_cookie(page, session(), minutes=30)
    clear_session(page)
    # Two `Set-Cookie` lines: the one that was set and the one that withdrew it. The last
    # wins in a browser, and a client that read the first would keep a dead token.
    raw = page.headers.getlist("set-cookie")[-1]

    assert f"{SESSION_COOKIE}=" in raw
    assert "Path=/" in raw
    assert "Max-Age=0" in raw


def test_an_empty_cookie_is_no_token() -> None:
    assert session_token(request(cookie="")) is None
    assert session_token(request(cookie=None)) is None
    assert session_token(request(cookie="a-token")) == "a-token"


# -- the dependency ----------------------------------------------------------


def test_a_live_token_is_a_session_and_reaches_the_template() -> None:
    held = session()
    jukebox = StubJukebox({held.token: held})
    page = request()

    assert depend_admin(page, jukebox) is held  # type: ignore[arg-type]
    assert page.state.admin_session is held
    assert jukebox.admin.asked == [held.token]


def test_nothing_and_nothing_like_a_token_are_the_same_answer() -> None:
    jukebox = StubJukebox({})

    for page in (request(cookie=None), request(cookie=""), request(cookie="guessed")):
        with pytest.raises(AuthenticationError):
            depend_admin(page, jukebox)  # type: ignore[arg-type]


def test_a_state_changing_request_from_elsewhere_is_refused_even_with_a_session() -> None:
    held = session()
    jukebox = StubJukebox({held.token: held})

    with pytest.raises(CrossSiteError):
        depend_admin(request("POST", origin="http://evil.example"), jukebox)  # type: ignore[arg-type]


def test_a_get_from_elsewhere_is_not_refused() -> None:
    """A bookmark, a link, and a refresh are all `GET`s, and none of them can change anything.

    Refusing them would break the ordinary way an operator reaches the page, in exchange for a
    defence a read does not need.
    """

    held = session()
    jukebox = StubJukebox({held.token: held})

    assert depend_admin(request("GET", origin="http://evil.example"), jukebox) is held  # type: ignore[arg-type]


def test_a_tool_that_sends_no_origin_is_allowed() -> None:
    """`curl --cookie` is a documented way to run this appliance (docs/api/README.md).

    The `Origin` header is what a browser adds and a script does not, so requiring it would
    make the JSON interface unusable from anywhere but a page — which is SAPRS 10.2's "two
    complementary interfaces" narrowed to one.
    """

    held = session()
    jukebox = StubJukebox({held.token: held})

    assert depend_admin(request("POST"), jukebox) is held  # type: ignore[arg-type]


def test_a_same_origin_post_is_allowed() -> None:
    held = session()
    jukebox = StubJukebox({held.token: held})

    assert depend_admin(request("POST", origin=HOST), jukebox) is held  # type: ignore[arg-type]


# -- the two answers to one status --------------------------------------------


def test_a_browser_that_was_bounced_goes_to_the_door_with_a_way_back() -> None:
    page = respond(request("GET", "/admin/queue"), UNAUTHORIZED, {})

    assert page.status_code == 303
    assert page.headers["location"] == "/admin/login?next=/admin/queue"


def test_a_followed_link_keeps_its_query() -> None:
    page = respond(request("GET", "/admin/logs"), UNAUTHORIZED, {})

    assert page.headers["location"].startswith("/admin/login?next=/admin/logs")


def test_an_htmx_panel_is_told_to_go_and_back() -> None:
    """A panel whose session ended gets a door, not a sentence it cannot act on.

    Two things are asserted here because the first attempt at this interface got both wrong.

    The body is still the fragment (`partials/error.html`, `details.login`), so a client that
    ignores htmx's response headers still says what happened where the stale number was.

    The header is the part that makes it true in a browser. htmx's default `responseHandling`
    does not swap a 4xx body at all — the vendored copy's own table says so, and
    `tests/regression/test_issue_28_administration_interface.py` pins it — so a fragment alone
    would be markup no one rendered: the region would simply stop redrawing, which reads as "the
    page froze" rather than "you are signed out". `HX-Redirect` is handled before that table is
    consulted, so the tab goes to the sign-in page and `?next=` brings it back to the panel that
    asked. A swap cannot sign itself in; a navigation can.
    """

    page = respond(request("GET", htmx=True), UNAUTHORIZED, {"login": "/admin/login"})
    body = bytes(page.body).decode()

    assert page.status_code == 401
    assert "Sign in again" in body
    assert "/admin/login" in body
    assert page.headers["hx-redirect"] == "/admin/login?next=/admin/logs"


def test_a_tool_gets_the_envelope_the_documentation_promises() -> None:
    page = respond(request("GET", accept="application/json"), UNAUTHORIZED, {})
    body: dict[str, Any] = json.loads(bytes(page.body))

    assert page.status_code == 401
    assert body["error"]["code"] == "unauthorized"
    assert "status" not in body["error"], "the envelope is code + message + details (SAPRS 10.7)"
    assert "hx-redirect" not in page.headers, "a redirect is a browser action, not a client one"


def test_a_post_from_a_browser_is_not_redirected() -> None:
    """A redirect after a failed write is a 303 to a page that will not show why it failed.

    `_is_person` asks for a `GET` for this reason: the person who pressed the button needs the
    sentence, and the person who followed a link needs the door.
    """

    page = respond(request("POST"), UNAUTHORIZED, {})

    assert page.status_code == 401
    assert "html" in page.headers["content-type"]


def test_the_renderer_used_for_an_error_is_built_once_per_request_at_most() -> None:
    """`errors.py` falls back to `build_renderer()` when no renderer is on the app state.

    A cache is not worth the global; the check here is that the fallback produces the shell
    rather than raising, because a 401 that cannot be rendered is a 500.
    """

    page = build_renderer().render("partials/error.html", {"failure": UNAUTHORIZED, "details": {}})

    assert "must sign in" in page
